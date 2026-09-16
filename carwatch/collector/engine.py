"""Collection orchestration (spec §8).

One `Run` row per (search, site) per collection. The single most important rule
here: a fetch that failed or was blocked must never be diffed. Treating a failed
fetch as "no results" would mass-delist every listing and destroy the history
the tool exists to keep.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from sqlmodel import Session, select

from carwatch.adapters.base import (
    AdapterError,
    AdapterNotBuiltError,
    BlockedError,
    RawListing,
    get_adapter,
)
from carwatch.collector.diff import Change, diff
from carwatch.collector.lock import CollectionBusy, collection_lock
from carwatch.config import Config, Settings
from carwatch.db import init_db, session_scope, sync_searches
from carwatch.models import (
    Event,
    EventType,
    Listing,
    PriceHistory,
    Run,
    RunStatus,
    Search,
    utcnow,
)

log = logging.getLogger(__name__)

# A callable that turns a site key + settings into an adapter. Injectable so
# tests can run the whole engine without touching the network.
AdapterFactory = Callable[[str, Any], Any]


@dataclass
class RunSummary:
    """What one search's run did — returned to the CLI and the dashboard."""

    search_name: str
    site: str
    status: RunStatus
    listings_found: int = 0
    new_count: int = 0
    price_change_count: int = 0
    delisted_count: int = 0
    error_message: Optional[str] = None
    note: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status is RunStatus.OK


@dataclass
class Progress:
    """Where a collection has got to, for the dashboard's status strip.

    Mutated in place by the collecting thread and read by the requests polling
    the strip. Plain attribute writes only, so a reader sees at worst a value
    one step stale — fine for a line of text redrawn every two seconds.
    """

    steps: int = 0
    step: int = 0
    search: str = ""
    site: str = ""
    page: int = 0


def collect_search(
    session: Session,
    search: Search,
    settings: Settings,
    adapter_factory: AdapterFactory = get_adapter,
    force: bool = False,
    on_page: Optional[Callable[[int], None]] = None,
) -> RunSummary:
    """Run one search against its site and apply the diff. Never raises.

    `on_page` is handed to the adapter, which calls it with each page number
    it is about to fetch (`adapters.base.report_page`).

    `force` bypasses the blocked-cooldown. Runs are manual, so when the user
    asks for one they are present and waiting - a back-off meant for unattended
    scheduling should not silently refuse them.
    """
    summary = RunSummary(search_name=search.name, site=search.site, status=RunStatus.OK)

    # A site that blocked us recently is the last one to hammer on the next run.
    # Skipping is politer, and avoids turning a temporary block into a hardened
    # one. No Run row is written: nothing was attempted.
    cooldown = None if force else _cooldown_remaining(session, search, settings)
    if cooldown is not None:
        summary.status = RunStatus.BLOCKED
        summary.error_message = (
            f"skipped: {search.site} blocked us less than "
            f"{settings.blocked_cooldown_hours:g}h ago; backing off for another "
            f"{cooldown:.1f}h before trying again"
        )
        log.info("%s / %s: %s", search.name, search.site, summary.error_message)
        return summary

    run = Run(search_id=search.id, site=search.site, started_at=utcnow())
    session.add(run)
    session.flush()  # assign run.id for the events below

    # ---------------------------------------------------------------- fetch
    try:
        adapter = adapter_factory(search.site, settings.for_site(search.site))
        if on_page is not None:
            try:
                adapter.on_page = on_page
            except AttributeError:  # an adapter that will not take one just goes unreported
                pass
        fetched = adapter.fetch_listings(search.url)
    except BlockedError as exc:
        return _finish_failed(session, run, summary, RunStatus.BLOCKED, str(exc))
    except AdapterNotBuiltError as exc:
        # Not a failure of the site — just a phase we haven't reached. Recorded
        # as `partial` so it is visible without looking like a scraping error.
        return _finish_failed(session, run, summary, RunStatus.PARTIAL, str(exc))
    except AdapterError as exc:
        return _finish_failed(session, run, summary, RunStatus.ERROR, str(exc))
    except Exception as exc:  # noqa: BLE001 - one site must never break the others
        log.exception("Unexpected error collecting %s / %s", search.name, search.site)
        return _finish_failed(
            session, run, summary, RunStatus.ERROR, f"{type(exc).__name__}: {exc}"
        )

    # A site that silently widened the search returns results that don't match.
    # The adapter already discards them; surface why the run found nothing.
    relaxation = getattr(adapter, "last_relaxation", None)
    if relaxation:
        summary.note = (
            f"site relaxed the search ({relaxation.get('rule')}); "
            f"non-matching results discarded"
        )

    # --------------------------------------------------------------- diff
    existing = {
        x.site_listing_id: x
        for x in session.exec(select(Listing).where(Listing.search_id == search.id)).all()
    }
    result = diff(existing, fetched, settings.delist_after_missed_runs)

    for change in result.changes:
        _apply_change(session, run, search, existing.get(change.raw.site_listing_id), change)

    for delisting in result.delistings:
        listing = delisting.listing
        listing.missed_runs = delisting.missed_runs
        if delisting.delist_now:
            listing.is_active = False
            session.add(
                Event(
                    run_id=run.id,
                    listing_id=listing.id,
                    type=EventType.DELISTED,
                    old_price=listing.price,
                    new_price=None,
                    created_at=utcnow(),
                )
            )
        session.add(listing)

    # -------------------------------------------------------------- finish
    run.finished_at = utcnow()
    run.status = RunStatus.OK
    run.listings_found = len(fetched)
    run.new_count = result.new_count
    run.price_change_count = result.price_change_count
    run.delisted_count = result.delisted_count
    if summary.note:
        run.error_message = summary.note
    session.add(run)

    summary.listings_found = len(fetched)
    summary.new_count = result.new_count
    summary.price_change_count = result.price_change_count
    summary.delisted_count = result.delisted_count
    return summary


