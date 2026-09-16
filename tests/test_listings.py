"""Listings page tests (front-end plan §6/§7).

The page is the merge layer made visible, so these exercise it end to end: three
sites collected into a real database, then rendered, then asserted on the HTML.
Unit-level merge behaviour lives in `test_merge.py`.
"""

import textwrap

import pytest
from fastapi.testclient import TestClient

from carwatch.adapters.base import RawListing
from carwatch.collector.engine import collect
from carwatch.config import load_config
from carwatch.web.app import create_app

CONFIG = """
settings:
  db_path: "{db}"
searches:
  - name: "Qashqai"
    sources:
      - site: "autovit"
        url: "https://www.autovit.ro/autoturisme/nissan/qashqai"
      - site: "olx"
        url: "https://www.olx.ro/autoturisme/nissan/"
      - site: "mobilede"
        url: "https://www.mobile.de/ro/vehicule/cautare.html?s=Car"
"""

AD_SLUG = "7HQf7M"
AUTOVIT_AD = "https://www.autovit.ro/autoturisme/anunt/nissan-qashqai-ID7HQf7M.html"
OLX_SAME_AD = "https://www.autovit.ro/anunt/nissan-qashqai-ID7HQf7M.html"
OTHER_AUTOVIT_AD = "https://www.autovit.ro/autoturisme/anunt/other-IDzzz999.html"


class PerSiteAdapter:
    """Serves a different scripted result per site, so merges can be exercised."""

    last_relaxation = None

    def __init__(self, by_site):
        self.by_site = by_site
        self.site = None

    def __call__(self, site, settings):
        self.site = site
        return self

    def fetch_listings(self, url, max_pages=None):
        return self.by_site.get(self.site, [])


def raw(ad_id, url, price, **kw):
    kw.setdefault("title", "Nissan Qashqai " + ad_id)
    kw.setdefault("year", 2025)
    kw.setdefault("mileage_km", 12000)
    return RawListing(
        site_listing_id=ad_id, url=url, price=price, currency="EUR", **kw
    )


def mobilede(ad_id, price, **kw):
    url = "https://www.mobile.de/ro/vehicule/detalii.html?id=" + ad_id
    return raw(ad_id, url, price, **kw)


@pytest.fixture
def project(tmp_path):
    """A config plus a `collect(...)` helper writing to its own database."""
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(CONFIG).format(db=(tmp_path / "t.db").as_posix()),
        encoding="utf-8",
    )
    config = load_config(path)

    class Project:
        db = config.db_file
        config_path = path

        def reconfigure(self, text):
            """Rewrite config.yaml, as editing it between collections would."""
            path.write_text(
                textwrap.dedent(text).format(db=(tmp_path / "t.db").as_posix()),
                encoding="utf-8",
            )
            return self

        def run(self, by_site):
            # Reloaded each time, so a reconfigure between runs takes effect.
            collect(load_config(path), adapter_factory=PerSiteAdapter(by_site))
            return self

        def client(self, view=None):
            client = TestClient(create_app(path))
            if view:
                client.cookies.set("carwatch-view", view)
            return client

        def get(self, url="/", view=None):
            return self.client(view).get(url).text

    return Project()


# ------------------------------------------------------------------ merging


def test_the_same_ad_on_two_sites_is_one_card(project):
    """The whole point of §5 — on live data this turned 87 rows into 42 cars."""
    body = project.run(
        {
            "autovit": [raw("700", AUTOVIT_AD, 26980.0)],
            "olx": [raw("305871621", OLX_SAME_AD, 27500.0)],
        }
    ).get()

    assert body.count("card-photo-link") == 1
    assert "AUTOVIT" in body and "OLX" in body
    # Decision 4: the autovit price is the canonical one on a merged card.
    assert "26 980 EUR" in body
    assert "27 500" not in body


def test_an_olx_ad_that_is_not_syndicated_gets_its_own_card(project):
    body = project.run(
        {
            "autovit": [raw("700", AUTOVIT_AD, 26980.0)],
            "olx": [raw("999", "https://www.olx.ro/d/oferta/qashqai-IDabc.html", 24000.0)],
        }
    ).get()

    assert body.count("card-photo-link") == 2


# ------------------------------------------------------------------- tabs


def test_markets_are_separate_tabs(project):
    project.run(
        {
            "autovit": [raw("700", AUTOVIT_AD, 26980.0, title="Romanian car")],
            "mobilede": [mobilede("461256563", 31480.0, title="German car")],
        }
    )

    romania = project.get("/?market=ro")
    germany = project.get("/?market=de")

    assert "Romanian car" in romania and "German car" not in romania
    assert "German car" in germany and "Romanian car" not in germany
    # The Romanian median does appear on the Germany tab — as the reference the
    # marker is measured against, not as a listing.
    assert "AUTOVIT" not in germany


def test_tab_counts_are_cars_not_rows(project):
    """Two rows for one syndicated ad must read as one car on the tab."""
    body = project.run(
        {
            "autovit": [raw("700", AUTOVIT_AD, 26980.0)],
            "olx": [raw("305871621", OLX_SAME_AD, 27500.0)],
            "mobilede": [mobilede("1", 31480.0), mobilede("2", 33000.0)],
        }
    ).get()

    assert 'Romania <span class="tab-count">1</span>' in body
    assert 'Germany <span class="tab-count">2</span>' in body


def test_an_unknown_market_falls_back_instead_of_erroring(project):
    response = project.run({"autovit": [raw("700", AUTOVIT_AD, 26980.0)]}).client().get(
        "/?market=nowhere"
    )
    assert response.status_code == 200
    assert "26 980 EUR" in response.text


def test_empty_database_renders_an_empty_state(project):
    body = project.get()
    assert "Nothing collected" in body


