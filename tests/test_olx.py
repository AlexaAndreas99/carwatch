"""olx.ro adapter tests.

The main fixture is real markup captured from a live olx.ro search page, so the
parser is exercised against the actual DOM rather than something idealised.
"""

from pathlib import Path

import pytest
from selectolax.parser import HTMLParser

from carwatch.adapters.base import BlockedError
from carwatch.adapters.olx import OlxAdapter

FIXTURE = Path(__file__).parent / "fixtures" / "olx_search.html"


@pytest.fixture
def adapter():
    return OlxAdapter(None)


@pytest.fixture
def page_html():
    return FIXTURE.read_text(encoding="utf-8")


def install(monkeypatch, adapter, pages):
    """Replace the fetch strategy so no network (or browser) is used."""
    requested = []

    def fake_make_fetcher(search_url):
        def fetch(url):
            requested.append(url)
            if url not in pages:
                raise AssertionError(f"unexpected URL: {url}")
            result = pages[url]
            if isinstance(result, Exception):
                raise result
            return result

        return fetch

    monkeypatch.setattr(adapter, "_make_fetcher", fake_make_fetcher)
    return requested


EMPTY_PAGE = (
    '<html><body><div data-testid="listing-filters"></div>'
    '<p data-testid="total-count">Am gasit 0 rezultate</p>'
    '<div data-testid="listing-grid"></div></body></html>'
)


# ------------------------------------------------------------------ parsing


def test_parses_real_cards(adapter, page_html, monkeypatch):
    base = "https://www.olx.ro/auto-masini-moto-ambarcatiuni/autoturisme/nissan/"
    install(monkeypatch, adapter, {base: page_html, base + "?page=2": EMPTY_PAGE})

    listings = adapter.fetch_listings(base)

    assert len(listings) == 2
    a, b = listings

    assert a.site_listing_id == "305871621"
    assert a.price == 26980.0
    assert a.currency == "EUR"
    assert a.year == 2025
    assert a.mileage_km == 21700
    assert a.location == "Alba Iulia"
    assert "An2025 Facelift" in a.title
    assert a.url.startswith("https://www.autovit.ro/anunt/")

    # Decimal comma, and a price with a trailing "negotiable" label.
    assert b.site_listing_id == "307984601"
    assert b.price == 24649.52
    assert b.currency == "EUR"
    assert b.mileage_km == 3000
    assert b.location == "Strejnicu"


def test_ids_are_stable_and_unique(adapter, page_html, monkeypatch):
    base = "https://www.olx.ro/x"
    install(monkeypatch, adapter, {base: page_html, base + "?page=2": EMPTY_PAGE})
    ids = [x.site_listing_id for x in adapter.fetch_listings(base)]
    assert ids == ["305871621", "307984601"]
    assert len(set(ids)) == len(ids)


# ------------------------------------------------------------- price parsing


@pytest.mark.parametrize(
    "text,expected",
    [
        ("26 980 €", (26980.0, "EUR")),
        ("24 649,52 €", (24649.52, "EUR")),
        ("115.000 lei", (115000.0, "RON")),
        ("9 500 EUR", (9500.0, "EUR")),
        ("1.234.567 lei", (1234567.0, "RON")),
        ("", (None, None)),
        ("Preț la cerere", (None, None)),
    ],
)
def test_price_parsing(text, expected):
    assert OlxAdapter._parse_price(text) == expected


def test_price_handles_non_breaking_spaces():
    # OLX renders thousands separators as NBSP / narrow NBSP.
    assert OlxAdapter._parse_price("26 980 €") == (26980.0, "EUR")


# ---------------------------------------------------- year / mileage parsing


@pytest.mark.parametrize(
    "text,year,km",
    [
        ("2025  21 700 km", 2025, 21700),
        ("2018 165.000 km", 2018, 165000),
        ("2024 1 km", 2024, 1),
        ("Nissan Qashqai 2020", 2020, None),
    ],
)
def test_year_and_mileage(text, year, km):
    assert OlxAdapter._parse_year_and_mileage(text) == (year, km)