def _cooldown_remaining(
    session: Session, search: Search, settings: Settings
) -> Optional[float]:
    """Hours still to wait before retrying a site that blocked us, or None.

    Only `blocked` counts. An `error` is usually ours (a parse break, a network
    blip) and is worth retrying immediately; a block is the site saying no.
    """
    if settings.blocked_cooldown_hours <= 0:
        return None

    last = session.exec(
        select(Run).where(Run.search_id == search.id).order_by(Run.started_at.desc())
    ).first()
    if last is None or last.status is not RunStatus.BLOCKED:
        return None

    elapsed_hours = (utcnow() - last.started_at).total_seconds() / 3600.0
    remaining = settings.blocked_cooldown_hours - elapsed_hours
    return remaining if remaining > 0 else None


def _finish_failed(
    session: Session,
    run: Run,
    summary: RunSummary,
    status: RunStatus,
    message: str,
) -> RunSummary:
    """Record a failed fetch and skip diffing entirely (§8 step 1).

    No listing is touched: not `last_seen`, not `missed_runs`, not `is_active`.
    A blocked site looks identical to a site with zero results, so the only safe
    response is to change nothing.
    """
    run.finished_at = utcnow()
    run.status = status
    run.error_message = message[:2000]
    session.add(run)

    summary.status = status
    summary.error_message = message
    log.warning("%s / %s: %s — skipping diff", summary.search_name, summary.site, status.value)
    return summary