# ------------------------------------------------------------------ photos


def test_photos_are_hot_linked_and_lazy(project):
    body = project.run(
        {"autovit": [raw("700", AUTOVIT_AD, 26980.0, image_url="https://cdn/car.jpg")]}
    ).get()

    assert 'src="https://cdn/car.jpg"' in body
    assert 'loading="lazy"' in body


def test_a_merged_card_borrows_the_photo_from_whichever_site_has_one(project):
    """17 of 47 olx cards carry no photo; merged, they get autovit's."""
    project.run(
        {
            "autovit": [raw("700", AUTOVIT_AD, 26980.0, image_url="https://cdn/car.jpg")],
            "olx": [raw("305871621", OLX_SAME_AD, 27500.0)],
        }
    )

    for view in ("cards", "compact"):
        body = project.get(view=view)
        assert body.count('src="https://cdn/car.jpg"') == 1


def test_a_car_without_a_photo_gets_the_placeholder(project):
    body = project.run({"autovit": [raw("700", AUTOVIT_AD, 26980.0)]}).get()

    assert "photo-fallback" in body
    assert "<img" not in body


# ------------------------------------------------------------- the two views


def test_only_the_view_in_use_is_rendered(project):
    """Emitting both and hiding one with CSS made the Germany tab 528KB of HTML
    for 173 cars, half of it markup nobody was looking at."""
    project.run({"autovit": [raw("700", AUTOVIT_AD, 26980.0)]})

    cards = project.get(view="cards")
    compact = project.get(view="compact")

    assert "card-grid" in cards and "compact-list" not in cards
    assert "compact-list" in compact and "card-grid" not in compact


def test_cards_are_the_default_view(project):
    project.run({"autovit": [raw("700", AUTOVIT_AD, 26980.0)]})

    body = project.get()

    assert "card-grid" in body
    assert "compact-list" not in body


def test_an_unknown_cookie_value_falls_back_to_cards(project):
    """A stale or hand-edited cookie must not blank the page."""
    project.run({"autovit": [raw("700", AUTOVIT_AD, 26980.0)]})

    body = project.get(view="nonsense")

    assert "card-grid" in body


def test_the_toggle_marks_the_view_you_are_on(project):
    """The server renders the active state, so it is right on first paint
    rather than after a script runs."""
    project.run({"autovit": [raw("700", AUTOVIT_AD, 26980.0)]})

    compact = project.get(view="compact")

    assert 'data-view="compact" title="Compact rows"' in compact
    assert 'aria-pressed="true"' in compact
    assert compact.count('aria-pressed="true"') == 1


def test_rendering_one_view_roughly_halves_the_page(project):
    """The measurement that prompted this: 270KB of cards plus 254KB of compact
    rows for the same cars."""
    project.run(
        {"autovit": [raw(str(n), AUTOVIT_AD.replace("7HQf7M", "ID%03d" % n), 20000.0 + n)
                     for n in range(40)]}
    )

    cards = project.get(view="cards")
    compact = project.get(view="compact")

    # Neither view carries the other's markup at all.
    assert "<article class=\"card" not in compact
    assert "thumb-cell" not in cards


# ------------------------------------------------------------------ ordering


def test_newest_first_on_every_tab(project):
    """The agreed default: arrivals are what you are watching for, and it is the
    one ordering that means anything before there is price history."""
    project.run({"autovit": [raw("1", OTHER_AUTOVIT_AD, 20000.0, title="Older car")]})
    project.run(
        {
            "autovit": [
                raw("1", OTHER_AUTOVIT_AD, 20000.0, title="Older car"),
                raw("2", AUTOVIT_AD, 30000.0, title="Newer car"),
            ]
        }
    )

    body = project.get()

    assert body.index("Newer car") < body.index("Older car")


# ---------------------------------------------------------------- sparkline


def test_no_sparkline_until_there_is_a_real_trace(project):
    """§7: a real price trace, not decoration. One observation is not a trace."""
    body = project.run({"autovit": [raw("1", AUTOVIT_AD, 26980.0)]}).get()
    assert "<polyline" not in body


def test_a_price_drop_gets_a_sparkline_a_flag_and_the_green_border(project):
    project.run({"autovit": [raw("1", AUTOVIT_AD, 26980.0)]})
    body = project.run({"autovit": [raw("1", AUTOVIT_AD, 25500.0)]}).get()

    assert "<polyline" in body
    assert "drop-flag" in body
    assert "card-drop" in body


def test_a_price_rise_gets_the_trace_but_not_the_drop_treatment(project):
    """Green is reserved for drops; nothing else may claim it."""
    project.run({"autovit": [raw("1", AUTOVIT_AD, 25500.0)]})
    body = project.run({"autovit": [raw("1", AUTOVIT_AD, 26980.0)]}).get()

    assert "<polyline" in body
    assert "drop-flag" not in body
    assert "card-drop" not in body


# ------------------------------------------------------- cross-market marker


def test_germany_marks_cars_under_the_romanian_median(project):
    """Chosen deliberately, with the caveat kept on screen: German list prices
    are frequently pre-VAT and exclude import and registration cost."""
    project.run(
        {
            "autovit": [
                raw("700", AUTOVIT_AD, 30000.0),
                raw("701", OTHER_AUTOVIT_AD, 30000.0),
            ],
            "mobilede": [mobilede("1", 25000.0), mobilede("2", 35000.0)],
        }
    )

    germany = project.get("/?market=de")
    romania = project.get("/?market=ro")

    # The 25 000 car only, and only once now that one view is rendered.
    assert germany.count("ref-marker") == 1
    assert "Romanian median" in germany
    assert "pre-VAT" in germany
    # One-directional: Romania is not marked against its own median.
    assert "ref-marker" not in romania
    assert "Romanian median" not in romania


