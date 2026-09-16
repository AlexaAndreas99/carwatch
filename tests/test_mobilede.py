"""mobile.de/ro adapter tests.

The page fixture is real markup captured from a live Romanian search, so parsing
is exercised against the actual DOM rather than something idealised.
"""

from pathlib import Path

import pytest
from selectolax.parser import HTMLParser

from carwatch.adapters.base import BlockedError, get_adapter
from carwatch.adapters.mobilede import MobileDeAdapter

FIXTURE = Path(__file__).parent / "fixtures" / "mobilede_search.html"

BASE = (
    "https://www.mobile.de/ro/vehicule/c%C4%83utare.html"
    "?isSearchRequest=true&s=Car&vc=Car&fr=2025"
)

EMPTY_PAGE = (
    '<html><body><div data-testid="srp">'
    '<div data-testid="result-list-header">0 rezultate</div>'
    '<div data-testid="result-list"></div></div></body></html>'
)


@pytest.fixture
def adapter():
    return MobileDeAdapter(None)


@pytest.fixture
def page_html():
    return FIXTURE.read_text(encoding="utf-8")


def install(monkeypatch, pages):
    requested = []

    def fake_get_html(self, url):
        requested.append(url)
        if url not in pages:
            raise AssertionError(f"unexpected URL: {url}")
        result = pages[url]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr("carwatch.adapters.http.PoliteClient.get_html", fake_get_html)
    return requested


# ------------------------------------------------------------------ parsing


def test_parses_real_cards(adapter, page_html, monkeypatch):
    install(monkeypatch, {BASE: page_html, BASE + "&pageNumber=2": EMPTY_PAGE})

    listings = adapter.fetch_listings(BASE)

    assert len(listings) == 2
    for x in listings:
        assert x.site_listing_id.isdigit()
        assert x.price and x.price > 1000
        assert x.currency == "EUR"
        assert x.url.startswith("https://www.mobile.de/ro/vehicule/detalii.html?id=")
        assert "Qashqai" in x.title


def test_ids_are_unique_and_stable(adapter, page_html, monkeypatch):
    install(monkeypatch, {BASE: page_html, BASE + "&pageNumber=2": EMPTY_PAGE})
    ids = [x.site_listing_id for x in adapter.fetch_listings(BASE)]
    assert len(set(ids)) == len(ids)


def test_canonical_url_drops_search_session_junk():
    """The card href carries searchId/refId UUIDs that change every run, plus a
    copy of every filter. Storing those would churn the URL in the DB."""
    href = (
        "https://www.mobile.de/ro/vehicule/detalii.html?id=461256563&vc=Car"
        "&fr=2025&ms=18700%3B47&ref=srp&searchId=a9f6bd6f-91e8&refId=a9f6bd6f"
    )
    assert (
        MobileDeAdapter._canonical_url(href)
        == "https://www.mobile.de/ro/vehicule/detalii.html?id=461256563"
    )


def test_canonical_url_passes_through_when_no_id():
    assert MobileDeAdapter._canonical_url("https://x/y") == "https://x/y"


# -------------------------------------------------------------- price parsing


@pytest.mark.parametrize(
    "text,expected",
    [
        ("33.990 EUR", (33990.0, "EUR")),
        ("31.480 EUR ¹", (31480.0, "EUR")),
        ("9.999 EUR", (9999.0, "EUR")),
        ("1.234.567 EUR", (1234567.0, "EUR")),
        ("", (None, None)),
        ("Pret la cerere", (None, None)),
    ],
)
def test_price_parsing(text, expected):
    assert MobileDeAdapter._parse_price(text) == expected


def test_price_ignores_the_ron_conversion():
    """Cards show a RON figure derived from EUR at the day's rate; tracking it
    would record exchange-rate drift as price changes."""
    price, currency = MobileDeAdapter._parse_price(
        "28.480 EUR ¹ 19% TVA 149.000 RON²"
    )
    assert (price, currency) == (28480.0, "EUR")


# ---------------------------------------------------- registration / mileage

ATTRS = (
    "Fără accidente • Înmatriculare temporară "
    "• Prima înmatriculare 04/2026 "
    "• 5 km • 116 kW (158 CP) • Benzină 6,9 l/100km (comb.)"
)


