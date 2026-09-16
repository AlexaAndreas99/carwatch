"""CLI entry point: python -m carwatch.collect

    python -m carwatch.collect --all
    python -m carwatch.collect --search "Nissan Qashqai 2025 4x4 Tekna"
    python -m carwatch.collect --all --site autovit
    python -m carwatch.collect --status
    python -m carwatch.collect --backfill-images

Exit codes: 0 all sources ok, 1 bad arguments, 2 bad config, 5 at least one
source was blocked or errored (the others still collected normally), 6 another
collection was already running so this one was skipped.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from sqlmodel import select

from carwatch.collector.backfill import backfill_image_urls
from carwatch.collector.engine import collect
from carwatch.collector.lock import CollectionBusy
from carwatch.console import setup_console
from carwatch.config import Config, ConfigError, load_config
from carwatch.db import init_db, search_filters, session_scope, sync_searches
from carwatch.models import Listing, Run, Search

DEFAULT_CONFIG = "config.yaml"


def _print_config(config: Config) -> None:
    s = config.settings
    lo, hi = s.request_delay_seconds
    print(f"Config:   {config.path}")
    print(f"Database: {config.db_file}")
    print(
        f"Settings: delay {lo}-{hi}s | delist after {s.delist_after_missed_runs} "
        f"missed run(s) | UA {s.user_agent!r}"
    )
    if s.blocked_cooldown_hours:
        print(f"          back off {s.blocked_cooldown_hours:g}h after a site blocks us")
    for site in sorted(s.per_site):
        site_settings = s.for_site(site)
        slo, shi = site_settings.request_delay_seconds
        print(f"          {site}: delay {slo}-{shi}s")
    print()

    for sc in config.searches:
        flag = "enabled" if sc.enabled else "disabled"
        print(f"  {sc.name}  [{flag}]")
        if sc.metadata:
            meta = "  ".join(f"{k}={v}" for k, v in sorted(sc.metadata.items()))
            print(f"    meta: {meta}")
        for src in sc.sources:
            print(f"    - {src.site:<9} {src.url}")
        print()


def _print_db_state(config: Config) -> None:
    with session_scope(config.db_file) as session:
        rows = sync_searches(session, config)
        session.commit()

        print("Database state:")
        for row in sorted(rows, key=lambda r: (r.name, r.site)):
            active = len(
                session.exec(
                    select(Listing).where(
                        Listing.search_id == row.id, Listing.is_active == True  # noqa: E712
                    )
                ).all()
            )
            last_run = session.exec(
                select(Run).where(Run.search_id == row.id).order_by(Run.started_at.desc())
            ).first()
            when = (
                f"{last_run.started_at:%Y-%m-%d %H:%M} ({last_run.status.value})"
                if last_run
                else "never run"
            )
            flag = "" if row.enabled else "  [disabled]"
            print(f"  #{row.id} {row.name} / {row.site}{flag}")
            print(f"      active listings: {active}   last run: {when}")

        stale = session.exec(select(Search).where(Search.enabled == False)).all()  # noqa: E712
        if stale:
            print()
            print(f"  ({len(stale)} search/site row(s) disabled — not in config or enabled: false)")


def cmd_init(config: Config) -> int:
    init_db(config.db_file)
    print(f"Database ready: {config.db_file}")
    with session_scope(config.db_file) as session:
        rows = sync_searches(session, config)
    print(f"Synced {len(rows)} search/site row(s) from config.")
    return 0


def cmd_status(config: Config) -> int:
    init_db(config.db_file)
    _print_config(config)
    _print_db_state(config)
    return 0


def cmd_backfill_images(config: Config) -> int:
    """Fill in photos for listings collected before `image_url` existed."""
    init_db(config.db_file)
    with session_scope(config.db_file) as session:
        result = backfill_image_urls(session)

    if not result.filled and not result.unavailable:
        print("Every listing already has an image URL. Nothing to do.")
        return 0

    for site in sorted(result.filled):
        print(f"  {site:<9} filled {result.filled[site]} listing(s) from raw_json")
    for site in sorted(result.unavailable):
        print(
            f"  {site:<9} {result.unavailable[site]} listing(s) have no stored image; "
            f"re-collect this site to pick their photos up"
        )
    print()
    print(f"Filled {result.total_filled}, still missing {result.total_unavailable}.")
    return 0


def cmd_collect(
    config: Config,
    which: str | None,
    run_all: bool,
    site: str | None,
    force: bool = False,
) -> int:
    if run_all:
        if not config.enabled_searches():
            print("No enabled searches in config.", file=sys.stderr)
            return 1
        names = None
    else:
        assert which is not None
        found = config.find_search(which)
        if found is None:
            configured = ", ".join(repr(s.name) for s in config.searches) or "(none)"
            print(f"No search named {which!r}. Configured: {configured}", file=sys.stderr)
            return 1
        if not found.enabled:
            print(f"Search {found.name!r} is disabled in config.", file=sys.stderr)
            return 1
        names = [found.name]

    try:
        summaries = collect(
            config,
            search_names=names,
            sites=[site] if site else None,
            force=force,
        )
    except CollectionBusy as exc:
        # A scheduled run overlapping with a dashboard "Run now" is the normal
        # cause. Skipping is correct - two runs would diff against stale state.
        print(f"Skipped: {exc}", file=sys.stderr)
        return 6

    if not summaries:
        print("Nothing to collect (no matching enabled search/site).", file=sys.stderr)
        return 1

    print(f"{'SEARCH / SITE':<46} {'STATUS':<8} {'FOUND':>6} {'NEW':>4} {'PRICE':>6} {'GONE':>5}")
    print("-" * 82)
    for s in summaries:
        label = f"{s.search_name} / {s.site}"
        if len(label) > 45:
            label = label[:42] + "..."
        print(
            f"{label:<46} {s.status.value:<8} {s.listings_found:>6} "
            f"{s.new_count:>4} {s.price_change_count:>6} {s.delisted_count:>5}"
        )

    for s in summaries:
        if s.note:
            print(f"\nnote  [{s.search_name} / {s.site}] {s.note}")
        if s.error_message:
            print(f"\n{s.status.value.upper()}  [{s.search_name} / {s.site}]")
            print(f"      {s.error_message}")
            print("      Listings were left untouched — a failed fetch is never")
            print("      treated as 'everything got delisted'.")

    # Blocked/error on any source is worth a non-zero exit so a scheduled run
    # surfaces the problem, but the run itself still did its job for the rest.
    return 0 if all(s.ok for s in summaries) else 5


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m carwatch.collect",
        description="CarWatch collector — run a collection and exit.",
    )
    p.add_argument(
        "-c",
        "--config",
        default=DEFAULT_CONFIG,
        help=f"path to config.yaml (default: {DEFAULT_CONFIG})",
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument("--search", metavar="NAME", help="collect one search by name")
    group.add_argument("--all", action="store_true", help="collect every enabled search")
    group.add_argument(
        "--init", action="store_true", help="create the database and exit"
    )
    group.add_argument(
        "--status",
        action="store_true",
        help="show the parsed config and current DB state, then exit",
    )
    group.add_argument(
        "--backfill-images",
        action="store_true",
        help="fill in listing photos from data already stored, then exit",
    )
    p.add_argument(
        "--site",
        help="restrict the collection to one site (autovit, olx, mobilede)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="collect even if a site is in its post-block cooldown",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true", help="show adapter warnings"
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_console()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        if not Path(args.config).is_file():
            print(
                "Pass --config with the path to your config.yaml, or run from the "
                "project directory.",
                file=sys.stderr,
            )
        return 2

    if args.init:
        return cmd_init(config)
    if args.backfill_images:
        return cmd_backfill_images(config)
    if args.search or args.all:
        return cmd_collect(config, args.search, args.all, args.site, args.force)
    return cmd_status(config)


if __name__ == "__main__":
    raise SystemExit(main())