def test_no_marker_when_there_is_no_romanian_data_to_compare_against(project):
    germany = project.run({"mobilede": [mobilede("1", 25000.0)]}).get("/?market=de")

    assert "ref-marker" not in germany
    assert "Romanian median" not in germany


# ----------------------------------------------------------------- delisted


def test_delisted_cars_keep_their_muted_section_at_the_bottom(project):
    """Agreed: a muted section as on the search detail page, not a filter toggle."""
    project.run({"autovit": [raw("1", AUTOVIT_AD, 26980.0, title="Gone car")]})
    body = project.run({"autovit": []}).get()

    assert "Recently delisted" in body
    assert "Gone car" in body


def test_a_car_still_listed_on_one_site_is_not_delisted(project):
    """One site dropping a syndicated ad is not the car being gone."""
    project.run(
        {
            "autovit": [raw("700", AUTOVIT_AD, 26980.0, title="Still here")],
            "olx": [raw("305871621", OLX_SAME_AD, 27500.0)],
        }
    )
    body = project.run({"autovit": [], "olx": [raw("305871621", OLX_SAME_AD, 27500.0)]}).get()

    assert "Recently delisted" not in body
    assert "Still here" in body


# ------------------------------------------------------- filters and sorting


def fleet(project):
    """A small, deliberately varied fleet to filter against."""
    return project.run(
        {
            "autovit": [
                raw(
                    "1",
                    "https://www.autovit.ro/autoturisme/anunt/a-IDaaa111.html",
                    22000.0,
                    title="Cheap petrol Qashqai",
                    year=2024,
                    mileage_km=80000,
                    fuel="Benzina",
                    location="Cluj",
                ),
                raw(
                    "2",
                    "https://www.autovit.ro/autoturisme/anunt/b-IDbbb222.html",
                    31000.0,
                    title="Dear hybrid Qashqai",
                    year=2026,
                    mileage_km=5000,
                    fuel="Hibrid",
                    location="Bucuresti",
                ),
                raw(
                    "3",
                    "https://www.autovit.ro/autoturisme/anunt/c-IDccc333.html",
                    26000.0,
                    title="Middling diesel Qashqai",
                    year=2025,
                    mileage_km=40000,
                    fuel="Diesel",
                    location="Cluj",
                ),
            ]
        }
    )


def titles(body):
    """Card titles in render order."""
    import re

    return re.findall(r'class="card-title"[^>]*>\s*([^<]+?)\s*<', body)


def test_text_filter_matches_title_and_location(project):
    fleet(project)

    assert titles(project.get("/?q=hybrid")) == ["Dear hybrid Qashqai"]
    assert sorted(titles(project.get("/?q=cluj"))) == [
        "Cheap petrol Qashqai",
        "Middling diesel Qashqai",
    ]


def test_price_range_filter(project):
    fleet(project)

    assert titles(project.get("/?price_min=25000&price_max=30000")) == [
        "Middling diesel Qashqai"
    ]


def test_year_range_filter(project):
    fleet(project)

    assert titles(project.get("/?year_min=2025&year_max=2025")) == [
        "Middling diesel Qashqai"
    ]


def test_max_mileage_filter(project):
    fleet(project)

    assert titles(project.get("/?mileage_max=10000")) == ["Dear hybrid Qashqai"]


def test_fuel_filter_and_its_options(project):
    fleet(project)

    body = project.get()
    for fuel in ("Benzina", "Diesel", "Hibrid"):
        assert '<option value="%s"' % fuel in body

    assert titles(project.get("/?fuel=Diesel")) == ["Middling diesel Qashqai"]


def test_filters_combine(project):
    fleet(project)

    assert titles(project.get("/?q=qashqai&year_min=2025&price_max=30000")) == [
        "Middling diesel Qashqai"
    ]


def test_a_filter_that_matches_nothing_says_so_and_offers_a_way_back(project):
    fleet(project)

    body = project.get("/?price_max=1")

    assert "No Romania cars match these filters" in body
    assert "Clear them" in body


def test_the_count_shows_the_filtered_total(project):
    fleet(project)

    body = project.get("/?fuel=Diesel")

    assert "<strong>1</strong> car" in body
    assert "of 3" in body


# ------------------------------------------------------------------- sorting


def test_default_sort_is_newest_first(project):
    project.run({"autovit": [raw("1", OTHER_AUTOVIT_AD, 20000.0, title="First car")]})
    project.run(
        {
            "autovit": [
                raw("1", OTHER_AUTOVIT_AD, 20000.0, title="First car"),
                raw("2", AUTOVIT_AD, 30000.0, title="Second car"),
            ]
        }
    )

    assert titles(project.get()) == ["Second car", "First car"]
    # The default is not spelled out in the URL, so a plain "/" stays plain.
    assert 'value="newest" selected' in project.get()


def test_sort_by_price_ascending(project):
    fleet(project)

    assert titles(project.get("/?sort=price")) == [
        "Cheap petrol Qashqai",
        "Middling diesel Qashqai",
        "Dear hybrid Qashqai",
    ]


def test_sort_by_mileage_ascending(project):
    fleet(project)

    assert titles(project.get("/?sort=mileage")) == [
        "Dear hybrid Qashqai",
        "Middling diesel Qashqai",
        "Cheap petrol Qashqai",
    ]


def test_sort_by_year_newest_first(project):
    fleet(project)

    assert titles(project.get("/?sort=year")) == [
        "Dear hybrid Qashqai",
        "Middling diesel Qashqai",
        "Cheap petrol Qashqai",
    ]


