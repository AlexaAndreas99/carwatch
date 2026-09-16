"""Configurations and the sidebar (configurations plan §5).

The health dot is the part with a real problem behind it: for four days a
configuration that autovit had quietly relaxed returned nothing on every run
and looked, in the UI, exactly like a healthy one. Most of what follows is
about making that distinguishable.
"""

import re
import textwrap
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from carwatch.adapters.base import BlockedError, RawListing
from carwatch.collector.engine import collect
from carwatch.config import load_config
from carwatch.config_writer import archive_search
from carwatch.db import get_engine
from carwatch.models import Listing, PriceHistory
from carwatch.web import configurations
from carwatch.web.app import create_app

CONFIG = """
settings:
  db_path: "{db}"
searches:
  - name: "Qashqai 2024+"
    make: "Nissan"
    year_min: 2024
    sources:
      - site: "autovit"
        url: "https://www.autovit.ro/autoturisme/nissan/qashqai/de-la-2024"
      - site: "olx"
        url: "https://www.olx.ro/autoturisme/nissan/"

  - name: "Juke"
    make: "Nissan"
    sources:
      - site: "autovit"
        url: "https://www.autovit.ro/autoturisme/nissan/juke"
"""


class PerSiteAdapter:
    """Scripted results per (search name, site), so one source can fail alone."""

    def __init__(self, by_site, relaxed_sites=()):
        self.by_site = by_site
        self.relaxed_sites = set(relaxed_sites)
        self.site = None
        self.last_relaxation = None

    def __call__(self, site, settings):
        self.site = site
        self.last_relaxation = (
            {"rule": "widened year", "applied": True}
            if site in self.relaxed_sites
            else None
        )
        return self

    def fetch_listings(self, url, max_pages=None):
        out = self.by_site.get(self.site, [])
        if isinstance(out, Exception):
            raise out
        return out


def raw(ad_id, price=25000.0, **kw):
    kw.setdefault("title", f"Nissan {ad_id}")
    kw.setdefault("year", 2024)
    kw.setdefault("url", f"https://www.autovit.ro/anunt/ID{ad_id}.html")
    return RawListing(
        site_listing_id=ad_id, currency="EUR", price=price, **kw
    )


@pytest.fixture
def project(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(CONFIG).format(db=(tmp_path / "t.db").as_posix()),
        encoding="utf-8",
    )
    config = load_config(path)

    class Project:
        db = config.db_file

        def run(self, by_site, relaxed_sites=()):
            return collect(
                config, adapter_factory=PerSiteAdapter(by_site, relaxed_sites)
            )

        def client(self):
            return TestClient(create_app(path))

        def get(self, url="/"):
            return self.client().get(url).text

        def index(self):
            # Through the app, so the `search` rows exist even when nothing has
            # collected yet — that is the "never run" case.
            self.client()
            with Session(get_engine(config.db_file)) as session:
                return configurations.load_configurations(session)

        def by_name(self, name):
            return next(c for c in self.index().configurations if c.name == name)

        def backdate(self, days):
            """Push everything collected so far into the past.

            A trend needs more than one day, and a test cannot wait for one.
            """
            shift = timedelta(days=days)
            with Session(get_engine(config.db_file)) as session:
                for row in session.exec(select(Listing)):
                    row.first_seen -= shift
                    session.add(row)
                for row in session.exec(select(PriceHistory)):
                    row.observed_at -= shift
                    session.add(row)
                session.commit()

        def archive(self, name):
            """Archive through config.yaml, which is the only way that sticks.

            Disabling the `search` rows directly does not: the app re-syncs
            config.yaml into them on every startup, which is exactly the
            property that lets config.yaml stay the single source of truth.
            """
            archive_search(path, name)

    return Project()


# ------------------------------------------------------------------ slugs