def test_year_comes_from_first_registration():
    """Anchored on the label: a card also holds power figures, postcodes and
    model designations like 'MY24' that a bare year regex would grab."""
    assert MobileDeAdapter._parse_first_registration(ATTRS) == (2026, "04/2026")


def test_year_absent_for_an_unregistered_new_car():
    attrs = "Fără accidente • Vehicul nou • 100 km • 116 kW"
    assert MobileDeAdapter._parse_first_registration(attrs) == (None, None)


def test_model_designation_is_not_read_as_a_year():
    assert MobileDeAdapter._parse_first_registration("QASHQAI MY24 1.3") == (None, None)


@pytest.mark.parametrize(
    "text,expected",
    [
        (ATTRS, 5),
        ("• 18.620 km •", 18620),
        ("• 163.500 km •", 163500),
        ("no km here", None),
    ],
)
def test_mileage_parsing(text, expected):
    assert MobileDeAdapter._parse_mileage(text) == expected


# ----------------------------------------------------------- fuel / gearbox


def test_fuel_from_bullet_segment():
    assert MobileDeAdapter._match_word(ATTRS, {"benzină": "Benzină"}) == "Benzină"


def test_substring_does_not_mislabel_fuel():
    """Regression from live data: a '1.3 DIG-T' petrol car came back as Electric
    because 'electric' appears as a substring elsewhere in the specs."""
    attrs = (
        "Fără accidente • Scaune electrice • 10 km "
        "• 116 kW • Benzină 6,3 l/100km"
    )
    table = {"electric": "Electric", "benzină": "Benzină"}
    assert MobileDeAdapter._match_word(attrs, table) == "Benzină"


def test_hybrid_is_detected():
    attrs = "Fără accidente • 10 km • 116 kW • Hibrid 5,8 l/100km"
    assert MobileDeAdapter._match_word(attrs, {"hibrid": "Hibrid"}) == "Hibrid"


# ------------------------------------------------------------------ location


@pytest.mark.parametrize(
    "seller,expected",
    [
        ("Kück Automobile GmbH DE-52353 Düren 4.8 stele ( 1697 )", "DE-52353 Düren"),
        # A whole-number rating, which an earlier decimal-only trim let through.
        ("NISSAN KRUCK DE-13437 Berlin 5 stele ( 30 )", "DE-13437 Berlin"),
        ("Autohaus DE-27729 Wallhöfen bei Bremen", "DE-27729 Wallhöfen bei Bremen"),
        ("", None),
    ],
)
def test_location_parsing(seller, expected):
    assert MobileDeAdapter._parse_location(seller) == expected


# --------------------------------------------------------------- pagination


def test_paginates_with_page_number(adapter, page_html, monkeypatch):
    requested = install(
        monkeypatch,
        {
            BASE: page_html,
            BASE + "&pageNumber=2": page_html,
            BASE + "&pageNumber=3": EMPTY_PAGE,
        },
    )

    listings = adapter.fetch_listings(BASE)

    # Page 2 repeats page 1, so we stop there rather than walking to MAX_PAGES.
    assert len(listings) == 2
    assert requested == [BASE, BASE + "&pageNumber=2"]


def test_empty_results_page_is_not_an_error(adapter, monkeypatch):
    install(monkeypatch, {BASE: EMPTY_PAGE})
    assert adapter.fetch_listings(BASE) == []


# ------------------------------------------------------------------ blocking


def test_access_denied_page_is_blocked_not_empty(adapter, monkeypatch):
    """The German host serves Akamai challenges. If this route ever does the
    same, it must read as `blocked`, never as zero results - otherwise the diff
    would delist every listing."""
    wall = (
        "<html><head><title>Zugriff verweigert / Access denied</title></head>"
        "<body>Error Reference: 0.1c045368</body></html>"
    )
    install(monkeypatch, {BASE: wall})
    with pytest.raises(BlockedError):
        adapter.fetch_listings(BASE)


def test_blocked_error_propagates(adapter, monkeypatch):
    install(monkeypatch, {BASE: BlockedError("403")})
    with pytest.raises(BlockedError):
        adapter.fetch_listings(BASE)


def test_registered_in_the_adapter_factory():
    assert get_adapter("mobilede", None).site_name == "mobilede"


# --------------------------------------------------------------- card photos