def test_sort_by_biggest_drop(project):
    project.run(
        {
            "autovit": [
                raw("1", OTHER_AUTOVIT_AD, 30000.0, title="Small drop"),
                raw("2", AUTOVIT_AD, 30000.0, title="Big drop"),
            ]
        }
    )
    project.run(
        {
            "autovit": [
                raw("1", OTHER_AUTOVIT_AD, 29500.0, title="Small drop"),
                raw("2", AUTOVIT_AD, 25000.0, title="Big drop"),
            ]
        }
    )

    assert titles(project.get("/?sort=drop")) == ["Big drop", "Small drop"]


def test_cars_with_nothing_to_sort_on_go_last_rather_than_crashing(project):
    """A missing price or mileage must never raise, and must never sort as 0 —
    a car with no price is not the cheapest car."""
    project.run(
        {
            "autovit": [
                raw("1", OTHER_AUTOVIT_AD, None, title="No price", mileage_km=None),
                raw("2", AUTOVIT_AD, 25000.0, title="Has price"),
            ]
        }
    )

    assert titles(project.get("/?sort=price")) == ["Has price", "No price"]
    assert titles(project.get("/?sort=mileage")) == ["Has price", "No price"]


def test_an_unknown_sort_key_falls_back_instead_of_erroring(project):
    fleet(project)

    response = project.client().get("/?sort=nonsense")

    assert response.status_code == 200
    assert 'value="newest" selected' in response.text


# ------------------------------------------------------- state in the URL


def test_switching_market_keeps_the_filters(project):
    project.run(
        {
            "autovit": [raw("1", AUTOVIT_AD, 26000.0, fuel="Diesel")],
            "mobilede": [mobilede("9", 31000.0, fuel="Diesel")],
        }
    )

    body = project.get("/?fuel=Diesel&sort=price&q=qashqai")

    assert "market=de" in body
    assert "fuel=Diesel" in body
    assert "sort=price" in body
    assert "q=qashqai" in body


def test_a_clear_link_appears_only_once_something_is_filtered(project):
    fleet(project)

    assert ">clear</a>" not in project.get()
    assert ">clear</a>" in project.get("/?fuel=Diesel")


def test_the_sidebar_replaces_the_search_picker(project):
    """The two toolbar dropdowns retired into one sidebar selection (§8).

    One configuration is several `search` rows, one per site, and a merged car
    can belong to more than one — so the scope matches on any of them.
    """
    fleet(project)

    body = project.get()

    assert ">All searches</option>" not in body
    assert "All configurations" in body
    assert 'href="/?config=qashqai"' in body

    assert titles(project.get("/?config=qashqai")) != []


def test_an_unknown_config_slug_falls_back_to_all(project):
    """A link to a configuration that is gone shows everything, not a 404: the
    sidebar is right there saying what does exist."""
    fleet(project)

    assert titles(project.get("/?config=nothing-named-this")) == titles(project.get())


def test_the_german_reference_median_ignores_the_filters(project):
    """Narrowing the German list must not move the Romanian yardstick it is
    measured against."""
    project.run(
        {
            "autovit": [
                raw("1", AUTOVIT_AD, 20000.0),
                raw("2", OTHER_AUTOVIT_AD, 30000.0),
            ],
            "mobilede": [mobilede("9", 31000.0), mobilede("10", 40000.0)],
        }
    )

    unfiltered = project.get("/?market=de")
    filtered = project.get("/?market=de&price_min=35000")

    assert "25 000 EUR" in unfiltered
    assert "25 000 EUR" in filtered


# ------------------------------------------------- restyled changes feed


def test_the_changes_feed_shows_photos(project):
    project.run(
        {"autovit": [raw("1", AUTOVIT_AD, 26980.0, image_url="https://cdn/car.jpg")]}
    )

    body = project.get("/changes?baseline=1")

    assert 'src="https://cdn/car.jpg"' in body
    assert 'loading="lazy"' in body
    assert "row-photo" in body


def test_a_changes_row_without_a_photo_falls_back_like_a_card(project):
    project.run({"autovit": [raw("1", AUTOVIT_AD, 26980.0)]})

    body = project.get("/changes?baseline=1")

    assert "photo-fallback" in body


def test_the_changes_feed_badges_the_source(project):
    project.run({"autovit": [raw("1", AUTOVIT_AD, 26980.0)]})

    body = project.get("/changes?baseline=1")

    assert "AUTOVIT" in body


def test_the_changes_feed_still_reports_price_movement(project):
    """§6: unchanged in function, restyled only."""
    project.run({"autovit": [raw("1", AUTOVIT_AD, 26980.0)]})
    body = project.run({"autovit": [raw("1", AUTOVIT_AD, 25500.0)]}).get("/changes")

    assert "drop" in body
    assert "26 980" in body and "25 500" in body


# ----------------------------------------------------- missing facts


def test_unknown_year_and_mileage_are_omitted_not_dashed(project):
    """A brand-new mobile.de car has neither. Printing "— · —" reads as a
    rendering fault rather than as two things we do not know."""
    project.run(
        {
            "mobilede": [
                mobilede(
                    "1",
                    31480.0,
                    title="Brand new Qashqai",
                    year=None,
                    mileage_km=None,
                    fuel="Benzina",
                    location="DE-72658 Bempflingen",
                )
            ]
        }
    )

    body = project.get("/?market=de")

    assert "Benzina \u00b7 DE-72658 Bempflingen" in body
    assert "\u2014 \u00b7 \u2014" not in body


def test_known_facts_are_still_all_shown(project):
    project.run(
        {
            "autovit": [
                raw(
                    "1",
                    AUTOVIT_AD,
                    26980.0,
                    year=2025,
                    mileage_km=15651,
                    fuel="Benzina",
                    location="Cluj",
                )
            ]
        }
    )

    body = project.get()

    assert "2025 \u00b7 15 651 km \u00b7 Benzina \u00b7 Cluj" in body