@pytest.mark.parametrize(
    "name, slug",
    [
        ("Nissan Qashqai 2024+ (any trim)", "nissan-qashqai-2024-any-trim"),
        ("Qashqai 2025 4x4 Tekna", "qashqai-2025-4x4-tekna"),
        ("Dacia Sandero Stepway (bucureşti)", "dacia-sandero-stepway-bucuresti"),
        ("   ", "configuration"),
        ("+++", "configuration"),
    ],
)
def test_slugify(name, slug):
    """Accent-folded and punctuation-free: a URL you can read and retype."""
    assert configurations.slugify(name) == slug


def test_colliding_slugs_are_disambiguated(project):
    """Two names can slug identically, and a collision would silently scope the
    page to the wrong configuration."""
    project.run({"autovit": [raw("1")]})

    made_up = [
        configurations.Configuration(name="Qashqai 2024+", slug="qashqai-2024"),
        configurations.Configuration(name="Qashqai 2024", slug="qashqai-2024"),
    ]
    configurations._disambiguate(made_up)
    assert len({c.slug for c in made_up}) == 2


# ----------------------------------------------------------------- grouping


def test_one_configuration_groups_its_per_site_rows(project):
    project.run({"autovit": [raw("1")], "olx": [raw("2")]})

    wide = project.by_name("Qashqai 2024+")
    assert sorted(wide.sites) == ["autovit", "olx"]
    assert len(wide.search_ids) == 2


def test_the_car_count_is_cars_not_rows(project):
    """The same ad on autovit and olx is one car, as on the Listings page."""
    same_ad = "https://www.autovit.ro/autoturisme/anunt/nissan-ID7HQf7M.html"
    project.run(
        {
            "autovit": [raw("700", url=same_ad)],
            "olx": [raw("305871621", url=same_ad)],
        }
    )
    assert project.by_name("Qashqai 2024+").car_count == 1


def test_metadata_comes_from_the_config_block(project):
    project.run({"autovit": [raw("1")]})
    assert project.by_name("Qashqai 2024+").metadata == {
        "make": "Nissan",
        "year_min": 2024,
    }


# ------------------------------------------------------------- health dots


def test_a_collecting_configuration_is_green(project):
    project.run({"autovit": [raw("1")], "olx": [raw("2")]})
    assert project.by_name("Qashqai 2024+").health == configurations.HEALTH_OK


def test_a_source_that_found_nothing_is_amber(project):
    """The silent-zero alarm. A configuration returning nothing looked exactly
    like a healthy one, which is the problem this whole feature answers."""
    project.run({"autovit": [], "olx": []})

    wide = project.by_name("Qashqai 2024+")
    assert wide.health == configurations.HEALTH_WARN
    assert "found nothing" in wide.health_title


def test_a_relaxed_search_is_amber_and_says_so(project):
    """autovit widens a search it would otherwise answer with zero, and the
    adapter discards the non-matching results. Found-nothing is the symptom;
    the relaxation is the cause, and the cause is what the tooltip says."""
    project.run({"autovit": [], "olx": [raw("2")]}, relaxed_sites={"autovit"})

    wide = project.by_name("Qashqai 2024+")
    assert wide.health == configurations.HEALTH_WARN
    assert "relaxed" in wide.health_title


def test_a_blocked_source_is_red(project):
    project.run({"autovit": BlockedError("bot wall"), "olx": [raw("2")]})

    wide = project.by_name("Qashqai 2024+")
    assert wide.health == configurations.HEALTH_BAD
    assert "blocked" in wide.health_title


def test_red_outranks_amber(project):
    """One bad source makes the configuration bad: the dot reports the loudest
    thing wrong, not an average."""
    project.run({"autovit": BlockedError("bot wall"), "olx": []})
    assert project.by_name("Qashqai 2024+").health == configurations.HEALTH_BAD


def test_a_never_run_configuration_is_neither_good_nor_bad(project):
    """Nothing has been tried yet, so neither green nor amber would be honest."""
    assert project.by_name("Juke").health == configurations.HEALTH_NONE


def test_an_archived_configuration_is_amber(project):
    project.run({"autovit": [raw("1")], "olx": [raw("2")]})
    project.archive("Qashqai 2024+")

    wide = project.by_name("Qashqai 2024+")
    assert wide.enabled is False
    assert wide.health == configurations.HEALTH_WARN
    assert "archived" in wide.health_title


