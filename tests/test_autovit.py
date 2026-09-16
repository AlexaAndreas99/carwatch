"""autovit adapter tests.

These run offline against a synthetic __NEXT_DATA__ page that mirrors the real
shape observed on autovit.ro (GraphQL urql cache -> advertSearch -> edges[].node).
"""

import json

import pytest

from carwatch.adapters.autovit import AutovitAdapter
from carwatch.adapters.base import AdapterError, BlockedError
from carwatch.adapters.http import looks_like_challenge, with_page_param


def make_node(ad_id, price=25700, currency="EUR", year="2024", mileage="54000",
              title="Nissan Qashqai", short="Nissan Qashqai 1.3 Tekna 4x4"):
    return {
        "id": ad_id,
        "title": title,
        "shortDescription": short,
        "url": f"https://www.autovit.ro/autoturisme/anunt/nissan-qashqai-ID{ad_id}.html",
        "location": {
            "city": {"name": "Bucuresti"},
            "region": {"name": "Bucuresti"},
        },
        "price": {
            "amount": {
                "units": price,
                "nanos": 0,
                "value": str(price),
                "currencyCode": currency,
            }
        },
        "parameters": [
            {"key": "make", "value": "nissan", "displayValue": "Nissan"},
            {"key": "model", "value": "qashqai", "displayValue": "Qashqai"},
            {"key": "year", "value": year, "displayValue": year},
            {"key": "mileage", "value": mileage, "displayValue": f"{mileage} km"},
            {"key": "fuel_type", "value": "petrol", "displayValue": "Benzina"},
        ],
    }


def make_page(nodes, total_count, offset=0, relaxation=None):
    payload = {
        "advertSearch": {
            "totalCount": total_count,
            "pageInfo": {"pageSize": 32, "currentOffset": offset},
            "edges": [{"node": n} for n in nodes],
            "relaxation": relaxation
            or {"rule": None, "applied": False, "filters": None},
        }
    }
    next_data = {
        "props": {"pageProps": {"urqlState": {
            "111": {"data": json.dumps({"seoMetadata": {}})},
            "222": {"data": json.dumps(payload)},
        }}}
    }
    return (
        '<html><head><title>Nissan Qashqai</title></head><body>'
        '<script id="__NEXT_DATA__" type="application/json" nonce="abc123" '
        'crossorigin="anonymous">'
        + json.dumps(next_data)
        + "</script></body></html>"
    )