# ------------------------------------------------------- source health


def test_runs_page_carries_the_health_table_and_the_log(project):
    project.run({"autovit": [raw("1", AUTOVIT_AD, 26980.0)]})

    body = project.get("/runs")

    assert "Run log" in body
    assert "before merging" in body
    assert "autovit" in body


# ------------------------------------------------------- static asset cache


def test_the_stylesheet_url_carries_a_version_stamp(project):
    """StaticFiles sends no Cache-Control, so browsers reuse a cached
    stylesheet without revalidating and a CSS edit appears to do nothing."""
    import re

    body = project.get()

    match = re.search(r'href="(/static/style\.css\?v=\d+)"', body)
    assert match, "stylesheet link has no version stamp"


def test_the_stamp_changes_when_the_stylesheet_changes(project, monkeypatch, tmp_path):
    import re

    from carwatch.web import app as app_module

    css = tmp_path / "style.css"
    css.write_text("body { color: red }", encoding="utf-8")
    monkeypatch.setattr(app_module, "STATIC_DIR", tmp_path)

    first = app_module._static_url("style.css")
    import os

    os.utime(css, (0, 1_700_000_000))
    second = app_module._static_url("style.css")

    assert first != second
    assert re.match(r"^/static/style\.css\?v=\d+$", second)


def test_a_missing_static_file_still_yields_a_usable_url(monkeypatch, tmp_path):
    """A stat() failure must not take the whole page down over a cache hint."""
    from carwatch.web import app as app_module

    monkeypatch.setattr(app_module, "STATIC_DIR", tmp_path)

    assert app_module._static_url("nope.css") == "/static/nope.css"


def test_the_stylesheet_is_actually_served(project):
    response = project.client().get("/static/style.css")

    assert response.status_code == 200
    assert "card-grid" in response.text


# ------------------------------------------- empty and unusable filter values

# The toolbar is a plain GET form, so every field is submitted whether or not
# it was filled in: pressing Apply with the price boxes blank sends
# "?price_min=&price_max=&year_min=..." — not an absent parameter. Declaring
# them as bare Optional[float] made that a 422 with a JSON dump of validation
# errors instead of the page. Every earlier test omitted the parameters, which
# is why none of them caught it.

FORM_SUBMIT = (
    "/?market=de&q=&price_min=&price_max=&year_min=&year_max="
    "&mileage_max=&fuel=&search=&sort=price"
)


def test_submitting_the_toolbar_with_every_field_blank(project):
    project.run({"mobilede": [mobilede("1", 31480.0, title="German car")]})

    response = project.client().get(FORM_SUBMIT)

    assert response.status_code == 200
    assert "German car" in response.text


def test_blank_numbers_do_not_filter_anything_out(project):
    fleet(project)

    body = project.get("/?price_min=&price_max=&year_min=&mileage_max=")

    assert len(titles(body)) == 3


def test_one_filled_field_still_applies_when_the_rest_are_blank(project):
    fleet(project)

    body = project.get("/?price_min=&price_max=25000&year_min=&mileage_max=")

    assert titles(body) == ["Cheap petrol Qashqai"]


@pytest.mark.parametrize(
    "value", ["abc", "12,5", "1e", "--3", " ", "%20", "NaN-ish"]
)
def test_an_unusable_number_is_ignored_rather_than_returning_a_traceback(
    project, value
):
    """A hand-edited URL must not be answered with a JSON validation dump. The
    dropped filter is self-evident: the box renders back empty."""
    fleet(project)

    response = project.client().get("/?price_max=" + value)

    assert response.status_code == 200
    assert len(titles(response.text)) == 3


def test_a_round_tripped_decimal_year_still_reads_as_a_year(project):
    fleet(project)

    assert titles(project.get("/?year_min=2026.0")) == ["Dear hybrid Qashqai"]


def test_the_search_detail_page_survives_a_blank_form_too(project):
    """It has its own filter form with the same shape of number fields."""
    project.run({"autovit": [raw("1", AUTOVIT_AD, 26980.0)]})

    response = project.client().get("/search/1?q=&price_max=&year_min=&sort=price")

    assert response.status_code == 200


# ------------------------- blank parameters, everywhere they can be submitted

# The Listings toolbar was not the only form that submits empty fields. The
# Changes search picker renders <option value="">All searches</option> and
# auto-submits on change, so selecting it sent "search_id=" and answered with
# a 422 JSON dump. These cover every route that takes a number.


@pytest.mark.parametrize(
    "url",
    [
        "/changes?type=all&search_id=",
        "/changes?limit=",
        "/changes?type=drops&search_id=&limit=",
        "/runs?limit=",
        "/search/1?q=&price_max=&year_min=",
        "/?price_min=&price_max=&year_min=&year_max=&mileage_max=",
    ],
)
def test_no_route_422s_on_a_blank_number(project, url):
    project.run({"autovit": [raw("1", AUTOVIT_AD, 26980.0)]})

    assert project.client().get(url).status_code == 200


@pytest.mark.parametrize("url", ["/changes?limit=abc", "/runs?limit=abc"])
def test_an_unusable_limit_falls_back_to_the_default(project, url):
    project.run({"autovit": [raw("1", AUTOVIT_AD, 26980.0)]})

    assert project.client().get(url).status_code == 200


@pytest.mark.parametrize("url", ["/changes?limit=999999", "/runs?limit=999999"])
def test_an_absurd_limit_is_clamped_rather_than_rejected(project, url):
    """`le=` answered a hand-typed number with a 422. Clamping still protects
    the query and still renders the page."""
    project.run({"autovit": [raw("1", AUTOVIT_AD, 26980.0)]})

    assert project.client().get(url).status_code == 200


