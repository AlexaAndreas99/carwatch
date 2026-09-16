"""Adapter preview: python -m carwatch.probe

Fetches a search URL through its site adapter and prints the normalized
listings, without touching the database. This is how you check an adapter is
parsing correctly before wiring it into the collector.

    python -m carwatch.probe --search "Nissan Qashqai 2025 4x4 Tekna" --site autovit
    python -m carwatch.probe --url "https://www.autovit.ro/autoturisme/nissan/qashqai"
    python -m carwatch.probe --url "..." --json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from typing import Optional

from carwatch.adapters.base import AdapterError, BlockedError, RawListing, get_adapter
from carwatch.config import KNOWN_SITES, ConfigError, Settings, load_config
from carwatch.console import setup_console

DEFAULT_CONFIG = "config.yaml"


def _site_from_url(url: str) -> Optional[str]:
    lowered = url.lower()
    for site, fragments in KNOWN_SITES.items():
        if any(f in lowered for f in fragments):
            return site
    return None


def _fmt_price(listing: RawListing) -> str:
    if listing.price is None:
        return "—"
    price = f"{listing.price:,.0f}".replace(",", " ")
    return f"{price} {listing.currency or ''}".strip()


def _fmt_km(listing: RawListing) -> str:
    if listing.mileage_km is None:
        return "—"
    return f"{listing.mileage_km:,} km".replace(",", " ")


def print_table(listings: list[RawListing]) -> None:
    if not listings:
        return

    rows = []
    for x in listings:
        rows.append(
            (
                x.site_listing_id,
                str(x.year or "—"),
                _fmt_price(x),
                _fmt_km(x),
                (x.fuel or "—"),
                (x.location or "—"),
                x.title,
            )
        )

    headers = ("ID", "YEAR", "PRICE", "MILEAGE", "FUEL", "LOCATION", "TITLE")
    widths = [
        max(len(headers[i]), max(len(r[i]) for r in rows)) for i in range(len(headers))
    ]
    # Let the title column run long rather than truncating useful trim info.
    widths[-1] = len(headers[-1])

    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(line)
    print("-" * min(len(line) + 40, 150))
    for r in rows:
        print("  ".join(str(r[i]).ljust(widths[i]) for i in range(len(r))))


def print_summary(listings: list[RawListing]) -> None:
    priced = [x.price for x in listings if x.price is not None]
    missing_price = sum(1 for x in listings if x.price is None)
    missing_year = sum(1 for x in listings if x.year is None)
    missing_km = sum(1 for x in listings if x.mileage_km is None)
    ids = [x.site_listing_id for x in listings]

    print()
    print(f"  listings parsed : {len(listings)}")
    print(f"  unique ids      : {len(set(ids))}" + ("  (OK)" if len(set(ids)) == len(ids) else "  ** DUPLICATES **"))
    if priced:
        print(
            f"  price range     : {min(priced):,.0f} – {max(priced):,.0f} "
            f"{listings[0].currency or ''}".replace(",", " ")
        )
    print(
        f"  missing fields  : price={missing_price}  year={missing_year}  mileage={missing_km}"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m carwatch.probe",
        description="Fetch a search URL through its adapter and print the parsed listings.",
    )
    p.add_argument("-c", "--config", default=DEFAULT_CONFIG)
    p.add_argument("--search", metavar="NAME", help="use a search URL from config.yaml")
    p.add_argument("--site", help="which site (required with --search if it has several)")
    p.add_argument("--url", help="probe an arbitrary search URL instead of config")
    p.add_argument("--json", action="store_true", help="dump full RawListings as JSON")
    p.add_argument("--limit", type=int, help="only print the first N listings")
    args = p.parse_args(argv)
    setup_console()

    if not args.url and not args.search:
        p.error("give either --search NAME or --url URL")

    settings = Settings()
    url = args.url
    site = args.site

    if args.search:
        try:
            config = load_config(args.config)
        except ConfigError as exc:
            print(f"Config error: {exc}", file=sys.stderr)
            return 2
        settings = config.settings
        search = config.find_search(args.search)
        if search is None:
            names = ", ".join(repr(s.name) for s in config.searches) or "(none)"
            print(f"No search named {args.search!r}. Configured: {names}", file=sys.stderr)
            return 1
        sources = search.sources
        if site:
            sources = [s for s in sources if s.site == site]
            if not sources:
                print(f"Search {search.name!r} has no {site!r} source.", file=sys.stderr)
                return 1
        if len(sources) > 1:
            have = ", ".join(s.site for s in sources)
            print(f"Search {search.name!r} has several sources ({have}); pass --site.", file=sys.stderr)
            return 1
        url, site = sources[0].url, sources[0].site
    else:
        # --url may still borrow settings (UA, delays) from config if present.
        try:
            settings = load_config(args.config).settings
        except ConfigError:
            pass
        site = site or _site_from_url(url)
        if not site:
            print(f"Could not tell which site {url!r} belongs to; pass --site.", file=sys.stderr)
            return 1

    print(f"site : {site}")
    print(f"url  : {url}")
    lo, hi = settings.request_delay_seconds
    print(f"ua   : {settings.user_agent}   delay {lo}-{hi}s between pages")
    print()

    try:
        adapter = get_adapter(site, settings)
        listings = adapter.fetch_listings(url)
    except BlockedError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        print("\nThe collector would record status='blocked' and skip diffing.", file=sys.stderr)
        return 3
    except AdapterError as exc:
        print(f"ADAPTER ERROR: {exc}", file=sys.stderr)
        return 4

    shown = listings[: args.limit] if args.limit else listings

    if args.json:
        print(json.dumps([asdict(x) for x in shown], indent=2, ensure_ascii=False))
        return 0

    relaxation = getattr(adapter, "last_relaxation", None)
    if relaxation:
        print("NOTE: the site RELAXED this search and returned non-matching results.")
        print(f"      rule    : {relaxation.get('rule')}")
        print(f"      changed : {adapter._relaxation_summary(relaxation)}")
        print("      Those results were discarded — the true match count is 0.")
        print()

    if not listings:
        print("No listings matched this search URL.")
        if not relaxation:
            print("(That is a valid result, not an error — but it proves nothing about")
            print(" the parser. Try a looser search URL to verify parsing works.)")
        return 0

    print_table(shown)
    if args.limit and len(listings) > args.limit:
        print(f"... and {len(listings) - args.limit} more")
    print_summary(listings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