def test_posting_date_is_not_mistaken_for_the_model_year():
    """Regression, caught against live data: a card's flattened text contains the
    posting date and often a year in the title, so taking the first year found
    anywhere returned 2026 for 2025 cars."""
    text = (
        "Nissan Qashqai Primul proprietar, masina de reprezentanta 25 990 € Utilizat "
        "Bucuresti, Sectorul 2 - Reactualizat la 31 august 2026 2025  14 108 km"
    )
    assert OlxAdapter._parse_year_and_mileage(text) == (2025, 14108)


def test_year_in_title_does_not_win_over_params_line():
    text = "Nissan Qashqai 30.12.2025 Garantie 24 649,52 € Strejnicu - 01 septembrie 2026 2024  3 000 km"
    assert OlxAdapter._parse_year_and_mileage(text) == (2024, 3000)


def test_mileage_digits_are_not_mistaken_for_a_year():
    """'2 025 km' is an odometer, not a model year — the mileage figure is
    excluded from the year search."""
    year, km = OlxAdapter._parse_year_and_mileage("2 025 km")
    assert km == 2025
    assert year is None


# ------------------------------------------------------------------ location


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Alba Iulia - Reactualizat azi la 12:55", "Alba Iulia"),
        ("Bucuresti, Sectorul 2 - Reactualizat la 31 august 2026", "Bucuresti, Sectorul 2"),
        ("", None),
    ],
)
def test_location_parsing(text, expected):
    assert OlxAdapter._parse_location(text) == expected


# --------------------------------------------------------------- pagination


def test_paginates_until_no_new_cards(adapter, page_html, monkeypatch):
    base = "https://www.olx.ro/x"
    requested = install(
        monkeypatch,
        adapter,
        {base: page_html, base + "?page=2": page_html, base + "?page=3": EMPTY_PAGE},
    )

    listings = adapter.fetch_listings(base)

    # Page 2 repeats page 1 (OLX serves the last page for out-of-range numbers),
    # so we stop there rather than looping to MAX_PAGES.
    assert len(listings) == 2
    assert requested == [base, base + "?page=2"]


def test_empty_results_page_is_not_an_error(adapter, monkeypatch):
    base = "https://www.olx.ro/x"
    install(monkeypatch, adapter, {base: EMPTY_PAGE})
    assert adapter.fetch_listings(base) == []


def test_card_without_id_is_skipped(adapter, monkeypatch):
    html = (
        '<div data-testid="listing-grid">'
        '<div data-cy="l-card"><a data-testid="card-title-link" href="/x"><h4>No id</h4></a></div>'
        '<div data-cy="l-card" id="123"><a data-testid="card-title-link" href="/y"><h4>Fine</h4></a></div>'
        "</div>"
    )
    base = "https://www.olx.ro/x"
    install(monkeypatch, adapter, {base: html, base + "?page=2": EMPTY_PAGE})

    listings = adapter.fetch_listings(base)
    assert [x.site_listing_id for x in listings] == ["123"]
    assert listings[0].url == "https://www.olx.ro/y"  # relative href absolutised


def test_blocked_propagates(adapter, monkeypatch):
    base = "https://www.olx.ro/x"
    install(monkeypatch, adapter, {base: BlockedError("CloudFront 403")})
    with pytest.raises(BlockedError):
        adapter.fetch_listings(base)


# ------------------------------------------------------- browser escalation


def test_missing_playwright_reports_blocked_not_crash(adapter, monkeypatch):
    """Without Playwright the run must degrade to `blocked` with instructions.

    `carwatch.adapters.browser` imports fine on a machine with no Playwright —
    it guards the import internally — so the failure actually surfaces from
    `get_html` as BrowserUnavailableError. If that escapes, the collector
    records status='error' with a raw traceback instead of a clean 'blocked'.
    """
    from carwatch.adapters.browser import BrowserUnavailableError

    def boom(self, url):
        raise BrowserUnavailableError("Playwright is not installed")

    monkeypatch.setattr(
        "carwatch.adapters.browser.BrowserFetcher.get_html", boom
    )

    fetch = adapter._open_browser()
    with pytest.raises(BlockedError, match="[Pp]laywright"):
        fetch("https://www.olx.ro/x")