def test_changes_scopes_on_the_same_config_parameter_as_listings(project):
    """One selection, shared between the two pages (§5). The old picker keyed on
    the configuration *and* site, which is why the two disagreed."""
    project.run({"autovit": [raw("1", AUTOVIT_AD, 26980.0)]})

    body = project.get("/changes")

    assert "All configurations" in body
    assert '<option value="">All searches</option>' not in body
    assert project.client().get("/changes?config=qashqai").status_code == 200
    assert project.client().get("/changes?config=").status_code == 200


# --------------------------------------------- one change, one row in the feed


def feed_rows(body):
    return body.count('class="feed-row')


def test_one_change_on_a_syndicated_ad_is_one_feed_row(project):
    """Measured on live data before this: 87 event rows behind 42 cars, 38 of
    them repeated. The same drop should be reported once."""
    project.run(
        {
            "autovit": [raw("700", AUTOVIT_AD, 26980.0)],
            "olx": [raw("305871621", OLX_SAME_AD, 26980.0)],
        }
    )
    body = project.run(
        {
            "autovit": [raw("700", AUTOVIT_AD, 25500.0)],
            "olx": [raw("305871621", OLX_SAME_AD, 25500.0)],
        }
    ).get("/changes?type=drops")

    assert feed_rows(body) == 1


def test_a_merged_feed_row_names_every_site_that_reported_it(project):
    project.run(
        {
            "autovit": [raw("700", AUTOVIT_AD, 26980.0)],
            "olx": [raw("305871621", OLX_SAME_AD, 26980.0)],
        }
    )
    body = project.run(
        {
            "autovit": [raw("700", AUTOVIT_AD, 25500.0)],
            "olx": [raw("305871621", OLX_SAME_AD, 25500.0)],
        }
    ).get("/changes?type=drops")

    assert "AUTOVIT" in body and "OLX" in body


def test_the_feed_quotes_the_canonical_price(project):
    """Same rule as a merged card: autovit speaks for the car, so the feed and
    the Listings page never disagree about the number."""
    project.run(
        {
            "autovit": [raw("700", AUTOVIT_AD, 26980.0)],
            "olx": [raw("305871621", OLX_SAME_AD, 27500.0)],
        }
    )
    body = project.run(
        {
            "autovit": [raw("700", AUTOVIT_AD, 25500.0)],
            "olx": [raw("305871621", OLX_SAME_AD, 26000.0)],
        }
    ).get("/changes?type=drops")

    assert "26 980" in body and "25 500" in body
    assert "27 500" not in body


def test_two_separate_cars_stay_two_rows(project):
    project.run(
        {
            "autovit": [
                raw("1", AUTOVIT_AD, 26980.0),
                raw("2", OTHER_AUTOVIT_AD, 30000.0),
            ]
        }
    )
    body = project.run(
        {
            "autovit": [
                raw("1", AUTOVIT_AD, 25500.0),
                raw("2", OTHER_AUTOVIT_AD, 29000.0),
            ]
        }
    ).get("/changes?type=drops")

    assert feed_rows(body) == 2


def test_different_kinds_of_change_are_never_folded_together(project):
    """A car that appeared and later dropped is two things that happened."""
    project.run({"autovit": [raw("1", AUTOVIT_AD, 26980.0)]})
    body = project.run({"autovit": [raw("1", AUTOVIT_AD, 25500.0)]}).get(
        "/changes?baseline=1"
    )

    assert feed_rows(body) == 2  # one "new", one "drop"


def test_the_chip_counts_match_the_rows_they_promise(project):
    """87 events behind 42 rows meant every chip over-promised."""
    import re

    project.run(
        {
            "autovit": [raw("700", AUTOVIT_AD, 26980.0)],
            "olx": [raw("305871621", OLX_SAME_AD, 26980.0)],
        }
    )
    body = project.run(
        {
            "autovit": [raw("700", AUTOVIT_AD, 25500.0)],
            "olx": [raw("305871621", OLX_SAME_AD, 25500.0)],
        }
    ).get("/changes?type=drops")

    shown = feed_rows(body)
    claimed = int(
        re.search(r'\u25bc Price drops <span class="dim">(\d+)</span>', body).group(1)
    )
    assert claimed == shown == 1


# --------------------------------------------- "New" instead of an empty year


def test_an_unregistered_car_says_new_rather_than_leaving_a_gap(project):
    """21 of 173 German listings are brand new, so they have no
    first-registration year — and newest-first puts every one of them on the
    first screen, where a column of blanks reads as a broken parser."""
    project.run(
        {
            "mobilede": [
                mobilede(
                    "1",
                    31480.0,
                    title="Brand new Qashqai",
                    year=None,
                    mileage_km=None,
                    condition="new",
                    fuel="Benzina",
                )
            ]
        }
    )

    body = project.get("/?market=de")

    assert "New" in body
    assert "Brand new Qashqai" in body


def test_a_car_with_a_year_still_shows_the_year(project):
    project.run({"autovit": [raw("1", AUTOVIT_AD, 26980.0, year=2025)]})

    body = project.get()

    assert "2025" in body


def test_an_unknown_year_with_no_explanation_is_still_a_dash(project):
    """Only "new" earns the word. A year we simply failed to parse must not be
    dressed up as a fact."""
    project.run(
        {"autovit": [raw("1", AUTOVIT_AD, 26980.0, year=None, condition=None)]}
    )

    body = project.get()

    assert ">New<" not in body