def test_a_healthy_configuration_says_so(project):
    project.run({"autovit": [raw("1")], "olx": [raw("2")]})
    assert project.by_name("Qashqai 2024+").health_title == "collecting cleanly"


# -------------------------------------------------------------- the sidebar


def test_the_sidebar_appears_on_both_scoped_pages(project):
    project.run({"autovit": [raw("1")]})

    for page in ("/", "/changes"):
        body = project.get(page)
        assert 'class="sidebar"' in body
        assert "All configurations" in body
        assert "Qashqai 2024+" in body


def test_the_total_is_cars_counted_once_not_the_sum_of_the_rows(project):
    """A car matching two configurations belongs to both; adding the per-
    configuration counts would claim more cars than the page can show."""
    both = raw("shared")
    project.run({"autovit": [both]})

    index = project.index()
    assert sum(c.car_count for c in index.configurations) == 2
    assert index.total_cars == 1
    assert ">1</span>" in project.get()


def test_selecting_a_configuration_scopes_the_cars(project):
    """olx is a source of "Qashqai 2024+" and not of "Juke", so a car found
    only there belongs to one configuration and not the other."""
    project.run({"olx": [raw("1", title="Qashqai only")]})

    assert "Qashqai only" in project.get("/?config=qashqai-2024")
    assert "Qashqai only" not in project.get("/?config=juke")


def test_the_scope_survives_a_filter_and_a_market_switch(project):
    """The whole point of carrying it in the URL: every link the page builds
    keeps the selection."""
    project.run({"autovit": [raw("1")]})

    body = project.get("/?config=juke")
    assert 'name="config" value="juke"' in body  # the toolbar carries it
    assert "config=juke" in body  # and so do the market tabs


def test_an_unknown_slug_is_the_all_scope_not_a_404(project):
    project.run({"autovit": [raw("1")]})
    assert project.client().get("/?config=deleted-last-week").status_code == 200


def test_a_configuration_can_be_selected_by_name_too(project):
    """Hand-typed URLs, and links kept from the old `?search=<name>` filter."""
    project.run({"autovit": [raw("1")]})
    index = project.index()
    assert configurations.find(index.configurations, "Juke").slug == "juke"


# ------------------------------------------------------ archived, and hiding


def test_archived_configurations_are_hidden_by_default(project):
    project.run({"autovit": [raw("1")]})
    project.archive("Juke")

    body = project.get()
    assert 'class="config-name">Juke<' not in body
    assert "show 1 archived" in body


def test_the_toggle_shows_them(project):
    project.run({"autovit": [raw("1")]})
    project.archive("Juke")

    body = project.get("/?disabled=1")
    assert 'class="config-name">Juke<' in body
    assert "hide archived" in body


def test_a_selected_archived_configuration_is_always_listed(project):
    """Scoping to something the sidebar does not show reads as a lost selection."""
    project.run({"autovit": [raw("1")]})
    project.archive("Juke")

    body = project.get("/?config=juke")
    assert 'class="config-name">Juke<' in body


# ------------------------------------------------- the configuration page


def test_the_configuration_page_shows_its_sources_and_health(project):
    project.run({"autovit": [], "olx": [raw("2")]}, relaxed_sites={"autovit"})

    body = project.get("/config/qashqai-2024")

    assert "Qashqai 2024+" in body
    assert "Sources" in body
    assert "AUTOVIT" in body and "OLX" in body
    # The relaxation, stated on the page rather than buried in a run's note.
    assert "relaxed this search" in body


def test_the_configuration_page_shows_its_cars_and_changes(project):
    project.run({"olx": [raw("1", 25000, title="Qashqai only")]})
    project.run({"olx": [raw("1", 23000, title="Qashqai only")]})

    body = project.get("/config/qashqai-2024")

    assert "Qashqai only" in body
    assert "Its cars" in body
    assert "Recent changes" in body
    assert "drop" in body