def test_http_block_escalates_to_browser(adapter, monkeypatch):
    """Plain HTTP is tried first; a block switches the whole run to a browser."""
    calls = {"http": 0, "browser": 0}

    def http_get(self, url):
        calls["http"] += 1
        raise BlockedError("CloudFront 403")

    def fake_browser(self):
        def get(url):
            calls["browser"] += 1
            return EMPTY_PAGE

        return get

    monkeypatch.setattr("carwatch.adapters.http.PoliteClient.get_html", http_get)
    monkeypatch.setattr(OlxAdapter, "_open_browser", fake_browser)

    adapter.fetch_listings("https://www.olx.ro/x")

    assert calls["http"] == 1          # tried once, not once per page
    assert calls["browser"] == 1
    assert adapter.used_browser is True


# ------------------------------------------------------------ title cleanup


@pytest.mark.parametrize(
    "raw_title,expected",
    [
        # OLX prefixes its own model name, and the seller repeats it.
        (
            "Nissan Qashqai Nissan Qashqai Mild Hybrid X-Tronic N-Connecta",
            "Nissan Qashqai Mild Hybrid X-Tronic N-Connecta",
        ),
        # Case-insensitive, and the seller's own casing is what survives.
        (
            "Nissan Qashqai NISSAN QASHQAI 1.3 l 156CP X MHEV",
            "NISSAN QASHQAI 1.3 l 156CP X MHEV",
        ),
        (
            "Nissan Qashqai Nissan Qashqai 1.3 Mild Hybrid 158 CP, Tekna, 4x4",
            "Nissan Qashqai 1.3 Mild Hybrid 158 CP, Tekna, 4x4",
        ),
        # Nothing repeated — left exactly as-is.
        (
            "Nissan Qashqai An2025 Facelift Automat Virtual Cockpit",
            "Nissan Qashqai An2025 Facelift Automat Virtual Cockpit",
        ),
        # The whole title is the repeated phrase.
        ("Nissan Qashqai Nissan Qashqai", "Nissan Qashqai"),
        # Too short to contain a repeat.
        ("Nissan Qashqai", "Nissan Qashqai"),
        ("", ""),
    ],
)
def test_title_repeat_is_collapsed(raw_title, expected):
    assert OlxAdapter._normalise_title(raw_title) == expected


def test_title_collapse_prefers_the_longest_repeat():
    """A 1-word match would leave 'Qashqai Nissan Qashqai ...' behind."""
    assert (
        OlxAdapter._normalise_title("Nissan Qashqai Nissan Qashqai Tekna")
        == "Nissan Qashqai Tekna"
    )


# --------------------------------------------------------------- card photos

CARD_WITH_PHOTO = """
<div data-cy="l-card" id="305871621">
  <div><a href="/x.html" data-testid="card-title-link"><div><div>
    <img src="https://frankfurt.apollo.olxcdn.com/v1/files/abc/image;s=216x152;q=50"
         sizes="216px" alt="Nissan Qashqai">
  </div></div></a></div>
  <div data-testid="ad-card-title"><a href="/x.html" data-testid="card-title-link">
    <h4>Nissan Qashqai</h4></a>
    <p data-testid="ad-price">26 980 &euro;</p></div>
  <p data-testid="location-date">Alba Iulia - azi la 12:55</p>
  <span>2025  21 700 km</span>
</div>
"""


def _card(html):
    return HTMLParser(html).css_first('[data-cy="l-card"]')


def test_parses_the_card_photo():
    url = OlxAdapter._parse_image(_card(CARD_WITH_PHOTO))
    assert url == "https://frankfurt.apollo.olxcdn.com/v1/files/abc/image;s=644x461;q=80"


