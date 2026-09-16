"""Filtering as you type, and the Changes feed folded into collections.

Both replace something that worked but read as friction: the toolbar only
applied a text filter when you left the field, which felt like pressing Apply,
and the feed was one flat list of every change ever recorded.
"""

import textwrap
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from carwatch.adapters.base import RawListing
from carwatch.collector.engine import collect
from carwatch.config import load_config
from carwatch.db import get_engine
from carwatch.models import Run
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
"""

HTMX = {"HX-Request": "true"}


class PerSiteAdapter:
    last_relaxation = None

    def __init__(self, by_site):
        self.by_site = by_site
        self.site = None

    def __call__(self, site, settings):
        self.site = site
        return self

    def fetch_listings(self, url, max_pages=None):
        return self.by_site.get(self.site, [])


def raw(ad_id, price=25000.0, **kw):
    kw.setdefault("title", f"Nissan Qashqai {ad_id}")
    kw.setdefault("year", 2025)
    kw.setdefault("url", f"https://www.autovit.ro/anunt/ID{ad_id}.html")
    return RawListing(site_listing_id=ad_id, currency="EUR", price=price, **kw)


@pytest.fixture
def project(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(CONFIG).format(db=(tmp_path / "t.db").as_posix()),
        encoding="utf-8",
    )
    config = load_config(path)

    class Project:
        def run(self, by_site):
            collect(config, adapter_factory=PerSiteAdapter(by_site))
            return self

        def client(self):
            return TestClient(create_app(path))

        def get(self, url="/", **kw):
            return self.client().get(url, **kw)

        def backdate(self, hours):
            """Push every run so far into the past.

            Collections are grouped by how far apart their runs started, and a
            test cannot wait a day between them.
            """
            with Session(get_engine(config.db_file)) as session:
                for row in session.exec(select(Run)):
                    row.started_at -= timedelta(hours=hours)
                    if row.finished_at:
                        row.finished_at -= timedelta(hours=hours)
                    session.add(row)
                session.commit()
            return self

    return Project()


# --------------------------------------------------- filtering as you type


def test_an_htmx_request_returns_only_the_results(project):
    """The toolbar must not be re-rendered: replacing the field you are typing
    in is what takes the cursor out of it."""
    project.run({"autovit": [raw("1")]})

    r = project.get("/?q=qashqai", headers=HTMX)

    assert r.status_code == 200
    assert "<!doctype html>" not in r.text.lower()
    assert 'class="filters toolbar"' not in r.text
    assert 'name="q"' not in r.text
    assert "listing-results" not in r.text  # it *is* the contents of that div


def test_an_ordinary_request_still_returns_the_whole_page(project):
    project.run({"autovit": [raw("1")]})

    body = project.get("/").text
    assert "<!doctype html>" in body.lower()
    assert 'class="filters toolbar"' in body
    assert 'id="listing-results"' in body


def test_the_fragment_filters(project):
    project.run({"autovit": [raw("1", title="Tekna 4x4"), raw("2", title="Acenta")]})

    body = project.get("/?q=tekna", headers=HTMX).text
    assert "Tekna 4x4" in body
    assert "Acenta" not in body


def test_the_fragment_rebuilds_the_market_tabs_out_of_band(project):
    """Each tab carries the filters in its href, so after you type they have to
    be rebuilt — otherwise switching market silently drops what you typed."""
    project.run({"autovit": [raw("1")]})

    body = project.get("/?q=tekna", headers=HTMX).text
    assert 'id="market-tabs"' in body
    assert 'hx-swap-oob="outerHTML"' in body
    assert "q=tekna" in body


def test_the_fragment_rebuilds_the_clear_link_out_of_band(project):
    """It should be there only while something is narrowing the list."""
    project.run({"autovit": [raw("1")]})

    filtered = project.get("/?q=tekna", headers=HTMX).text
    assert 'id="toolbar-clear"' in filtered
    assert 'class="clear"' in filtered

    # An empty toolbar still sends the container, or the stale link would stay.
    unfiltered = project.get("/", headers=HTMX).text
    assert 'id="toolbar-clear"' in unfiltered
    assert 'class="clear"' not in unfiltered


def test_the_pushed_url_is_the_tidy_one(project):
    """htmx would otherwise push the form's own serialisation, empty boxes and
    all. The address bar is read and copied, so it gets the canonical URL."""
    project.run({"autovit": [raw("1")]})

    r = project.get(
        "/?market=ro&q=tekna&price_min=&price_max=&fuel=&sort=newest", headers=HTMX
    )

    pushed = r.headers["HX-Push-Url"]
    assert "q=tekna" in pushed
    assert "price_min=" not in pushed
    assert "sort=" not in pushed  # the default is left out


def test_a_plain_page_load_pushes_nothing(project):
    project.run({"autovit": [raw("1")]})
    assert "HX-Push-Url" not in project.get("/").headers


def test_the_scope_survives_filtering(project):
    """The sidebar's selection rides in the toolbar's hidden fields, so it must
    come back in the pushed URL too."""
    project.run({"autovit": [raw("1")]})

    r = project.get("/?config=qashqai&q=tekna", headers=HTMX)
    assert "config=qashqai" in r.headers["HX-Push-Url"]


# ------------------------------------------- the feed, folded by collection


def test_the_feed_is_grouped_into_collapsible_collections(project):
    project.run({"autovit": [raw("1", 25000)]})
    project.run({"autovit": [raw("1", 23000)]})

    body = project.get("/changes").text
    assert 'class="run-group"' in body
    assert "<summary" in body
    assert "1 change" in body


def test_the_newest_collection_is_open_and_the_rest_are_not(project):
    """The one you came to look at is the newest; the rest fold away."""
    project.run({"autovit": [raw("1", 25000)]})
    project.backdate(30)
    project.run({"autovit": [raw("1", 23000)]})
    project.backdate(20)
    project.run({"autovit": [raw("1", 21000)]})

    body = project.get("/changes").text
    assert body.count('class="run-group"') == 2  # the first run is the baseline
    assert body.count("<details class=\"run-group\" open>") == 1


def test_runs_minutes_apart_are_one_collection(project):
    """A collection runs its sources one after another. Splitting those into
    separate headings would be worse than not grouping at all."""
    project.run({"autovit": [raw("1", 25000)], "olx": [raw("2", 30000)]})
    project.run({"autovit": [raw("1", 23000)], "olx": [raw("2", 28000)]})

    body = project.get("/changes").text
    assert body.count('class="run-group"') == 1


def test_collections_a_day_apart_are_separate(project):
    project.run({"autovit": [raw("1", 25000)]})  # the baseline
    project.backdate(24)

    project.run({"autovit": [raw("1", 23000)]})
    body = project.get("/changes").text
    assert body.count('class="run-group"') == 1

    # Backdated again, or this run would cluster with the one above — which is
    # the behaviour `test_runs_minutes_apart_are_one_collection` pins.
    project.backdate(24)
    project.run({"autovit": [raw("1", 21000)]})

    body = project.get("/changes").text
    assert body.count('class="run-group"') == 2


def test_a_collections_tally_matches_the_rows_inside_it(project):
    project.run({"autovit": [raw("1", 25000), raw("2", 30000)]})
    project.run({"autovit": [raw("1", 23000), raw("2", 28000)]})

    body = project.get("/changes").text
    assert "2 changes" in body
    assert body.count('class="feed-row') == 2


def test_the_empty_state_survives_the_grouping(project):
    """The feed used to key its empty state off the flat row list."""
    project.run({"autovit": [raw("1")]})

    body = project.get("/changes").text
    assert 'class="run-group"' not in body
    assert "Nothing has changed yet" in body or "Nothing here yet" in body
