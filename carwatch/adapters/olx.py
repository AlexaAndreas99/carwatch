"""olx.ro adapter (spec §7).

Data source — and why it differs from what §7 anticipated:

§7 expected `__NEXT_DATA__` or an internal offers API over plain HTTP. Neither is
available in practice:

  * olx.ro sits behind CloudFront and returns **403 to every plain-HTTP request**,
    regardless of headers — an honest UA, no UA, and a full Chrome header set all
    get the same 919-byte "Request blocked" page. Even `/robots.txt` is 403. The
    block is at the network/TLS-fingerprint layer, not the User-Agent, so there is
    no header combination that fixes it (and per §14 we do not impersonate to
    evade one).
  * The rendered search page carries no `__NEXT_DATA__` and no streamed RSC
    payload; the offers API exists but its parameters are not derivable from the
    pasted friendly URL.

So this adapter renders the page in a real browser (§4's sanctioned escalation
for "sites that return bot walls to plain HTTP") and parses the listing cards
from the DOM. Plain HTTP is still attempted first — it is cheap, and the block
may not apply from every network — and we fall back only when it fails.

Card structure (verified against a live search page):

    <div data-cy="l-card" id="305871621">              <- OLX ad id, stable
      <div data-testid="ad-card-title">
        <a data-testid="card-title-link" href="...">   <- may point at autovit.ro
          <h4>Nissan Qashqai ...</h4>
        <p data-testid="ad-price">26 980 €</p>
      <p data-testid="location-date">Alba Iulia - Reactualizat azi la 12:55</p>
      <span>2025  21 700 km</span>

Note that OLX syndicates autovit.ro ads, so a listing here may be the same
physical car as one in an autovit search. They are stored as separate listings
(keyed per search), which is correct — but see the README on de-duplication.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Optional

from selectolax.parser import HTMLParser, Node

from carwatch.adapters.base import AdapterError, BlockedError, RawListing, report_page
from carwatch.adapters.http import PoliteClient, with_page_param
from carwatch.adapters.images import image_url_from

log = logging.getLogger(__name__)

MAX_PAGES = 25

CARD_SELECTOR = '[data-cy="l-card"]'

# "26 980 €" / "24 649,52 €" / "115.000 lei" — Romanian formatting uses spaces
# (often non-breaking) for thousands and a comma for decimals.
_CURRENCY_BY_SYMBOL = (
    ("€", "EUR"),
    ("eur", "EUR"),
    ("lei", "RON"),
    ("ron", "RON"),
    ("$", "USD"),
)

_YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")

# The mileage figure immediately before "km". Two alternatives, in order:
#   1. a grouped number - "21 700", "165.000" (Romanian thousands separators,
#      where the space is often a non-breaking one; \s covers those)
#   2. a plain digit run - "21700", "1"
# A loose character class instead runs backwards over the year: "2024 1 km"
# then parses as mileage 20241 rather than year 2024 / mileage 1.
# The leading \b matters: without it "2018 165.000 km" matches from inside the
# year as "18 165.000" -> 18 165 000 km.
_KM_RE = re.compile(r"\b(\d{1,3}(?:[.\s]\d{3})+|\d+)\s*km\b", re.IGNORECASE)

# The card's params line, where year and mileage sit adjacent: "2025  21 700 km".
#
# This pairing is what makes the year trustworthy. A card's flattened text also
# contains the posting date ("Reactualizat la 31 august 2026") and often a year
# in the title ("An2025 Facelift"), so taking the first year found anywhere in
# the card reads the *listing date* as the model year — 2025 cars were coming
# back as 2026.
# The size directive in an apollo CDN URL: ";s=216x152" with an optional
# ";q=50" quality suffix.
_CDN_SIZE_RE = re.compile(r";s=\d+x\d+(?:;q=\d+)?")
# 4:3-ish at roughly 2x the 268px card called for in the front-end plan §7.
CARD_IMAGE_SIZE = ";s=644x461;q=80"

_PARAMS_RE = re.compile(
    r"\b(19[5-9]\d|20[0-4]\d)\s+(\d{1,3}(?:[.\s]\d{3})+|\d+)\s*km\b", re.IGNORECASE
)


class OlxAdapter:
    site_name = "olx"

    def __init__(self, settings: Any = None) -> None:
        self.user_agent = getattr(settings, "user_agent", "CarWatch/1.0 (personal use)")
        self.delay_range = tuple(getattr(settings, "request_delay_seconds", (1.5, 4.0)))
        self.settings = settings
        # Set when we had to fall back to a real browser, so the collector can
        # explain the slower run.
        self.used_browser = False

    # ---------------------------------------------------------------- fetching

    def fetch_listings(
        self, search_url: str, max_pages: Optional[int] = None
    ) -> list[RawListing]:
        listings: list[RawListing] = []
        seen_ids: set[str] = set()
        self.used_browser = False

        fetch_page = self._make_fetcher(search_url)

        for page in range(1, min(MAX_PAGES, max_pages or MAX_PAGES) + 1):
            report_page(self, page)
            url = with_page_param(search_url, page)
            html = fetch_page(url)
            cards = self._parse_page(html)

            if not cards:
                break

            new_on_page = 0
            for listing in cards:
                if listing.site_listing_id in seen_ids:
                    continue
                seen_ids.add(listing.site_listing_id)
                listings.append(listing)
                new_on_page += 1

            # OLX serves the last page again for out-of-range page numbers
            # instead of an empty result, so repeats mean "past the end".
            if new_on_page == 0:
                break

        return listings

    def _make_fetcher(self, search_url: str):
        """Return a `url -> html` callable, escalating to a browser if needed.

        Plain HTTP is tried once. If it is blocked we switch to a real browser
        for the whole run rather than retrying HTTP on every page.
        """
        client = PoliteClient(self.user_agent, self.delay_range)

        state = {"mode": "http", "browser": None}

        def fetch(url: str) -> str:
            if state["mode"] == "http":
                try:
                    return client.get_html(url)
                except BlockedError as exc:
                    log.info(
                        "olx blocked plain HTTP (%s); escalating to a real browser", exc
                    )
                    state["mode"] = "browser"
                    self.used_browser = True

            if state["browser"] is None:
                state["browser"] = self._open_browser()
            return state["browser"](url)

        return fetch

    def _open_browser(self):
        """Lazily start the shared Playwright helper.

        Everything that can go wrong here — Playwright not installed, Chromium
        not downloaded — is reported as BlockedError, because from the
        collector's point of view the outcome is the same as any other bot wall:
        we could not read the page, so nothing may be diffed or delisted. Letting
        a RuntimeError escape would land as status='error' with a raw traceback.
        """
        unavailable = BlockedError(
            "olx.ro blocks plain HTTP (CloudFront 403), so the page has to be "
            "rendered in a real browser — but Playwright is not available. "
            "Install it with:\n"
            "    pip install playwright\n"
            "    playwright install chromium"
        )

        try:
            from carwatch.adapters.browser import (
                BrowserFetcher,
                BrowserUnavailableError,
            )
        except ImportError as exc:
            raise unavailable from exc

        # Scrolling matters here: olx fills a card's image `src` only when the
        # card enters the viewport, so capturing straight after load gave
        # photos for 10 of 47 listings.
        fetcher = BrowserFetcher(delay_range=self.delay_range, scroll_to_bottom=True)

        def get_html(url: str) -> str:
            try:
                return fetcher.get_html(url)
            except BrowserUnavailableError as exc:
                raise BlockedError(f"{unavailable} ({exc})") from exc

        return get_html

    # ----------------------------------------------------------------- parsing

    def _parse_page(self, html: str) -> list[RawListing]:
        tree = HTMLParser(html)
        cards = tree.css(CARD_SELECTOR)

        if not cards and self._looks_like_results_page(tree):
            # A results page that renders zero cards is a legitimate empty
            # search, not a parse failure.
            return []

        out: list[RawListing] = []
        for card in cards:
            listing = self._parse_card(card)
            if listing is not None:
                out.append(listing)
        return out

    @staticmethod
    def _looks_like_results_page(tree: HTMLParser) -> bool:
        return bool(
            tree.css_first('[data-testid="total-count"]')
            or tree.css_first('[data-testid="listing-grid"]')
            or tree.css_first('[data-testid="listing-filters"]')
        )

    def _parse_card(self, card: Node) -> Optional[RawListing]:
        site_id = (card.attributes.get("id") or "").strip()
        link = card.css_first('a[data-testid="card-title-link"]') or card.css_first("a[href]")
        url = (link.attributes.get("href") if link else None) or ""
        url = url.strip()

        if not site_id or not url:
            # No stable id means we cannot diff it across runs; skip rather than
            # invent one from position.
            return None

        if url.startswith("/"):
            url = "https://www.olx.ro" + url

        title = self._text(card.css_first('[data-testid="ad-card-title"] h4'))
        if not title and link is not None:
            title = (link.attributes.get("aria-label") or "").strip()
        if not title:
            title = self._text(card.css_first("h4")) or "(untitled)"
        title = self._normalise_title(title)

        price, currency = self._parse_price(
            self._text(card.css_first('[data-testid="ad-price"]'))
        )
        location = self._parse_location(
            self._text(card.css_first('[data-testid="location-date"]'))
        )

        card_text = card.text(separator=" ", strip=True)
        year, mileage = self._parse_year_and_mileage(card_text)

        return RawListing(
            site_listing_id=site_id,
            url=url,
            title=title,
            price=price,
            currency=currency,
            year=year,
            mileage_km=mileage,
            location=location,
            image_url=self._parse_image(card),
            # olx cards don't expose make/model/fuel/gearbox as structured
            # fields — only the free-text title. Left None rather than guessed
            # at from substring matching, which would be wrong as often as right.
            raw={"card_text": card_text, "url": url},
        )

    # ------------------------------------------------------------- field parsers

    @classmethod
    def _parse_image(cls, card: Node) -> Optional[str]:
        """The card photo, hot-linked from OLX's CDN (frankfurt.apollo.olxcdn.com).

        A result card holds exactly one photo; the favourite control is a
        `<button>`, not an image. So the first `<img>` with a real URL is the
        car — no need to exclude anything the way mobile.de's cards require.
        """
        for img in card.css("img"):
            url = image_url_from(img)
            if url:
                return cls._card_sized(url)
        return None

    @staticmethod
    def _card_sized(url: str) -> str:
        """Ask the OLX CDN for a card-sized image instead of a list thumbnail.

        The size lives in the URL — `…/image;s=216x152;q=50` — and the CDN
        renders whatever size is asked for from the same source file (checked
        against live URLs: 216x152 at q=50 is 4 KB, 644x461 at q=80 is 31 KB,
        both HTTP 200). 216px wide is what OLX's own list needs and is visibly
        soft in a 268px card on a 2x display, so we ask for the size the card
        actually uses. Still hot-linking (§4) — same host, same file, one
        parameter changed.
        """
        if "apollo.olxcdn.com" not in url:
            return url
        upgraded, count = _CDN_SIZE_RE.subn(CARD_IMAGE_SIZE, url, count=1)
        return upgraded if count else url

    @staticmethod
    def _text(node: Optional[Node]) -> str:
        return node.text(separator=" ", strip=True) if node is not None else ""

    @staticmethod
    def _normalise_title(title: str) -> str:
        """Collapse a leading phrase that OLX immediately repeats.

        OLX renders the card heading as its own model prefix followed by the
        seller's title, and the seller's title usually starts with the model
        too — giving "Nissan Qashqai Nissan Qashqai Mild Hybrid X-Tronic". The
        repeat carries no information and crowds the feed.

        Matching is case-insensitive (sellers shout: "Nissan Qashqai NISSAN
        QASHQAI 1.3") and the *second* occurrence is the one kept, since that is
        the seller's own text.
        """
        words = title.split()
        # Longest repeat first, so "Nissan Qashqai Nissan Qashqai" collapses as
        # one 2-word phrase rather than leaving a stray word behind.
        for size in range(min(6, len(words) // 2), 0, -1):
            head = [w.casefold() for w in words[:size]]
            nxt = [w.casefold() for w in words[size : size * 2]]
            if head == nxt:
                return " ".join(words[size:])
        return title

    @staticmethod
    def _parse_price(text: str) -> tuple[Optional[float], Optional[str]]:
        """'26 980 €' -> (26980.0, 'EUR'); '24 649,52 €' -> (24649.52, 'EUR')."""
        if not text:
            return None, None

        lowered = text.lower()
        currency = None
        for token, code in _CURRENCY_BY_SYMBOL:
            if token in lowered:
                currency = code
                break

        # Keep digits, separators; drop currency words, NBSPs and thin spaces.
        cleaned = re.sub(r"[^\d,.\-]", "", text.replace(" ", "").replace(" ", ""))
        if not cleaned:
            return None, currency

        # Romanian format: '.' groups thousands, ',' is the decimal separator.
        if "," in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(".", "")

        try:
            return float(cleaned), currency
        except ValueError:
            return None, currency

    @staticmethod
    def _parse_location(text: str) -> Optional[str]:
        """'Alba Iulia - Reactualizat azi la 12:55' -> 'Alba Iulia'."""
        if not text:
            return None
        location = text.split(" - ")[0].strip()
        return location or None

    @staticmethod
    def _parse_year_and_mileage(text: str) -> tuple[Optional[int], Optional[int]]:
        """Card params read like '2025  21 700 km'.

        Prefers the adjacent year+mileage pair, because a card's text also holds
        the posting date and often a year in the title; see _PARAMS_RE.
        """
        params = _PARAMS_RE.search(text)
        if params:
            digits = re.sub(r"[^\d]", "", params.group(2))
            return int(params.group(1)), (int(digits) if digits else None)

        # No adjacent pair (e.g. mileage absent) — fall back to reading each
        # independently, still keeping the mileage digits out of the year search.
        year = None
        mileage = None

        km_match = _KM_RE.search(text)
        if km_match:
            digits = re.sub(r"[^\d]", "", km_match.group(1))
            if digits:
                try:
                    mileage = int(digits)
                except ValueError:
                    mileage = None

        # Look for the year outside the mileage figure, so '21 700 km' can't
        # donate a stray '2170'-ish match and a '2025 km' odometer can't be read
        # as a year.
        search_space = text
        if km_match:
            search_space = text[: km_match.start()] + " " + text[km_match.end():]
        year_match = _YEAR_RE.search(search_space)
        if year_match:
            year = int(year_match.group(1))

        return year, mileage
