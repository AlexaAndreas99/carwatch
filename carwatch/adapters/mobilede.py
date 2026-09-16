"""mobile.de/ro adapter (spec §7 - "Hard / best-effort").

Route: the **Romanian search app** on `www.mobile.de`, e.g.

    https://www.mobile.de/ro/vehicule/căutare.html?isSearchRequest=true&s=Car&...

This is the URL you get by searching on mobile.de/ro and copying the address
bar - exactly the workflow §3 describes. It is reachable over plain HTTP with an
honest User-Agent (HTTP 200, no challenge), and each result card is an anchor to
`detalii.html?id=<ad id>`, giving the stable `site_listing_id` diffing needs.

Two other mobile.de routes were investigated and are dead ends, recorded here so
nobody burns time rediscovering them:

  * `suchen.mobile.de/fahrzeuge/search.html` (the German search app) answers
    **HTTP 403** behind Akamai Bot Manager on first contact - obfuscated sensor
    script, `Error Reference:` page. §14 forbids working around that, so we
    don't touch it.
  * `www.mobile.de/ro/automobil/<make>/vhc:car,ms1:...` (the SEO category page)
    renders listings but exposes **no ad id at all** - bare `<article>` cards
    with `href=""` and only a positional `data-testid`. Unusable for diffing:
    listings reorder between runs, so positional ids would invent `new` and
    `delisted` events every run.

**robots.txt:** `www.mobile.de/robots.txt` disallows `/ro/*/*.html?` for
`User-agent: *`, which covers this search URL, and `/ro/*/*id=*`, which covers
the detail links. The user was shown this explicitly and accepted the risk for
personal, low-volume use, which is the call §14 leaves to them ("Scraping may be
against a site's ToS even when technically possible; the user should be aware
and accept that risk for personal use"). We stay well inside the polite envelope
that section asks for: one run a day, randomized delays, an honest User-Agent,
no attempt to defeat any anti-bot system, and a hard page cap. If mobile.de ever
starts challenging this route, the adapter reports `blocked` and stops - it will
not try to get around it.

Card structure (verified live):

    <a data-testid="base-result-listing-3-link"
       href="https://www.mobile.de/ro/vehicule/detalii.html?id=461176446&...">
      <div data-testid="...-title">           Nissan Qashqai TEKNA+ ALLRAD ...
      <div data-testid="...-price-label">     31.480 EUR
      <div data-testid="...-listing-details-attributes">
            Fără accidente • Prima înmatriculare 05/2025 • 1.609 km
            • 116 kW (158 CP) • Benzină
      <div data-testid="...-seller-info">     DEALER NAME  DE-15517 Fürstenwalde
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional
from urllib.parse import parse_qsl, urlparse

from selectolax.parser import HTMLParser, Node

from carwatch.adapters.base import AdapterError, BlockedError, RawListing, report_page
from carwatch.adapters.http import PoliteClient, with_page_param
from carwatch.adapters.images import image_url_from, in_block

log = logging.getLogger(__name__)

# 173 results at ~20/page is 9 pages; the cap is a safety net, not a target.
MAX_PAGES = 30

PAGE_PARAM = "pageNumber"

# Result cards are anchors straight to the ad. Sponsored cards use
# `top-result-listing-N-link`, ordinary ones `base-result-listing-N-link`;
# matching on the href keeps both without depending on that naming.
CARD_SELECTOR = 'a[href*="detalii.html"]'

_ID_RE = re.compile(r"[?&]id=(\d+)")

# "Prima înmatriculare 05/2025" (first registration) is the model year we want -
# NOT any other 4-digit number in the card, of which there are several.
_FIRST_REG_RE = re.compile(
    r"(?:prima\s+înmatriculare|prima\s+inmatriculare|erstzulassung)\D{0,4}"
    r"(\d{1,2})[/.](\d{4})",
    re.IGNORECASE,
)
_YEAR_ONLY_RE = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")
# "1.609 km" / "163.500 km" - German grouping, dot as thousands separator.
#
# The lookbehind is load-bearing. The same attribute line also carries fuel
# consumption - "8,6 l/100km (comb.)" - and without it that "100km" parses as
# the odometer. It silently gave every brand-new car ("Masina noua", which
# states no mileage at all) a plausible-looking 100 km: 21 of 173 listings.
# A wrong number that looks right is worse than no number.
_KM_RE = re.compile(r"(?<![/\d])(\d{1,3}(?:[.\s]\d{3})*|\d+)\s*km\b", re.IGNORECASE)
_POWER_RE = re.compile(r"(\d{2,4})\s*kW", re.IGNORECASE)

# Fuel words as mobile.de/ro renders them, mapped to what the dashboard shows.
_FUEL_WORDS = {
    "benzină": "Benzină",
    "benzina": "Benzină",
    "diesel": "Diesel",
    "hibrid": "Hibrid",
    "electric": "Electric",
    "gpl": "GPL",
    "gnc": "GNC",
    "hidrogen": "Hidrogen",
}
# "Masina noua" / "Vehicul nou" - an unregistered car. These listings carry no
# first registration and no odometer, so without capturing this the dashboard
# shows two empty cells and no reason for them.
_NEW_VEHICLE_WORDS = ("mașină nouă", "masina noua", "vehicul nou", "neuwagen")

_GEARBOX_WORDS = {
    "automat": "Automată",
    "automată": "Automată",
    "manual": "Manuală",
    "manuală": "Manuală",
    "semi-automat": "Semi-automată",
}

# A postcode-prefixed town, e.g. "DE-15517 Fürstenwalde", in the seller block.
_LOCATION_RE = re.compile(r"\b([A-Z]{2}-[\w\d]+\s+[^\n•]{2,40})")

_CHALLENGE_HINTS = ("zugriff verweigert", "access denied", "error reference")


class MobileDeAdapter:
    site_name = "mobilede"

    def __init__(self, settings: Any = None) -> None:
        self.user_agent = getattr(settings, "user_agent", "CarWatch/1.0 (personal use)")
        self.delay_range = tuple(getattr(settings, "request_delay_seconds", (1.5, 4.0)))

    # ---------------------------------------------------------------- fetching

    def fetch_listings(
        self, search_url: str, max_pages: Optional[int] = None
    ) -> list[RawListing]:
        listings: list[RawListing] = []
        seen_ids: set[str] = set()

        with PoliteClient(self.user_agent, self.delay_range) as client:
            for page in range(1, min(MAX_PAGES, max_pages or MAX_PAGES) + 1):
                report_page(self, page)
                url = with_page_param(search_url, page, param=PAGE_PARAM)
                html = client.get_html(url)

                # The German host serves an Akamai challenge; if this route ever
                # starts doing the same, stop rather than treat it as 0 results.
                lowered = html[:4000].lower()
                if any(hint in lowered for hint in _CHALLENGE_HINTS):
                    raise BlockedError(
                        f"mobile.de served an access-denied page for {url}. "
                        f"Not attempting to work around it (spec §14)."
                    )

                cards = self._parse_page(html, url)
                if not cards:
                    break

                new_on_page = 0
                for listing in cards:
                    if listing.site_listing_id in seen_ids:
                        continue
                    seen_ids.add(listing.site_listing_id)
                    listings.append(listing)
                    new_on_page += 1

                # Past the last page mobile.de re-serves earlier results rather
                # than an empty page, so repeats mean "done".
                if new_on_page == 0:
                    break

        return listings

    # ----------------------------------------------------------------- parsing

    def _parse_page(self, html: str, url: str) -> list[RawListing]:
        tree = HTMLParser(html)
        anchors = tree.css(CARD_SELECTOR)

        if not anchors and not self._looks_like_results_page(tree):
            raise AdapterError(
                f"No result cards and no search-results markup at {url} - "
                f"mobile.de's page structure has probably changed."
            )

        out: list[RawListing] = []
        for anchor in anchors:
            listing = self._parse_card(anchor)
            if listing is not None:
                out.append(listing)
        return out

    @staticmethod
    def _looks_like_results_page(tree: HTMLParser) -> bool:
        return bool(
            tree.css_first('[data-testid="result-list"]')
            or tree.css_first('[data-testid="result-list-header"]')
            or tree.css_first('[data-testid="srp"]')
        )

    def _parse_card(self, anchor: Node) -> Optional[RawListing]:
        href = (anchor.attributes.get("href") or "").strip()
        match = _ID_RE.search(href)
        if not match:
            return None
        site_id = match.group(1)

        attrs = self._sub_text(anchor, "listing-details-attributes") or self._sub_text(
            anchor, "listing-details"
        )
        price_text = self._sub_text(anchor, "price-label") or self._sub_text(
            anchor, "main-price-label"
        )
        price, currency = self._parse_price(price_text)

        year, month = self._parse_first_registration(attrs)

        return RawListing(
            site_listing_id=site_id,
            url=self._canonical_url(href),
            title=self._parse_title(anchor),
            price=price,
            currency=currency,
            year=year,
            mileage_km=self._parse_mileage(attrs),
            fuel=self._match_word(attrs, _FUEL_WORDS),
            gearbox=self._match_word(attrs, _GEARBOX_WORDS),
            location=self._parse_location(self._sub_text(anchor, "seller-info")),
            condition=self._parse_condition(attrs),
            image_url=self._parse_image(anchor),
            raw={
                "attributes": attrs,
                "price_text": price_text,
                "seller": self._sub_text(anchor, "seller-info"),
                "first_registration_month": month,
            },
        )

    # -------------------------------------------------------------- field bits

    @staticmethod
    def _parse_condition(attributes: str) -> Optional[str]:
        """"new" for an unregistered vehicle, else None.

        Diacritics are unreliable in scraped text, so the ASCII spelling is
        matched too. Anything else - "Vehicul demonstrativ", "Fara accidente" -
        is deliberately not mapped: those cars do have a registration year, so
        there is no empty cell to explain.
        """
        lowered = (attributes or "").casefold()
        return "new" if any(w in lowered for w in _NEW_VEHICLE_WORDS) else None

    @staticmethod
    def _parse_image(anchor: Node) -> Optional[str]:
        """The car's photo, hot-linked from mobile.de's CDN (img.classistatic.de).

        A result anchor carries the car's photos *and*, in its seller block, the
        dealer's logo - both plain `<img>`, distinguishable only by position.
        Today the photos come first, so "the first img" would usually work; it
        would also silently start showing dealer logos the day mobile.de moves
        the seller block up. Excluding the seller block outright is the version
        that stays correct.
        """
        for img in anchor.css("img"):
            if in_block(img, "seller-info"):
                continue
            url = image_url_from(img)
            if url:
                return url
        return None

    @staticmethod
    def _sub_text(anchor: Node, suffix: str) -> str:
        """Text of the descendant whose data-testid ends with `suffix`.

        The ids are prefixed per card ("base-result-listing-3-price-label"), so
        matching on the suffix keeps this independent of card position.
        """
        for node in anchor.css("[data-testid]"):
            testid = node.attributes.get("data-testid") or ""
            if testid.endswith(suffix):
                return node.text(separator=" ", strip=True)
        return ""

    @staticmethod
    def _canonical_url(href: str) -> str:
        """Strip the search-session junk so the stored URL is stable.

        The card href carries `searchId`/`refId` UUIDs that change every run, plus
        a copy of every filter. Keeping them would make the URL churn in the DB
        and leak one search's parameters into a listing that outlives it.
        """
        parsed = urlparse(href)
        ad_id = dict(parse_qsl(parsed.query)).get("id")
        if not ad_id:
            return href
        return f"https://www.mobile.de/ro/vehicule/detalii.html?id={ad_id}"

    def _parse_title(self, anchor: Node) -> str:
        """Model line plus the variant line, without the "Sponsorizat" badge."""
        title_node = None
        for node in anchor.css("[data-testid]"):
            if (node.attributes.get("data-testid") or "").endswith("-title"):
                title_node = node
                break
        if title_node is None:
            text = anchor.text(separator=" ", strip=True)
            return (text.split("  ")[0] or "(untitled)")[:200]

        # Drop the sponsored badge, which is glued to the model name
        # ("SponsorizatNissan Qashqai") when read as plain text.
        for badge in title_node.css('[data-testid$="sponsored-badge"]'):
            badge.decompose()

        title = " ".join(title_node.text(separator=" ", strip=True).split())
        return title or "(untitled)"

    @staticmethod
    def _parse_price(text: str) -> tuple[Optional[float], Optional[str]]:
        """'33.990 EUR' -> (33990.0, 'EUR').

        Only the EUR figure is read. Cards also show a RON conversion
        ("178.549 RON²"), but it is derived from the EUR price at the day's rate,
        so tracking it would record exchange-rate drift as price changes.
        """
        if not text:
            return None, None

        match = re.search(r"([\d.\s]+(?:,\d+)?)\s*(EUR|€)", text, re.IGNORECASE)
        if not match:
            return None, None

        cleaned = match.group(1).replace(" ", "").replace(" ", "").strip()
        # German/Romanian grouping: '.' thousands, ',' decimal.
        cleaned = cleaned.replace(".", "").replace(",", ".")
        try:
            return float(cleaned), "EUR"
        except ValueError:
            return None, "EUR"

    @staticmethod
    def _parse_first_registration(text: str) -> tuple[Optional[int], Optional[str]]:
        """Model year from 'Prima înmatriculare 05/2025'.

        Anchored on the label rather than taking any 4-digit number: a card also
        contains power figures, postcodes and model designations like "MY24".
        """
        if not text:
            return None, None

        match = _FIRST_REG_RE.search(text)
        if match:
            return int(match.group(2)), f"{match.group(1).zfill(2)}/{match.group(2)}"

        # "Vehicul nou" listings may carry no registration date at all; fall back
        # to a bare year only if one is present.
        fallback = _YEAR_ONLY_RE.search(text)
        return (int(fallback.group(1)), None) if fallback else (None, None)

    @staticmethod
    def _parse_mileage(text: str) -> Optional[int]:
        if not text:
            return None
        match = _KM_RE.search(text)
        if not match:
            return None
        digits = re.sub(r"[^\d]", "", match.group(1))
        return int(digits) if digits else None

    @staticmethod
    def _match_word(text: str, table: dict[str, str]) -> Optional[str]:
        """Find a fuel/gearbox value among the card's bullet-separated specs.

        Matches only where a segment *starts* with the word, rather than
        searching the whole string. A plain substring search picks "electric"
        out of unrelated spec text and mislabels a petrol car as electric -
        a "1.3 DIG-T" was coming back as Electric before this.
        """
        if not text:
            return None

        segments = [s.strip().lower() for s in text.split("•")]
        # Longest key first so "semi-automat" wins over "automat".
        for word in sorted(table, key=len, reverse=True):
            for segment in segments:
                if segment == word or segment.startswith(word + " "):
                    return table[word]
        return None

    @staticmethod
    def _parse_location(seller_text: str) -> Optional[str]:
        """'NISSAN KRUCK (...) DE-15517 Fürstenwalde 4.8 stele (24)' -> the town."""
        if not seller_text:
            return None
        match = _LOCATION_RE.search(seller_text)
        if not match:
            return None
        location = " ".join(match.group(1).split())
        # Trim a trailing seller rating that runs into the town name. The score
        # is not always decimal - "5 stele ( 30 )" as well as "4.8 stele (24)".
        location = re.split(r"\s+\d(?:[.,]\d+)?\s*stele", location)[0]
        return location.strip(" ,(") or None