def test_lazy_loaded_card_photo_is_read_from_data_src():
    """Below the fold OLX parks the real URL in data-src and puts a placeholder
    in src, so reading src alone would give a grid of grey squares."""
    html = CARD_WITH_PHOTO.replace(
        'src="https://frankfurt.apollo.olxcdn.com/v1/files/abc/image;s=216x152;q=50"',
        'src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" '
        'data-src="https://frankfurt.apollo.olxcdn.com/v1/files/abc/image;s=216x152;q=50"',
    )
    assert OlxAdapter._parse_image(_card(html)) == (
        "https://frankfurt.apollo.olxcdn.com/v1/files/abc/image;s=644x461;q=80"
    )


def test_card_without_a_usable_photo_is_none():
    """The captured fixture has its images stripped — exactly the shape a card
    with no photo takes, and it must not break parsing."""
    assert OlxAdapter._parse_image(_card(CARD_WITH_PHOTO.replace('src="h', 'src="'))) is None


def test_photoless_fixture_cards_still_parse(adapter, page_html, monkeypatch):
    base = "https://www.olx.ro/x"
    install(monkeypatch, adapter, {base: page_html, base + "?page=2": EMPTY_PAGE})

    listings = adapter.fetch_listings(base)

    assert [x.image_url for x in listings] == [None, None]
    assert [x.site_listing_id for x in listings] == ["305871621", "307984601"]


def test_image_url_reaches_the_raw_listing(adapter, monkeypatch):
    page = (
        '<html><body><div data-testid="listing-grid">'
        + CARD_WITH_PHOTO
        + "</div></body></html>"
    )
    base = "https://www.olx.ro/x"
    install(monkeypatch, adapter, {base: page, base + "?page=2": EMPTY_PAGE})

    (listing,) = adapter.fetch_listings(base)

    assert listing.image_url.startswith("https://frankfurt.apollo.olxcdn.com/")


def test_card_photo_is_requested_at_card_size():
    """OLX's list asks its CDN for 216px thumbnails; a 268px card at 2x needs
    more. The size is a URL parameter the same CDN honours."""
    assert OlxAdapter._parse_image(_card(CARD_WITH_PHOTO)) == (
        "https://frankfurt.apollo.olxcdn.com/v1/files/abc/image;s=644x461;q=80"
    )


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://frankfurt.apollo.olxcdn.com:443/v1/files/a-RO/image;s=216x152;q=50",
            "https://frankfurt.apollo.olxcdn.com:443/v1/files/a-RO/image;s=644x461;q=80",
        ),
        (
            "https://frankfurt.apollo.olxcdn.com/v1/files/a-RO/image;s=320x240",
            "https://frankfurt.apollo.olxcdn.com/v1/files/a-RO/image;s=644x461;q=80",
        ),
        # No size directive: the CDN already serves the original, leave it be.
        (
            "https://frankfurt.apollo.olxcdn.com/v1/files/a-RO/image",
            "https://frankfurt.apollo.olxcdn.com/v1/files/a-RO/image",
        ),
        # Another host entirely — never rewrite a URL we don't understand.
        ("https://example.com/photo.jpg;s=1x1", "https://example.com/photo.jpg;s=1x1"),
    ],
)
def test_cdn_size_rewriting(url, expected):
    assert OlxAdapter._card_sized(url) == expected


def test_browser_fallback_scrolls_for_lazy_images(adapter, monkeypatch):
    """Regression: without a scroll pass olx yielded photos for 10 of 47 cards."""
    captured = {}

    class FakeFetcher:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def get_html(self, url):
            return EMPTY_PAGE

    monkeypatch.setattr("carwatch.adapters.browser.BrowserFetcher", FakeFetcher)
    adapter._open_browser()

    assert captured["scroll_to_bottom"] is True


def test_olx_own_no_thumbnail_placeholder_is_not_treated_as_a_photo():
    """Measured on live data: 17 of 47 cards carry OLX's own placeholder SVG
    instead of a photo. Storing that path would put a broken image on the card —
    and these are syndicated autovit ads, so the merged card gets autovit's
    photo instead."""
    html = CARD_WITH_PHOTO.replace(
        'src="https://frankfurt.apollo.olxcdn.com/v1/files/abc/image;s=216x152;q=50"',
        'src="/app/static/media/no_thumbnail.15f456ec5.svg"',
    )
    assert OlxAdapter._parse_image(_card(html)) is None