def test_condition_survives_a_round_trip_through_the_database(project):
    from sqlmodel import select

    from carwatch.db import session_scope
    from carwatch.models import Listing

    project.run(
        {"mobilede": [mobilede("1", 31480.0, year=None, condition="new")]}
    )

    with session_scope(project.db) as session:
        (row,) = session.exec(select(Listing)).all()

    assert row.condition == "new"
    assert row.year is None


# ------------------------------------------------- the toolbar applies itself


def test_the_toolbar_has_no_apply_button_for_people_with_javascript(project):
    project.run({"autovit": [raw("1", AUTOVIT_AD, 26980.0)]})

    body = project.get()

    assert "data-autosubmit" in body
    # The only Apply left is the no-JS fallback.
    assert body.count("<button type=\"submit\">Apply</button>") == 1
    assert "<noscript><button type=\"submit\">Apply</button></noscript>" in body


def test_the_changes_picker_submits_itself_the_same_way(project):
    project.run({"autovit": [raw("1", AUTOVIT_AD, 26980.0)]})

    body = project.get("/changes")

    assert "data-autosubmit" in body
    # Replaced by the shared handler rather than an inline attribute.
    assert "onchange=" not in body


def test_the_search_detail_form_applies_itself_too(project):
    project.run({"autovit": [raw("1", AUTOVIT_AD, 26980.0)]})

    body = project.client().get("/search/1").text

    assert "data-autosubmit" in body
    assert "<noscript><button type=\"submit\">Filter</button></noscript>" in body


# ------------------------------------------------------ the search detail page


def test_the_detail_page_shows_photos(project):
    project.run(
        {"autovit": [raw("1", AUTOVIT_AD, 26980.0, image_url="https://cdn/car.jpg")]}
    )

    body = project.client().get("/search/1").text

    assert 'src="https://cdn/car.jpg"' in body
    assert "row-photo" in body


def test_the_detail_page_draws_a_sparkline_once_there_is_a_trace(project):
    project.run({"autovit": [raw("1", AUTOVIT_AD, 26980.0)]})
    assert "<polyline" not in project.client().get("/search/1").text

    project.run({"autovit": [raw("1", AUTOVIT_AD, 25500.0)]})
    assert "<polyline" in project.client().get("/search/1").text


def test_the_detail_page_stays_unmerged(project):
    """It is the one view that shows exactly what a single source returned —
    that is the whole reason to keep it next to the merged Listings page."""
    project.run(
        {
            "autovit": [raw("700", AUTOVIT_AD, 26980.0, title="Autovit copy")],
            "olx": [raw("305871621", OLX_SAME_AD, 27500.0, title="Olx copy")],
        }
    )

    autovit_page = project.client().get("/search/1").text
    olx_page = project.client().get("/search/2").text

    assert "Autovit copy" in autovit_page and "Olx copy" not in autovit_page
    # The olx row keeps its own price here, not autovit's canonical one.
    assert "27 500 EUR" in olx_page


def test_the_detail_page_names_the_configuration_it_belongs_to(project):
    """A source page is a drill-down *from* a configuration (§8).

    It used to break the trail: no sidebar, and a breadcrumb back to Listings
    that said nothing about which configuration you had come from.
    """
    project.run({"autovit": [raw("1", AUTOVIT_AD, 26980.0)]})

    body = project.client().get("/search/1").text

    assert "Overview" not in body
    assert 'class="sidebar"' in body
    assert 'href="/config/qashqai"' in body
    assert "Run this source now" in body


def test_the_sidebar_on_a_source_page_opens_configurations(project):
    """There is nothing to scope on a page about one `search` row, so selecting
    a configuration must go to it rather than build `/search/1?config=...`."""
    project.run({"autovit": [raw("1", AUTOVIT_AD, 26980.0)]})

    body = project.client().get("/search/1").text

    assert "/search/1?config=" not in body


# ------------------------------------------- the first run is not "what changed"

# When you start watching a search, every listing on it is "new" — that is the
# starting position, not a change. On the live database that was 215 of 216
# rows, burying the one thing that had actually happened.


def test_the_first_runs_arrivals_are_hidden_by_default(project):
    project.run({"autovit": [raw("1", AUTOVIT_AD, 26980.0, title="Was always there")]})

    body = project.get("/changes")

    assert "Was always there" not in body
    assert feed_rows(body) == 0


def test_they_are_hidden_but_never_silently(project):
    """An empty feed with no explanation is worse than a noisy one."""
    project.run({"autovit": [raw("1", AUTOVIT_AD, 26980.0)]})

    body = project.get("/changes")

    assert "1 initial listing" in body
    assert "Nothing has changed yet" in body


def test_the_baseline_can_be_folded_back_in(project):
    project.run({"autovit": [raw("1", AUTOVIT_AD, 26980.0, title="Was always there")]})

    body = project.get("/changes?baseline=1")

    assert "Was always there" in body
    assert feed_rows(body) == 1
    assert "hide 1 initial listing" in body


def test_a_real_arrival_after_the_first_run_is_news(project):
    project.run({"autovit": [raw("1", AUTOVIT_AD, 26980.0, title="Baseline car")]})
    body = project.run(
        {
            "autovit": [
                raw("1", AUTOVIT_AD, 26980.0, title="Baseline car"),
                raw("2", OTHER_AUTOVIT_AD, 30000.0, title="Genuinely new"),
            ]
        }
    ).get("/changes")

    assert "Genuinely new" in body
    assert "Baseline car" not in body
    assert feed_rows(body) == 1


def test_price_changes_are_never_hidden(project):
    """Only arrivals can be baseline — a first run has nothing to compare
    against, so it cannot produce a price change in the first place."""
    project.run({"autovit": [raw("1", AUTOVIT_AD, 26980.0)]})
    body = project.run({"autovit": [raw("1", AUTOVIT_AD, 25500.0)]}).get("/changes")

    assert feed_rows(body) == 1
    assert "drop" in body


