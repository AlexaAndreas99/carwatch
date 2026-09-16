"""Configurations: the `search` table read the way the UI talks about it.

**Terminology.** The dashboard says *configuration*; the code and the database
say `search`. They are the same thing seen from two sides, and the split is
deliberate — renaming the table would rewrite the rows every listing and price
observation hangs off, for no functional gain.

The mapping is one-to-many. One configuration in config.yaml with three
`sources:` is three `search` rows, one per site, so a single site being blocked
only takes down its own row. This module puts them back together:

    configuration  ->  Search(name="…", site="autovit")
                       Search(name="…", site="olx")
                       Search(name="…", site="mobilede")

Identity is the name, because that is what config.yaml keys on. URLs carry a
slug of it rather than a database id: a configuration has no single id — it has
one per site — and a slug survives the row churn that `sync_searches` does.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Optional

from sqlmodel import Session, select

from carwatch import site_rules
from carwatch.db import search_filters
from carwatch.models import Listing, Run, RunStatus, Search
from carwatch.web.merge import market_of, merge_listings

# The sidebar's health dot. Three states, in order of how loudly they ask for
# attention — `worst()` below relies on this order.
HEALTH_OK = "ok"
HEALTH_WARN = "warn"
HEALTH_BAD = "bad"
HEALTH_NONE = "none"  # never run: nothing to report yet, good or bad

_HEALTH_RANK = {HEALTH_OK: 0, HEALTH_NONE: 1, HEALTH_WARN: 2, HEALTH_BAD: 3}

ALL = "all"  # the sidebar's "All configurations" entry, and the default scope


def worst(states: Iterable[str]) -> str:
    """The loudest of several health states. One bad source makes a bad config."""
    return max(states, key=lambda s: _HEALTH_RANK.get(s, 0), default=HEALTH_NONE)


def slugify(name: str) -> str:
    """A name into something that reads well in a URL.

    Accent-folded because the configurations are Romanian-market and a name
    like "Dacia Sandero Stepway (bucureşti)" should not become percent-encoded
    soup in the address bar.
    """
    folded = unicodedata.normalize("NFKD", str(name))
    ascii_only = folded.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_only).strip("-").lower()
    return slug or "configuration"


@dataclass
class SourceHealth:
    """One `search` row — one site of one configuration — and how it is doing."""

    search: Search
    active_count: int = 0
    last_run: Optional[Run] = None

    @property
    def site(self) -> str:
        return self.search.site

    @property
    def status(self) -> str:
        return self.last_run.status.value if self.last_run else "never run"

    @property
    def note(self) -> Optional[str]:
        """A remark from a run that otherwise succeeded.

        `error_message` on an `ok` run is not an error — it is the collector
        saying something happened worth knowing, and today that means exactly
        one thing: the site relaxed the search and the non-matching results
        were discarded. That is the fact §5 wants on screen.
        """
        if self.last_run is None or self.last_run.status is not RunStatus.OK:
            return None
        return self.last_run.error_message or None

    @property
    def relaxed(self) -> bool:
        return bool(self.note and "relaxed" in self.note)

    @property
    def found(self) -> Optional[int]:
        return self.last_run.listings_found if self.last_run else None

    @property
    def health(self) -> str:
        """Green, amber or red for this one source (§5)."""
        if not self.search.enabled:
            return HEALTH_WARN
        if self.disallowed:
            return HEALTH_BAD
        if self.last_run is None:
            return HEALTH_NONE
        if self.last_run.status in (RunStatus.BLOCKED, RunStatus.ERROR):
            return HEALTH_BAD
        if self.last_run.status is RunStatus.PARTIAL:
            return HEALTH_WARN
        if self.note:  # the site relaxed the search
            return HEALTH_WARN
        if self.last_run.listings_found == 0:
            return HEALTH_WARN
        return HEALTH_OK

    @property
    def disallowed(self) -> Optional[str]:
        """Why CarWatch will not fetch this source's URL, or None.

        Reads the saved URL, not the last run: a URL hand-edited into
        config.yaml never reaches the form that would have refused it, and
        would otherwise sit there being fetched with nothing said.
        """
        return site_rules.refusal(self.search.site, self.search.url) or None

    @property
    def problem(self) -> Optional[str]:
        """Why this source is not green, in one line, or None when it is.

        Written for someone who has not been watching: "found 0" alone reads as
        a fact about the market, when for four days it has been a fact about
        the search URL.
        """
        if not self.search.enabled:
            return "disabled"
        # Ahead of the run's own verdict: a URL we should not be asking for is
        # the more important fact, whatever the last fetch happened to return.
        if self.disallowed:
            return self.disallowed
        if self.last_run is None:
            return "never run"
        if self.last_run.status is RunStatus.BLOCKED:
            return "blocked by the site"
        if self.last_run.status is RunStatus.ERROR:
            return self.last_run.error_message or "errored"
        if self.last_run.status is RunStatus.PARTIAL:
            return self.last_run.error_message or "partial"
        if self.relaxed:
            return "the site relaxed this search — its results did not match"
        if self.note:
            return self.note
        if self.last_run.listings_found == 0:
            return "found nothing on its last run"
        return None


@dataclass
class Configuration:
    """One entry in config.yaml, as the dashboard shows it."""

    name: str
    slug: str
    sources: list[SourceHealth] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    # Cars, not rows: an ad carried by both autovit and olx is one car, and a
    # count that disagreed with the Listings page would be worse than none.
    car_count: int = 0

    @property
    def enabled(self) -> bool:
        """Enabled if any of its sources is.

        Per-source rather than per-configuration in the database, because
        `sync_searches` disables the row for a source you removed from
        config.yaml while the configuration itself carries on.
        """
        return any(s.search.enabled for s in self.sources)

    @property
    def search_ids(self) -> set[int]:
        return {s.search.id for s in self.sources if s.search.id is not None}

    @property
    def sites(self) -> list[str]:
        return [s.site for s in self.sources]

    @property
    def health(self) -> str:
        if not self.enabled:
            return HEALTH_WARN
        return worst(s.health for s in self.sources)

    @property
    def problems(self) -> list[tuple[str, str]]:
        """(site, reason) for every source that is not green."""
        if not self.enabled:
            return [("", "archived — not collecting")]
        return [(s.site, s.problem) for s in self.sources if s.problem]

    @property
    def health_title(self) -> str:
        """The dot's tooltip: the whole story in one hover."""
        problems = self.problems
        if not problems:
            return "collecting cleanly"
        return "; ".join(f"{site}: {why}" if site else why for site, why in problems)

    @property
    def last_run(self) -> Optional[Run]:
        runs = [s.last_run for s in self.sources if s.last_run]
        return max(runs, key=lambda r: r.started_at) if runs else None