ANCHOR_WITH_PHOTO = """
<a data-testid="base-result-listing-1-link"
   href="https://www.mobile.de/ro/vehicule/detalii.html?id=461256563&s=Car">
  <div><img src="https://img.classistatic.de/api/v1/mo-prod/images/ab/car.jpg"></div>
  <div data-testid="base-result-listing-1-title"><h2>Nissan Qashqai</h2></div>
  <div data-testid="main-price-label"><span data-testid="price-label">28.480 EUR</span></div>
  <section><div data-testid="listing-details-attributes">
    Prima înmatriculare 03/2026 • 10 km • Benzină</div></section>
  <div data-testid="base-result-listing-1-seller-info">
    <div><img src="https://img.classistatic.de/api/v1/mo-prod/images/dealer-logo.png"></div>
    <span>CC Automobile</span><span>DE-52349 Düren</span>
  </div>
</a>
"""


def _anchor(html):
    return HTMLParser(html).css_first('a[href*="detalii.html"]')


def test_parses_the_card_photo():
    url = MobileDeAdapter._parse_image(_anchor(ANCHOR_WITH_PHOTO))
    assert url == "https://img.classistatic.de/api/v1/mo-prod/images/ab/car.jpg"


def test_dealer_logo_is_never_used_as_the_car_photo():
    """The car's photos and the dealer's logo are both plain <img> in the same
    anchor, so only position tells them apart. With the seller block moved
    above the gallery — the layout change this guards against — "take the first
    img" would put a dealer logo on every card."""
    seller_first = """
    <a data-testid="base-result-listing-1-link"
       href="https://www.mobile.de/ro/vehicule/detalii.html?id=461256563">
      <div data-testid="base-result-listing-1-seller-info">
        <div><img src="https://img.classistatic.de/dealer-logo.png"></div>
        <span>CC Automobile</span><span>DE-52349 Düren</span>
      </div>
      <div><img src="https://img.classistatic.de/car.jpg"></div>
    </a>
    """
    assert MobileDeAdapter._parse_image(_anchor(seller_first)) == (
        "https://img.classistatic.de/car.jpg"
    )


def test_anchor_with_only_a_dealer_logo_has_no_photo():
    only_logo = ANCHOR_WITH_PHOTO.replace(
        '<div><img src="https://img.classistatic.de/api/v1/mo-prod/images/ab/car.jpg"></div>',
        "",
    )
    assert MobileDeAdapter._parse_image(_anchor(only_logo)) is None


def test_photoless_fixture_cards_still_parse(adapter, page_html, monkeypatch):
    """The captured fixture has its <img> tags stripped bare — no src at all."""
    install(monkeypatch, {BASE: page_html, BASE + "&pageNumber=2": EMPTY_PAGE})

    listings = adapter.fetch_listings(BASE)

    assert [x.image_url for x in listings] == [None, None]
    assert all(x.price for x in listings)


def test_image_url_reaches_the_raw_listing(adapter, monkeypatch):
    page = (
        '<html><body><div data-testid="result-list">'
        + ANCHOR_WITH_PHOTO
        + "</div></body></html>"
    )
    install(monkeypatch, {BASE: page, BASE + "&pageNumber=2": EMPTY_PAGE})

    (listing,) = adapter.fetch_listings(BASE)

    assert listing.image_url.startswith("https://img.classistatic.de/")


# ------------------------------------------------------- mileage vs consumption


@pytest.mark.parametrize(
    "attributes,expected",
    [
        # A brand-new car states no odometer at all. The consumption figure in
        # the same line used to be read as one: 21 of 173 live listings came
        # back with a plausible-looking, entirely invented 100 km.
        (
            "Mașină nouă • 116 kW (158 CP) • Benzină 8,6 l/100km (comb.) "
            "• 154 g CO₂/km (comb.)",
            None,
        ),
        ("Fără accidente • Prima înmatriculare 05/2025 • 1.609 km • 116 kW", 1609),
        # Consumption *and* a real odometer in one line: the odometer wins.
        ("Prima înmatriculare 03/2026 • 10 km • Benzină 6,3 l/100km (comb.)", 10),
        ("Prima înmatriculare 01/2024 • 163.500 km • Diesel", 163500),
        # A genuine 100 km reading must still parse — the fix is about the "/",
        # not about the number.
        ("Prima înmatriculare 01/2024 • 100 km • Diesel", 100),
        ("", None),
    ],
)
def test_mileage_is_never_taken_from_the_consumption_figure(attributes, expected):
    assert MobileDeAdapter._parse_mileage(attributes) == expected