class FakeClient:
    """Stands in for PoliteClient, serving canned pages by URL."""

    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    def __call__(self, *a, **kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def get_html(self, url):
        self.requested.append(url)
        if url not in self.pages:
            raise AssertionError(f"unexpected URL requested: {url}")
        result = self.pages[url]
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def adapter():
    return AutovitAdapter(None)


def install(monkeypatch, pages):
    fake = FakeClient(pages)
    monkeypatch.setattr("carwatch.adapters.autovit.PoliteClient", fake)
    return fake


# ------------------------------------------------------------------ parsing


def test_parses_a_single_listing(monkeypatch, adapter):
    base = "https://www.autovit.ro/autoturisme/nissan/qashqai"
    install(monkeypatch, {base: make_page([make_node("7060859828")], 1)})

    listings = adapter.fetch_listings(base)

    assert len(listings) == 1
    x = listings[0]
    assert x.site_listing_id == "7060859828"
    assert x.url.endswith("ID7060859828.html")
    assert x.price == 25700.0
    assert x.currency == "EUR"
    assert x.year == 2024
    assert x.mileage_km == 54000
    assert x.make == "Nissan"
    assert x.model == "Qashqai"
    assert x.fuel == "Benzina"
    assert x.location == "Bucuresti"  # city == region, not duplicated
    assert x.title == "Nissan Qashqai 1.3 Tekna 4x4"


def test_title_prefers_short_description_without_duplicating():
    """Regression: title and shortDescription are sometimes both full spec lines;
    concatenating them produced a duplicated mess."""
    node = make_node(
        "1",
        title="Nissan Qashqai 1.3 l 156CP X-Tronic 4WD MHEV Tekna Plus",
        short="Nissan Qashqai 1.3 DIG-T MHEV Xtronic 4x4 Tekna Plus",
    )
    assert AutovitAdapter._title(node) == "Nissan Qashqai 1.3 DIG-T MHEV Xtronic 4x4 Tekna Plus"


def test_title_falls_back_to_title_field():
    assert AutovitAdapter._title({"title": "Nissan Qashqai", "shortDescription": ""}) == "Nissan Qashqai"
    assert AutovitAdapter._title({}) == "(untitled)"


def test_location_joins_city_and_region_when_different():
    assert AutovitAdapter._location(
        {"city": {"name": "Floresti"}, "region": {"name": "Cluj"}}
    ) == "Floresti, Cluj"
    assert AutovitAdapter._location({"city": {"name": "Sibiu"}, "region": {"name": "Sibiu"}}) == "Sibiu"
    assert AutovitAdapter._location(None) is None


def test_price_handles_missing_and_nanos():
    assert AutovitAdapter._price({"amount": {"units": 100, "nanos": 500000000, "currencyCode": "EUR"}}) == (100.5, "EUR")
    assert AutovitAdapter._price({"amount": {"value": "2500", "currencyCode": "RON"}}) == (2500.0, "RON")
    assert AutovitAdapter._price(None) == (None, None)


def test_node_without_stable_id_is_skipped(monkeypatch, adapter):
    """A listing we cannot identify must be dropped, never given a positional id
    — that would corrupt diffing on the next run."""
    base = "https://www.autovit.ro/x"
    good, bad = make_node("111"), make_node("222")
    del bad["id"]
    # totalCount stays 2 while only 1 is usable, so the adapter moves on to
    # page 2 and stops there on empty edges — that is correct behaviour.
    install(monkeypatch, {
        base: make_page([good, bad], 2),
        base + "?page=2": make_page([], 2, offset=32),
    })

    listings = adapter.fetch_listings(base)
    assert [x.site_listing_id for x in listings] == ["111"]


# --------------------------------------------------------------- pagination


def test_paginates_until_total_count_reached(monkeypatch, adapter):
    base = "https://www.autovit.ro/autoturisme/nissan/qashqai/de-la-2024"
    p1 = [make_node(str(i)) for i in range(32)]
    p2 = [make_node(str(i)) for i in range(32, 40)]
    fake = install(monkeypatch, {
        base: make_page(p1, 40, offset=0),
        base + "?page=2": make_page(p2, 40, offset=32),
    })

    listings = adapter.fetch_listings(base)

    assert len(listings) == 40
    assert len({x.site_listing_id for x in listings}) == 40
    # Stops at totalCount — never requests page 3.
    assert fake.requested == [base, base + "?page=2"]


def test_stops_on_empty_edges(monkeypatch, adapter):
    base = "https://www.autovit.ro/x"
    fake = install(monkeypatch, {
        base: make_page([make_node("1")], 999),
        base + "?page=2": make_page([], 999),
    })

    listings = adapter.fetch_listings(base)
    assert len(listings) == 1
    assert fake.requested == [base, base + "?page=2"]


def test_stops_when_site_loops_same_results(monkeypatch, adapter):
    """If the site keeps serving page 1 instead of paginating, stop instead of
    walking to MAX_PAGES and hammering it."""
    base = "https://www.autovit.ro/x"
    same = [make_node("1"), make_node("2")]
    fake = install(monkeypatch, {
        base: make_page(same, 999),
        base + "?page=2": make_page(same, 999),
    })

    listings = adapter.fetch_listings(base)
    assert len(listings) == 2
    assert len(fake.requested) == 2


def test_zero_results_is_not_an_error(monkeypatch, adapter):
    base = "https://www.autovit.ro/x"
    install(monkeypatch, {base: make_page([], 0)})
    assert adapter.fetch_listings(base) == []


# ----------------------------------------------------------------- relaxation

RELAXED = {
    "rule": "price_mileage_year_criteria_changed",
    "applied": True,
    "filters": [
        {"field": "filter_enum_make", "operator": "eq", "value": "nissan", "values": ["nissan"]},
        {"field": "filter_float_year", "operator": "gte", "value": "2024", "values": ["2025"]},
    ],
}


def test_relaxed_search_returns_no_listings(monkeypatch, adapter):
    """autovit widens a search that would return nothing (year>=2025 becomes
    year>=2024) and serves non-matching cars. Ingesting them would create
    phantom `new` events, then phantom `delisted` ones when a real match appears."""
    base = "https://www.autovit.ro/autoturisme/nissan/qashqai/de-la-2025/q-tekna"
    fake = install(monkeypatch, {
        base: make_page([make_node("7060859828", year="2024")], 1, relaxation=RELAXED),
    })

    listings = adapter.fetch_listings(base)

    assert listings == []
    assert adapter.last_relaxation is not None
    assert adapter.last_relaxation["rule"] == "price_mileage_year_criteria_changed"
    # Bails out on page 1 rather than paginating results it is going to discard.
    assert fake.requested == [base]


def test_relaxation_summary_names_the_widened_filter():
    assert AutovitAdapter._relaxation_summary(RELAXED) == "year: asked 2025, used 2024"


def test_relaxation_summary_ignores_unchanged_filters():
    """Only filters whose used value differs from what we asked for are changes."""
    unchanged = {"filters": [
        {"field": "filter_enum_make", "value": "nissan", "values": ["nissan"]},
    ]}
    assert "nissan" not in AutovitAdapter._relaxation_summary(unchanged)


def test_unrelaxed_search_leaves_last_relaxation_none(monkeypatch, adapter):
    base = "https://www.autovit.ro/x"
    install(monkeypatch, {base: make_page([make_node("1")], 1)})
    adapter.fetch_listings(base)
    assert adapter.last_relaxation is None


def test_last_relaxation_is_reset_between_fetches(monkeypatch, adapter):
    """A stale flag from a previous search must not mislabel the next one."""
    relaxed = "https://www.autovit.ro/relaxed"
    clean = "https://www.autovit.ro/clean"
    install(monkeypatch, {
        relaxed: make_page([make_node("1")], 1, relaxation=RELAXED),
        clean: make_page([make_node("2")], 1),
    })

    adapter.fetch_listings(relaxed)
    assert adapter.last_relaxation is not None
    adapter.fetch_listings(clean)
    assert adapter.last_relaxation is None


# ------------------------------------------------------------------ failures


def test_missing_advert_search_is_blocked_not_empty(monkeypatch, adapter):
    """A page that parses but carries no advertSearch is a soft block. Returning
    [] here would delist every listing on the next diff."""
    base = "https://www.autovit.ro/x"
    next_data = {"props": {"pageProps": {"urqlState": {"1": {"data": json.dumps({"seoMetadata": {}})}}}}}
    html = f'<script id="__NEXT_DATA__" nonce="x">{json.dumps(next_data)}</script>'
    install(monkeypatch, {base: html})

    with pytest.raises(BlockedError):
        adapter.fetch_listings(base)


def test_missing_next_data_is_adapter_error(monkeypatch, adapter):
    base = "https://www.autovit.ro/x"
    install(monkeypatch, {base: "<html><body>nothing here</body></html>"})
    with pytest.raises(AdapterError):
        adapter.fetch_listings(base)


def test_blocked_error_propagates(monkeypatch, adapter):
    base = "https://www.autovit.ro/x"
    install(monkeypatch, {base: BlockedError("403")})
    with pytest.raises(BlockedError):
        adapter.fetch_listings(base)


# -------------------------------------------------------- url / block helpers


def test_with_page_param_preserves_pasted_filters():
    url = (
        "https://www.autovit.ro/autoturisme/nissan/qashqai/de-la-2024/q-tekna"
        "?search%5Bfilter_enum_fuel_type%5D=petrol"
        "&search%5Bfilter_enum_gearbox%5D=automatic"
    )
    p2 = with_page_param(url, 2)
    assert "search%5Bfilter_enum_fuel_type%5D=petrol" in p2
    assert "search%5Bfilter_enum_gearbox%5D=automatic" in p2
    assert p2.endswith("page=2")
    # Page 1 carries no page param at all.
    assert "page=" not in with_page_param(url, 1)


def test_with_page_param_keeps_literal_colon():
    """OLX filters look like search[filter_float_price:to] — the colon must not
    be re-encoded to %3A."""
    url = "https://www.olx.ro/x/?search%5Bfilter_float_price:to%5D=30000"
    assert "price:to" in with_page_param(url, 2)


def test_with_page_param_replaces_existing_page():
    url = "https://www.autovit.ro/x?page=7&foo=bar"
    out = with_page_param(url, 3)
    assert out.count("page=") == 1
    assert "page=3" in out
    assert "foo=bar" in out


def test_challenge_detector_ignores_captcha_config_on_real_pages():
    """Regression: autovit's normal pages embed `disableAutoRefreshOnCaptchaPassed`,
    which a bare "captcha" substring match flagged as a bot wall."""
    body = "<html><head><title>Nissan Qashqai</title></head><body>" \
           "<script>disableAutoRefreshOnCaptchaPassed: true,</script>" + ("x" * 300_000) + "</body></html>"
    assert looks_like_challenge(body) is False


@pytest.mark.parametrize("body", [
    "<html><head><title>Just a moment...</title></head><body></body></html>",
    "<html><head><title>Access Denied</title></head><body>no</body></html>",
    "<html><head><title>x</title></head><body><div class='cf-browser-verification'></div></body></html>",
    "<html><head><title>x</title></head><body><iframe src='https://geo.captcha-delivery.com/c'></iframe></body></html>",
])
def test_challenge_detector_catches_real_walls(body):
    assert looks_like_challenge(body) is True


# --------------------------------------------------------------- card photos


def test_reads_the_card_photo_from_the_node(monkeypatch, adapter):
    """§4: the image is hot-linked from the URL autovit already hands us."""
    base = "https://www.autovit.ro/autoturisme/nissan/qashqai"
    node = make_node("7060859828")
    node["thumbnail"] = {
        "x1": "https://ireland.apollo.olxcdn.com/v1/files/abc-AUTOVITRO/image;s=320x240",
        "x2": "https://ireland.apollo.olxcdn.com/v1/files/abc-AUTOVITRO/image;s=640x480",
    }
    install(monkeypatch, {base: make_page([node], 1)})

    (listing,) = adapter.fetch_listings(base)

    # x2 over x1: cards are ~268px wide, so 320x240 is already soft at 2x.
    assert listing.image_url.endswith("s=640x480")


def test_card_photo_falls_back_to_x1():
    thumbnail = {"x1": "https://cdn/image;s=320x240", "x2": None}
    assert AutovitAdapter._image(thumbnail) == "https://cdn/image;s=320x240"


@pytest.mark.parametrize("thumbnail", [None, {}, {"x1": "", "x2": "  "}])
def test_missing_card_photo_is_none(thumbnail):
    assert AutovitAdapter._image(thumbnail) is None


def test_listing_without_a_thumbnail_still_parses(monkeypatch, adapter):
    """A photo is a nice-to-have; its absence must not cost us the listing."""
    base = "https://www.autovit.ro/autoturisme/nissan/qashqai"
    install(monkeypatch, {base: make_page([make_node("7060859828")], 1)})

    (listing,) = adapter.fetch_listings(base)

    assert listing.image_url is None
    assert listing.price == 25700.0