@dataclass
class ConfigurationIndex:
    """Every configuration, plus the one number none of them can answer alone.

    `total_cars` is not the sum of `car_count`: a car matching two
    configurations is counted by both, and "All configurations" adding those
    up would claim more cars than the Listings page can show. Counting the
    whole set once is the only figure that agrees with the page.
    """

    configurations: list["Configuration"] = field(default_factory=list)
    total_cars: int = 0


def load_configurations(session: Session) -> ConfigurationIndex:
    """Every configuration, with its per-source health and its car count.

    Four queries regardless of how many configurations there are. The obvious
    shape — a query per source — was what the Runs page did, and it is the
    thing that would make a sidebar rendered on every page expensive.
    """
    searches = session.exec(select(Search).order_by(Search.name, Search.site)).all()
    if not searches:
        return ConfigurationIndex()

    listings = session.exec(select(Listing).where(Listing.is_active == True)).all()  # noqa: E712

    by_search: dict[int, list[Listing]] = {}
    for row in listings:
        by_search.setdefault(row.search_id, []).append(row)

    last_runs = _last_run_per_search(session)

    grouped: dict[str, list[Search]] = {}
    for search in searches:
        grouped.setdefault(search.name, []).append(search)

    configurations: list[Configuration] = []
    for name, rows in grouped.items():
        sources = [
            SourceHealth(
                search=row,
                active_count=len(by_search.get(row.id, [])),
                last_run=last_runs.get(row.id),
            )
            for row in rows
        ]
        # filters_json is written from the same config block for every one of a
        # configuration's rows, so any of them answers for the whole.
        metadata = search_filters(rows[0])

        configurations.append(
            Configuration(
                name=name,
                slug=slugify(name),
                sources=sources,
                metadata=metadata,
                car_count=_car_count(
                    [x for row in rows for x in by_search.get(row.id, [])]
                ),
            )
        )

    _disambiguate(configurations)
    configurations.sort(key=lambda c: c.name.casefold())
    return ConfigurationIndex(
        configurations=configurations, total_cars=_car_count(listings)
    )


def _car_count(listings: list[Listing]) -> int:
    """Distinct cars, counted the way the Listings page counts them.

    Merging is per market: Romanian and German inventory are never the same
    car, so they must never share a merge group.
    """
    by_market: dict[str, list[Listing]] = {}
    for row in listings:
        by_market.setdefault(market_of(row.site), []).append(row)
    return sum(len(merge_listings(rows)) for rows in by_market.values())


def _last_run_per_search(session: Session) -> dict[int, Run]:
    """The most recent run for each `search` row, in one query."""
    runs = session.exec(select(Run).order_by(Run.started_at, Run.id)).all()
    latest: dict[int, Run] = {}
    for run in runs:  # ascending, so the last write per search wins
        latest[run.search_id] = run
    return latest


def _disambiguate(configurations: list[Configuration]) -> None:
    """Make slugs unique.

    "Qashqai 2024+" and "Qashqai 2024" slug identically. Rare, but a collision
    silently scopes the page to the wrong configuration, so the second and
    later claimants get a numeric suffix in a stable order.
    """
    seen: dict[str, int] = {}
    for configuration in sorted(configurations, key=lambda c: c.name):
        count = seen.get(configuration.slug, 0) + 1
        seen[configuration.slug] = count
        if count > 1:
            configuration.slug = f"{configuration.slug}-{count}"


def find(configurations: list[Configuration], key: str) -> Optional[Configuration]:
    """Resolve a `?config=` value: a slug, or the name itself.

    Names are accepted so a URL built by hand, or one carried over from the old
    `?search=<name>` filter, still lands somewhere sensible.
    """
    if not key or key == ALL:
        return None
    for configuration in configurations:
        if configuration.slug == key:
            return configuration
    folded = key.strip().casefold()
    for configuration in configurations:
        if configuration.name.casefold() == folded:
            return configuration
    return None


def owning(
    configurations: list[Configuration], search_id: int
) -> tuple[Optional[Configuration], Optional[SourceHealth]]:
    """Which configuration (and which of its sources) a `search` row belongs to.

    The reverse of the grouping this module does, for `/search/<id>` — the one
    page reached from a row rather than from a name.
    """
    for configuration in configurations:
        for source in configuration.sources:
            if source.search.id == search_id:
                return configuration, source
    return None, None


def visible(configurations: list[Configuration], show_disabled: bool) -> list[Configuration]:
    """What the sidebar lists.

    Archived configurations are hidden by default, the way Changes hides the
    initial listings: they are still collecting nothing, and a sidebar that
    accumulates every configuration ever tried stops being a quick way to reach
    the two you use.
    """
    if show_disabled:
        return configurations
    return [c for c in configurations if c.enabled]