def test_the_configuration_page_does_not_show_another_configurations_cars(project):
    project.run({"olx": [raw("1", title="Only in Qashqai")]})
    assert "Only in Qashqai" not in project.get("/config/juke")


def test_the_configuration_page_404s_for_an_unknown_slug(project):
    """Unlike `?config=`, a page *about* a configuration has nothing to show
    when there isn't one."""
    project.run({"autovit": [raw("1")]})
    assert project.client().get("/config/no-such-thing").status_code == 404


def test_an_archived_configuration_still_has_a_page(project):
    """Archiving hides it from the sidebar; its history stays reachable."""
    project.run({"autovit": [raw("1")]})
    project.archive("Juke")

    body = project.get("/config/juke")
    assert "archived" in body


def test_the_page_links_back_to_the_scoped_listings_and_changes(project):
    project.run({"autovit": [raw("1")]})

    body = project.get("/config/juke")
    assert 'href="/?config=juke"' in body
    assert 'href="/changes?config=juke"' in body


def test_the_median_comparison_is_withheld_without_enough_history(project):
    """Two cars a week apart is not a market. Saying "down 4 000" off that is
    worse than saying nothing."""
    project.run({"olx": [raw("1", 25000)]})

    body = project.get("/config/qashqai-2024")
    assert "not enough history yet" in body
    assert "in 7 days" not in body


# ------------------------------------------------- price trend and "also in"


def test_the_trend_needs_more_than_a_couple_of_cars(project):
    """A median over two ads is one seller's opinion. Such a day is left out
    rather than drawn as a spike."""
    project.run({"olx": [raw("1", 25000), raw("2", 26000)]})

    body = project.get("/config/qashqai-2024")
    assert "A trend needs a few days of history" in body


def test_the_trend_draws_once_there_are_enough_days(project):
    """A week of history is what made "is this segment getting cheaper?"
    answerable at all (§9.3)."""
    project.run({"olx": [raw(str(i), 25000 + i * 100) for i in range(6)]})
    project.backdate(4)
    project.run({"olx": [raw(str(i), 24000 + i * 100) for i in range(6)]})

    body = project.get("/config/qashqai-2024")
    assert "Median asking price" in body
    assert "<polyline" in body
    assert "cars today" in body


def test_a_car_in_two_configurations_names_them_both(project):
    """The relationship was already in the data — `search_ids` carries every
    search a car turned up in — but nothing said so (§9.4).

    Unscoped there is no "also" to speak of, so the badge simply names the
    configurations the car matches.
    """
    project.run({"autovit": [raw("1", title="In both")]})

    body = project.get()
    assert 'class="also-in">in' in body
    assert "also in" not in body
    assert 'href="/?config=juke"' in body
    assert 'href="/?config=qashqai-2024"' in body


def test_a_car_in_one_configuration_says_nothing(project):
    """The badge is only worth showing when it names something the view does
    not already imply."""
    project.run({"olx": [raw("1", title="Only one")]})
    assert 'class="also-in"' not in project.get()


def test_a_scoped_view_names_the_other_configuration_not_itself(project):
    project.run({"autovit": [raw("1", title="In both")]})

    body = project.get("/?config=juke")
    assert "also in" in body
    assert 'href="/?config=qashqai-2024"' in body
    assert 'class="also-in">also in\n        <a href="/?config=juke"' not in body


def test_an_archived_configuration_is_not_named_as_also_in(project):
    """It is not collecting the car any more, so saying so would be a claim
    about the past dressed as the present."""
    project.run({"autovit": [raw("1", title="In both")]})
    project.archive("Juke")

    assert 'class="also-in"' not in project.get()


def test_the_sidebar_on_a_configuration_page_opens_the_other_one(project):
    """`/config/A?config=B` would render A and look like the click did nothing,
    so on pages with nothing to scope the sidebar navigates instead."""
    project.run({"autovit": [raw("1")]})

    body = project.get("/config/juke")
    assert 'href="/config/qashqai-2024"' in body
    assert "/config/juke?config=" not in body