def _apply_change(
    session: Session,
    run: Run,
    search: Search,
    existing: Optional[Listing],
    change: Change,
) -> None:
    """Insert or update one listing, plus its price history and event rows."""
    raw = change.raw
    now = utcnow()

    if existing is None:
        listing = Listing(
            search_id=search.id,
            site=search.site,
            site_listing_id=raw.site_listing_id,
            url=raw.url,
            title=raw.title,
            make=raw.make,
            model=raw.model,
            year=raw.year,
            price=raw.price,
            currency=raw.currency,
            mileage_km=raw.mileage_km,
            fuel=raw.fuel,
            gearbox=raw.gearbox,
            location=raw.location,
            condition=raw.condition,
            image_url=raw.image_url,
            first_seen=now,
            last_seen=now,
            is_active=True,
            missed_runs=0,
            raw_json=json.dumps(raw.raw, ensure_ascii=False, default=str),
        )
        session.add(listing)
        session.flush()  # assign listing.id for the event / history rows
    else:
        listing = existing
        # Refresh the mutable fields — sellers edit ads in place.
        listing.url = raw.url
        listing.title = raw.title
        listing.make = raw.make
        listing.model = raw.model
        listing.year = raw.year
        listing.price = raw.price
        listing.currency = raw.currency
        listing.mileage_km = raw.mileage_km
        listing.fuel = raw.fuel
        listing.gearbox = raw.gearbox
        listing.location = raw.location
        listing.condition = raw.condition
        # Deliberately not symmetrical with the fields above: a card whose photo
        # failed to parse this run (lazy-loading, a layout tweak) should keep the
        # image we already have rather than blank the listing. The photo is the
        # one field where "no value" is far more likely to be our miss than the
        # seller removing it.
        if raw.image_url:
            listing.image_url = raw.image_url
        listing.last_seen = now
        listing.is_active = True
        listing.missed_runs = 0  # seen again, so the miss streak resets
        listing.raw_json = json.dumps(raw.raw, ensure_ascii=False, default=str)
        session.add(listing)

    if change.writes_price_history:
        session.add(
            PriceHistory(
                listing_id=listing.id,
                price=change.new_price,
                currency=raw.currency,
                observed_at=now,
            )
        )

    event_type = change.event_type
    if event_type is not None:
        session.add(
            Event(
                run_id=run.id,
                listing_id=listing.id,
                type=event_type,
                old_price=change.old_price,
                new_price=change.new_price,
                created_at=now,
            )
        )


def collect(
    config: Config,
    search_names: Optional[Iterable[str]] = None,
    sites: Optional[Iterable[str]] = None,
    adapter_factory: AdapterFactory = get_adapter,
    source: str = "cli",
    force: bool = False,
    progress: Optional[Progress] = None,
) -> list[RunSummary]:
    """Collect every enabled (search, site) pair, or a filtered subset.

    Each pair is committed on its own, so one site failing cannot roll back
    another's results.

    Holds a cross-process lock for the whole collection: a scheduled run and a
    dashboard "Run now" overlapping would each diff against a stale snapshot.
    Raises CollectionBusy if another collection is already running.
    """
    with collection_lock(config.db_file, source=source):
        return _collect_locked(
            config, search_names, sites, adapter_factory, force, progress
        )


def _collect_locked(
    config: Config,
    search_names: Optional[Iterable[str]],
    sites: Optional[Iterable[str]],
    adapter_factory: AdapterFactory,
    force: bool = False,
    progress: Optional[Progress] = None,
) -> list[RunSummary]:
    init_db(config.db_file)

    wanted_names = {n.casefold() for n in search_names} if search_names else None
    wanted_sites = {s.casefold() for s in sites} if sites else None

    with session_scope(config.db_file) as session:
        sync_searches(session, config)

    summaries: list[RunSummary] = []

    with session_scope(config.db_file) as session:
        rows = session.exec(select(Search).where(Search.enabled == True)).all()  # noqa: E712

    targets = [
        r
        for r in rows
        if (wanted_names is None or r.name.casefold() in wanted_names)
        and (wanted_sites is None or r.site.casefold() in wanted_sites)
    ]

    if progress is not None:
        progress.steps = len(targets)

    for step, row in enumerate(targets, start=1):
        on_page = None
        if progress is not None:
            progress.step, progress.search, progress.site, progress.page = (
                step,
                row.name,
                row.site,
                0,
            )
            on_page = lambda page, p=progress: setattr(p, "page", page)  # noqa: E731

        # One session (and therefore one transaction) per search.
        with session_scope(config.db_file) as session:
            search = session.get(Search, row.id)
            summaries.append(
                collect_search(
                    session,
                    search,
                    config.settings,
                    adapter_factory,
                    force=force,
                    on_page=on_page,
                )
            )

    return summaries