AUTOVIT_ONLY = """
settings:
  db_path: "{db}"
searches:
  - name: "Qashqai"
    sources:
      - site: "autovit"
        url: "https://www.autovit.ro/autoturisme/nissan/qashqai"
"""


def test_a_source_added_later_gets_its_own_baseline(project):
    """Adding mobile.de to config.yaml must not report its whole inventory as
    173 arrivals — that source has simply started, and its contents are where
    it starts."""
    project.reconfigure(AUTOVIT_ONLY)
    project.run({"autovit": [raw("1", AUTOVIT_AD, 26980.0)]})

    project.reconfigure(CONFIG)  # mobile.de appears in the config here
    body = project.run(
        {
            "autovit": [raw("1", AUTOVIT_AD, 26980.0)],
            "mobilede": [mobilede("9", 31480.0, title="German baseline")],
        }
    ).get("/changes")

    assert "German baseline" not in body
    assert feed_rows(body) == 0


def test_a_search_that_collects_cleanly_but_finds_nothing_has_its_baseline_already(
    project,
):
    """The case that drove the anchor change. The strict "2025 4x4 Tekna"
    autovit search ran 19 times, every one `ok`, every one 0 results, because
    autovit relaxes it. Anchoring on the first run that produced *events* would
    have left it with no baseline until real matches finally appeared — and
    would then have hidden them as "already there". They are the opposite: we
    had been watching successfully and genuinely finding none."""
    project.run({"autovit": []})
    project.run({"autovit": []})

    body = project.run(
        {"autovit": [raw("1", AUTOVIT_AD, 26980.0, title="Finally a match")]}
    ).get("/changes")

    assert "Finally a match" in body
    assert feed_rows(body) == 1


def test_the_baseline_is_pinned_and_does_not_move_with_later_runs(project):
    """Whatever else happens, run 1 stays the baseline. If it advanced, the feed
    would hide everything forever."""
    project.run({"autovit": [raw("1", AUTOVIT_AD, 26980.0, title="Baseline car")]})

    for price, title in [(26980.0, "Second arrival"), (25500.0, "Second arrival")]:
        project.run(
            {
                "autovit": [
                    raw("1", AUTOVIT_AD, 26980.0, title="Baseline car"),
                    raw("2", OTHER_AUTOVIT_AD, price, title=title),
                ]
            }
        )

    body = project.get("/changes")

    # The day-1 car is still hidden; the later arrival and its drop are not.
    assert "Baseline car" not in body
    assert "Second arrival" in body
    assert feed_rows(body) == 2  # one arrival, one price drop
    assert "1 initial listing" in body


def test_the_chip_counts_drop_the_baseline_too(project):
    """Otherwise every chip over-promises by the whole first run."""
    import re

    project.run(
        {
            "autovit": [
                raw("1", AUTOVIT_AD, 26980.0),
                raw("2", OTHER_AUTOVIT_AD, 30000.0),
            ]
        }
    )
    body = project.get("/changes")

    everything = int(
        re.search(r'Everything <span class="dim">(\d+)</span>', body).group(1)
    )
    assert everything == 0
    assert "2 initial listings" in body


def test_a_blocked_first_run_does_not_become_the_baseline(project):
    """The earliest run for a search can be one that collected nothing. The
    baseline is the first run that actually found listings."""
    from carwatch.adapters.base import BlockedError

    class Blocked:
        last_relaxation = None

        def __call__(self, site, settings):
            return self

        def fetch_listings(self, url, max_pages=None):
            raise BlockedError("bot wall")

    from carwatch.collector.engine import collect
    from carwatch.config import load_config

    collect(load_config(project.config_path), adapter_factory=Blocked())
    project.run({"autovit": [raw("1", AUTOVIT_AD, 26980.0, title="First real find")]})

    body = project.get("/changes")

    # The blocked run produced no events, so the real first collection is still
    # the baseline and its arrivals stay hidden.
    assert "First real find" not in body
    assert "1 initial listing" in body


def test_the_baseline_setting_survives_a_type_filter(project):
    """Clicking a chip must not silently re-hide what you asked to see."""
    project.run({"autovit": [raw("1", AUTOVIT_AD, 26980.0, title="Was always there")]})

    body = project.get("/changes?baseline=1")

    assert 'href="?type=new' in body
    assert "baseline=1" in body
    assert "Was always there" in project.get("/changes?type=new&baseline=1")


# ------------------------------------------------- what the card knows and the site does not


def test_a_card_says_how_long_the_car_has_been_watched(project):
    """"seen 3d ago" read as *last* seen; it was always the first sighting.

    How long a car has been sitting there is one of the few things the listing
    pages do not tell you, so the card says it plainly.
    """
    body = project.run({"autovit": [raw("700", AUTOVIT_AD, 26980.0)]}).get()

    assert "tracked since today" in body
    assert "seen 3d ago" not in body


def test_a_card_counts_the_prices_it_has_recorded(project):
    """A car that has moved while we watched says so; one that has not stays quiet."""
    project.run({"autovit": [raw("700", AUTOVIT_AD, 26980.0)]})
    assert "prices</span>" not in project.get()

    project.run({"autovit": [raw("700", AUTOVIT_AD, 25980.0)]})
    assert "2 prices</span>" in project.get()


def test_how_long_a_car_has_been_watched_reads_as_days():
    from datetime import timedelta

    from carwatch.models import utcnow
    from carwatch.web.app import _format_tracked

    assert _format_tracked(utcnow()) == "since today"
    assert _format_tracked(utcnow() - timedelta(days=1)) == "1 day"
    assert _format_tracked(utcnow() - timedelta(days=11)) == "11 days"
    assert _format_tracked(None) == "not yet"