@pytest.mark.parametrize("page, prefix", [("/", "/?"), ("/changes", "/changes?")])
def test_the_sidebar_still_scopes_in_place_on_listings_and_changes(project, page, prefix):
    """The two pages that do have something to scope keep scoping.

    Matched on the rows' own hrefs, so this fails if scoping ever starts
    navigating away from a page whose whole point is the filters you set.
    """
    project.run({"autovit": [raw("1")]})

    body = project.get(page + "?config=juke")
    rows = re.findall(r'class="config-link[^"]*"\s+href="([^"]+)"', body)

    assert rows, "the sidebar rendered no rows"
    assert all(href.startswith(prefix) or href == page for href in rows), rows
    assert any("config=qashqai-2024" in href for href in rows)


def test_every_row_scopes_like_the_all_row(project):
    """The whole row scopes, for "All" and for each configuration alike.

    It used to be split — the name opened the configuration, the count beside
    it scoped — so the first row did one thing and the rows under it another.
    """
    project.run({"autovit": [raw("1")]})

    rows = re.findall(r'class="config-link[^"]*"\s+href="([^"]+)"', project.get())

    assert rows == ["/", "/?config=juke", "/?config=qashqai-2024"], rows


def test_the_gear_opens_the_configuration(project):
    project.run({"autovit": [raw("1")]})

    gears = re.findall(r'class="config-open" href="([^"]+)"', project.get())

    assert gears == ["/config/juke", "/config/qashqai-2024"], gears


def test_the_selected_row_toggles_back(project):
    """Clicking the configuration you are already scoped to clears the scope,
    so there is a way out that is not "find the All row"."""
    project.run({"autovit": [raw("1")]})

    body = project.get("/?config=juke")
    scoping = re.findall(r'class="config-link scoping"\s+href="([^"]+)"', body)

    assert scoping == ["/"], scoping


def test_the_row_opens_the_configuration_where_there_is_nothing_to_scope(project):
    """On a configuration's own page the row navigates, and needs no gear."""
    project.run({"autovit": [raw("1")]})

    body = project.get("/config/juke")
    assert 'class="config-link" href="/config/juke"' in body
    assert 'class="config-open" href=' not in body


# -------------------------------------------- a URL we will not fetch, saved


DISALLOWED_CONFIG = """
settings:
  db_path: "{db}"
searches:
  - name: "Hand edited"
    sources:
      - site: "autovit"
        url: "https://www.autovit.ro/autoturisme/nissan?search%5Bfilter_float_price%3Ato%5D=30000"
"""


@pytest.fixture
def hand_edited(tmp_path):
    """A config.yaml with a robots-disallowed URL typed straight in.

    The form would have refused it, but the form is not the only way in — and a
    URL nobody may fetch must not sit there being fetched with nothing said.
    """
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(DISALLOWED_CONFIG).format(db=(tmp_path / "t.db").as_posix()),
        encoding="utf-8",
    )
    return path


def test_a_hand_edited_disallowed_url_still_loads(hand_edited):
    """`load_config` is deliberately untouched: a rule about what we *ought* to
    fetch must never stop the app reading its own configuration."""
    config = load_config(hand_edited)
    assert config.searches[0].sources[0].url


def test_a_saved_disallowed_url_is_shown_as_a_problem(hand_edited):
    """It is red, and the page says why, rather than looking merely empty."""
    client = TestClient(create_app(hand_edited))

    body = client.get("/config/hand-edited").text
    assert "robots.txt disallows" in body
    assert "dot-bad" in body


def test_a_disallowed_url_outranks_the_last_run_verdict(hand_edited):
    """Whatever the last fetch happened to return, a URL we should not be asking
    for is the more important fact about the source."""
    client = TestClient(create_app(hand_edited))
    with Session(get_engine(load_config(hand_edited).db_file)) as session:
        index = configurations.load_configurations(session)

    source = index.configurations[0].sources[0]
    assert source.health == configurations.HEALTH_BAD
    assert "_price" in source.problem
