"""autovit.ro adapter — the reference implementation (spec §7).

Data source: autovit is a Next.js app whose search page embeds its GraphQL (urql)
SSR cache in a `<script id="__NEXT_DATA__">` block. We parse `advertSearch` out
of that.

Why not the JSON API the page itself calls? autovit's robots.txt sets
`Disallow: /api/` for `User-agent: *`, so hitting the GraphQL endpoint directly
would be off-limits. The search results page is allowed (`Allow: /`) and already
contains the same structured data — so we read the page a crawler may read and
take the JSON it embeds. Same stability benefit as an API, no robots violation.

Pagination: `?page=N`, 32 results per page. `advertSearch.totalCount` says how
many to expect; a page past the end returns an empty `edges` list.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable, Optional

from carwatch.adapters.base import AdapterError, BlockedError, RawListing, report_page
from carwatch.adapters.http import PoliteClient, with_page_param

# `nonce` and `crossorigin` attributes sit between the id and the closing angle
# bracket, so match any attributes rather than a fixed order.
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)

# Absolute safety net so a pagination bug can never hammer the site.
MAX_PAGES = 50

log = logging.getLogger(__name__)


class AutovitAdapter:
    site_name = "autovit"

    def __init__(self, settings: Any = None) -> None:
        self.user_agent = getattr(settings, "user_agent", "CarWatch/1.0 (personal use)")
        self.delay_range = tuple(getattr(settings, "request_delay_seconds", (1.5, 4.0)))
        # Set by the last fetch_listings() call when autovit widened the search
        # behind our back. Read by the probe CLI and the collector to explain a
        # zero-result run. See _relaxation_summary().
        self.last_relaxation: Optional[dict] = None

    # ---------------------------------------------------------------- fetching

    def fetch_listings(
        self, search_url: str, max_pages: Optional[int] = None
    ) -> list[RawListing]:
        """Fetch every result page for `search_url` and return normalized listings.

        `max_pages` stops early. Collection never passes it — it wants the whole
        search — but *asking a question about* a URL does: checking whether a
        URL parses and matches anything needs one page, and walking fifty to
        answer it is rude to the site for no extra information.
        """
        listings: list[RawListing] = []
        seen_ids: set[str] = set()
        total_count: Optional[int] = None
        self.last_relaxation = None

        with PoliteClient(self.user_agent, self.delay_range) as client:
            for page in range(1, min(MAX_PAGES, max_pages or MAX_PAGES) + 1):
                report_page(self, page)
                url = with_page_param(search_url, page)
                html = client.get_html(url)
                advert_search = self._extract_advert_search(html, url)

                # autovit "relaxes" a search that would return nothing: it widens
                # a filter (e.g. year >= 2025 becomes year >= 2024) and returns
                # those results instead, with no visible difference in the page.
                # Those cars do NOT match the user's search. Ingesting them would
                # create phantom `new` events now, and phantom `delisted` events
                # later when a real match appears and the site stops relaxing.
                # The honest answer to a relaxed search is zero results.
                relaxation = advert_search.get("relaxation") or {}
                if relaxation.get("applied"):
                    self.last_relaxation = relaxation
                    log.warning(
                        "autovit relaxed this search (%s) and returned "
                        "non-matching results; treating as 0 matches. %s",
                        relaxation.get("rule") or "unspecified rule",
                        self._relaxation_summary(relaxation),
                    )
                    return []

                if total_count is None:
                    total_count = advert_search.get("totalCount")

                edges = advert_search.get("edges") or []
                if not edges:
                    break  # past the last page

                new_on_page = 0
                for edge in edges:
                    node = (edge or {}).get("node") or {}
                    listing = self._parse_node(node)
                    if listing is None or listing.site_listing_id in seen_ids:
                        continue
                    seen_ids.add(listing.site_listing_id)
                    listings.append(listing)
                    new_on_page += 1

                # Every id on this page was already seen — the site is looping us
                # over the same results instead of paginating. Stop rather than
                # walk to MAX_PAGES.
                if new_on_page == 0:
                    break

                if isinstance(total_count, int) and len(listings) >= total_count:
                    break

        return listings

    def _extract_advert_search(self, html: str, url: str) -> dict:
        """Pull `advertSearch` out of the page's embedded urql cache."""
        match = _NEXT_DATA_RE.search(html)
        if not match:
            raise AdapterError(
                f"No __NEXT_DATA__ block in {url} — autovit's page structure has "
                f"probably changed, or we were served a non-results page."
            )

        try:
            data = json.loads(match.group(1))
        except ValueError as exc:
            raise AdapterError(
                f"__NEXT_DATA__ in {url} is not valid JSON: {exc}"
            ) from exc

        urql_state = data.get("props", {}).get("pageProps", {}).get("urqlState") or {}
        if not isinstance(urql_state, dict):
            raise AdapterError(f"Unexpected urqlState shape in {url}.")

        for entry in urql_state.values():
            payload = (entry or {}).get("data")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except ValueError:
                    continue
            if isinstance(payload, dict) and isinstance(
                payload.get("advertSearch"), dict
            ):
                return payload["advertSearch"]

        # A soft bot wall still renders __NEXT_DATA__, just without results — so
        # "page parsed but no advertSearch" is treated as blocked, not as zero
        # results. Mis-reading it as zero would delist every listing.
        raise BlockedError(
            f"No advertSearch payload in {url} — autovit served a page without "
            f"search results (possible soft block or changed page structure)."
        )

    @staticmethod
    def _relaxation_summary(relaxation: dict) -> str:
        """Human-readable "what did the site change" line.

        The relaxation filters carry `values` (what we asked for) and `value`
        (what autovit substituted), so we can name the widened filter exactly.
        """
        changes = []
        for f in relaxation.get("filters") or []:
            asked = f.get("values") or []
            used = f.get("value")
            if used is not None and asked and str(used) not in [str(a) for a in asked]:
                field = str(f.get("field", "?")).replace("filter_float_", "").replace(
                    "filter_enum_", ""
                )
                changes.append(f"{field}: asked {'/'.join(map(str, asked))}, used {used}")
        return "; ".join(changes) if changes else "(no per-filter detail given)"

    # ---------------------------------------------------------------- parsing

    def _parse_node(self, node: dict) -> Optional[RawListing]:
        """Map one GraphQL Advert node onto a RawListing."""
        site_id = node.get("id")
        url = node.get("url")
        if not site_id or not url:
            # Without a stable id we cannot diff this listing across runs, and a
            # positional fallback would corrupt the history. Skip it.
            return None

        params = self._parameters(node.get("parameters"))
        price, currency = self._price(node.get("price"))

        return RawListing(
            site_listing_id=str(site_id),
            url=str(url),
            title=self._title(node),
            price=price,
            currency=currency,
            make=params.get("make_display") or params.get("make"),
            model=params.get("model_display") or params.get("model"),
            year=_to_int(params.get("year")),
            mileage_km=_to_int(params.get("mileage")),
            fuel=params.get("fuel_type_display") or params.get("fuel_type"),
            # Not returned in autovit search results — only on the ad detail
            # page — so this is normally None here.
            gearbox=params.get("gearbox_display") or params.get("gearbox"),
            location=self._location(node.get("location")),
            image_url=self._image(node.get("thumbnail")),
            raw=node,
        )

    @staticmethod
    def _title(node: dict) -> str:
        """Prefer `shortDescription` — it is the descriptive one.

        `title` is sometimes just the bare model ("Nissan Qashqai") and sometimes
        a full spec line of its own ("Nissan Qashqai 1.3 l 156CP X-Tronic 4WD
        MHEV Tekna Plus"). Concatenating the two produces a duplicated mess in
        the second case, so we pick one. shortDescription is populated on every
        node we've seen and reads as a spec line; `title` is the fallback.
        Anything lost here (e.g. horsepower) is still in `parameters` and raw.
        """
        short = (node.get("shortDescription") or "").strip()
        title = (node.get("title") or "").strip()
        return short or title or "(untitled)"

    @staticmethod
    def _parameters(params: Optional[Iterable[dict]]) -> dict[str, str]:
        """Flatten the parameters list to {key: value, key_display: displayValue}."""
        out: dict[str, str] = {}
        for p in params or []:
            key = (p or {}).get("key")
            if not key:
                continue
            value = p.get("value")
            display = p.get("displayValue")
            if value is not None:
                out[key] = str(value)
            if display is not None:
                out[f"{key}_display"] = str(display)
        return out

    @staticmethod
    def _price(price: Optional[dict]) -> tuple[Optional[float], Optional[str]]:
        """Read price.amount — `units` is the whole-currency figure and `nanos`
        the fractional part. Falls back to the string `value` if units is absent."""
        amount = (price or {}).get("amount") or {}

        units = amount.get("units")
        if isinstance(units, (int, float)):
            nanos = amount.get("nanos") or 0
            total = float(units) + (float(nanos) / 1_000_000_000)
        else:
            total = _to_float(amount.get("value"))
            if total is None:
                return None, amount.get("currencyCode")

        return total, amount.get("currencyCode")

    @staticmethod
    def _image(thumbnail: Optional[dict]) -> Optional[str]:
        """The card photo, from the node's own thumbnail block.

        `x1` is 320x240 and `x2` 640x480. Cards are ~268px wide, so x1 is
        already soft on a 2x display; x2 is the one that stays sharp and is
        still a small file. Both are hot-linked, never downloaded (§4).
        """
        thumb = thumbnail or {}
        for key in ("x2", "x1"):
            url = (thumb.get(key) or "").strip()
            if url:
                return url
        return None

    @staticmethod
    def _location(location: Optional[dict]) -> Optional[str]:
        loc = location or {}
        city = ((loc.get("city") or {}).get("name") or "").strip()
        region = ((loc.get("region") or {}).get("name") or "").strip()
        if city and region and city.casefold() != region.casefold():
            return f"{city}, {region}"
        return city or region or None


def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(float(str(value).replace(" ", "").replace(",", "")))
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(str(value).replace(" ", "").replace(",", ""))
    except (TypeError, ValueError):
        return None
