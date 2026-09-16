"""Dashboard tests — render every page against a real SQLite DB."""

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
    make: "Nissan"
    year_min: 2024
    sources:
      - site: "autovit"
        url: "https://www.autovit.ro/autoturisme/nissan/qashqai"
"""


class FakeAdapter:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.last_relaxation = None

    def __call__(self, site, settings):
        return self

    def fetch_listings(self, url, max_pages=None):
        out = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(out, Exception):
            raise out
        return out


def raw(ad_id, price, **kw):
    kw.setdefault("title", f"Nissan Qashqai {ad_id}")
    kw.setdefault("year", 2024)
    kw.setdefault("mileage_km", 40000)
    kw.setdefault("location", "Bucuresti")
    return RawListing(
        site_listing_id=ad_id,
        url=f"https://www.autovit.ro/anunt/ID{ad_id}.html",
        currency="EUR",
        price=price,
        **kw,
    )


@pytest.fixture
def app_and_config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(CONFIG).format(db=(tmp_path / "t.db").as_posix()),
        encoding="utf-8",
    )
    config = load_config(path)
    return path, config


@pytest.fixture
def client(app_and_config):
    path, _ = app_and_config
    return TestClient(create_app(path))


def seed(config, script):
    return collect(config, adapter_factory=FakeAdapter(script))


# ----------------------------------------------------------------- empty state


def test_listings_page_renders_with_no_data(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Listings" in r.text
    assert "Nothing collected" in r.text


def test_source_health_moved_from_runs_to_the_configuration_page(client):
    """It went Overview -> Runs -> the configuration it describes (§8)."""
    r = client.get("/config/qashqai")
    assert r.status_code == 200
    assert "never run" in r.text

    runs = client.get("/runs")
    assert runs.status_code == 200
    assert "never run" not in runs.text
    assert 'href="/config/qashqai"' in runs.text


def test_runs_page_renders_with_no_data(client):
    r = client.get("/runs")
    assert r.status_code == 200
    assert "No runs yet" in r.text


# ---------------------------------------------------------------- with data


def test_runs_page_shows_source_health_after_a_run(app_and_config):
    path, config = app_and_config
    seed(config, [[raw("1", 20000), raw("2", 18000)]])
    client = TestClient(create_app(path))

    r = client.get("/runs")
    assert r.status_code == 200
    assert "Qashqai" in r.text
    assert "autovit" in r.text
    assert ">ok<" in r.text


def test_detail_lists_active_listings(app_and_config):
    path, config = app_and_config
    seed(config, [[raw("1", 20000, title="Tekna 4x4"), raw("2", 18000)]])
    client = TestClient(create_app(path))

    r = client.get("/search/1")
    assert r.status_code == 200
    assert "Tekna 4x4" in r.text
    assert "20 000 EUR" in r.text
    assert "autovit.ro/anunt/ID1.html" in r.text


def test_detail_shows_price_drop(app_and_config):
    path, config = app_and_config
    adapter = FakeAdapter([[raw("1", 20000)], [raw("1", 18500)]])
    collect(config, adapter_factory=adapter)
    collect(config, adapter_factory=adapter)
    client = TestClient(create_app(path))

    r = client.get("/search/1")
    assert "18 500 EUR" in r.text
    assert "▼" in r.text          # drop marker
    assert "1 500" in r.text      # the delta
    assert "2 prices" in r.text   # history depth


def test_detail_shows_delisted_section(app_and_config):
    path, config = app_and_config
    adapter = FakeAdapter([[raw("1", 20000), raw("2", 18000)], [raw("1", 20000)]])
    collect(config, adapter_factory=adapter)
    collect(config, adapter_factory=adapter)
    client = TestClient(create_app(path))

    r = client.get("/search/1")
    assert "Recently delisted" in r.text


def test_404_for_unknown_search(client):
    assert client.get("/search/999").status_code == 404


# ------------------------------------------------------------ sort / filter


def test_sorting_by_price_orders_rows(app_and_config):
    path, config = app_and_config
    seed(config, [[raw("cheap", 9000), raw("dear", 30000)]])
    client = TestClient(create_app(path))

    # Match on the per-listing URLs, which appear only in the table body. The
    # prices themselves also appear in the summary header ("9 000–30 000"),
    # which would make any price-based position check meaningless.
    asc = client.get("/search/1?sort=price&dir=asc").text
    desc = client.get("/search/1?sort=price&dir=desc").text
    assert asc.index("IDcheap") < asc.index("IDdear")
    assert desc.index("IDdear") < desc.index("IDcheap")


def test_unknown_sort_key_falls_back_safely(app_and_config):
    path, config = app_and_config
    seed(config, [[raw("1", 20000)]])
    client = TestClient(create_app(path))
    assert client.get("/search/1?sort=;drop table listing").status_code == 200


def test_price_max_filter(app_and_config):
    path, config = app_and_config
    seed(config, [[raw("cheap", 9000), raw("dear", 30000)]])
    client = TestClient(create_app(path))

    r = client.get("/search/1?price_max=10000")
    assert "9 000" in r.text
    assert "30 000" not in r.text


def test_text_filter_matches_title(app_and_config):
    path, config = app_and_config
    seed(config, [[raw("1", 9000, title="Tekna 4x4"), raw("2", 30000, title="Acenta")]])
    client = TestClient(create_app(path))

    r = client.get("/search/1?q=tekna")
    assert "Tekna 4x4" in r.text
    assert "Acenta" not in r.text


def test_year_min_filter(app_and_config):
    path, config = app_and_config
    seed(config, [[raw("1", 9000, year=2023), raw("2", 30000, year=2025)]])
    client = TestClient(create_app(path))

    r = client.get("/search/1?year_min=2025")
    assert "2025" in r.text
    assert "9 000" not in r.text


# ------------------------------------------------------------------- notes


def test_blocked_run_note_is_surfaced(app_and_config):
    """A blocked run must be visible in the UI, not silently look like 0 results."""
    from carwatch.adapters.base import BlockedError

    path, config = app_and_config
    adapter = FakeAdapter([[raw("1", 20000)], BlockedError("bot wall")])
    collect(config, adapter_factory=adapter)
    collect(config, adapter_factory=adapter)
    client = TestClient(create_app(path))

    # The Listings page shows cars, not run status; a blocked source has to be
    # visible somewhere, and that somewhere is now Runs.
    assert "bot wall" in client.get("/runs").text


def test_runs_page_lists_runs(app_and_config):
    path, config = app_and_config
    seed(config, [[raw("1", 20000)]])
    client = TestClient(create_app(path))

    r = client.get("/runs")
    assert r.status_code == 200
    assert "Qashqai" in r.text
    assert ">ok<" in r.text


# ==========================================================================
# Phase 6 — changes feed, price-history charts, "Run now"
# ==========================================================================


def seed_with_changes(config):
    """Two runs producing a drop, a rise, a new listing and a delisting."""
    adapter = FakeAdapter([
        [raw("drop", 20000), raw("rise", 10000), raw("gone", 15000)],
        [raw("drop", 18500), raw("rise", 11000), raw("fresh", 9000)],
    ])
    collect(config, adapter_factory=adapter)
    collect(config, adapter_factory=adapter)


# ------------------------------------------------------------ changes feed


def test_changes_feed_empty_state(client):
    r = client.get("/changes")
    assert r.status_code == 200
    assert "Nothing here yet" in r.text


def test_changes_feed_lists_all_event_types(app_and_config):
    path, config = app_and_config
    seed_with_changes(config)
    client = TestClient(create_app(path))

    r = client.get("/changes")
    assert r.status_code == 200
    assert "drop" in r.text and "rise" in r.text
    assert "delisted" in r.text
    # Deltas, with direction markers.
    assert "1 500" in r.text     # 20000 -> 18500
    assert "1 000" in r.text     # 10000 -> 11000
    assert "▼" in r.text and "▲" in r.text


def test_changes_feed_filters_to_drops_only(app_and_config):
    path, config = app_and_config
    seed_with_changes(config)
    client = TestClient(create_app(path))

    r = client.get("/changes?type=drops")
    assert "IDdrop" in r.text
    assert "IDrise" not in r.text
    assert "IDfresh" not in r.text


def test_changes_feed_scopes_to_a_configuration(app_and_config):
    """The sidebar's selection, carried in `?config=` (configurations plan §5)."""
    path, config = app_and_config
    seed_with_changes(config)
    client = TestClient(create_app(path))

    assert client.get("/changes?type=all&config=qashqai").status_code == 200

    # An unknown slug is the "all configurations" scope, not an error page.
    r = client.get("/changes?type=all&config=no-such-configuration")
    assert r.status_code == 200
    assert "Nothing here yet" not in r.text


def test_changes_feed_is_reverse_chronological(app_and_config):
    path, config = app_and_config
    seed_with_changes(config)
    client = TestClient(create_app(path))

    # baseline=1 because the run-1 arrivals are hidden by default now, and this
    # test is about ordering rather than about that rule.
    r = client.get("/changes?type=new&baseline=1")
    # "fresh" appeared in run 2, the others in run 1, so it must come first.
    assert r.text.index("IDfresh") < r.text.index("IDdrop")


# ---------------------------------------------------------- price history


def test_history_fragment_renders_chart_for_changed_price(app_and_config):
    path, config = app_and_config
    seed_with_changes(config)
    client = TestClient(create_app(path))

    r = client.get("/listing/1/history")
    assert r.status_code == 200
    assert "<canvas" in r.text
    assert "20000" in r.text and "18500" in r.text     # both points in the series
    assert "chart.umd.min.js" in r.text


def test_history_fragment_explains_single_observation(app_and_config):
    path, config = app_and_config
    seed(config, [[raw("1", 20000)]])
    client = TestClient(create_app(path))

    r = client.get("/listing/1/history")
    assert "Only one price recorded" in r.text
    assert "<canvas" not in r.text


def test_history_404_for_unknown_listing(client):
    assert client.get("/listing/999/history").status_code == 404


def test_detail_rows_have_expand_buttons(app_and_config):
    path, config = app_and_config
    seed(config, [[raw("1", 20000)]])
    client = TestClient(create_app(path))

    r = client.get("/search/1")
    assert 'hx-get="/listing/1/history"' in r.text


# ----------------------------------------------------------------- run now


def test_run_now_starts_a_collection(app_and_config, monkeypatch):
    path, config = app_and_config
    app = create_app(path)
    monkeypatch.setattr(
        "carwatch.web.jobs.collect",
        lambda cfg, search_names=None, sites=None, **kw: collect(
            cfg, search_names, sites, FakeAdapter([[raw("1", 20000)]])
        ),
    )
    client = TestClient(app)

    r = client.post("/run")
    assert r.status_code == 200

    app.state.jobs.wait(timeout=20)
    job = app.state.jobs.latest()
    assert job is not None
    assert job.running is False
    assert job.totals["found"] == 1
    assert job.totals["new"] == 1

    # And the listing really landed in the DB.
    assert "20 000 EUR" in client.get("/search/1").text


def test_run_status_reports_finished_run(app_and_config, monkeypatch):
    path, config = app_and_config
    app = create_app(path)
    monkeypatch.setattr(
        "carwatch.web.jobs.collect",
        lambda cfg, search_names=None, sites=None, **kw: collect(
            cfg, search_names, sites, FakeAdapter([[raw("1", 20000)]])
        ),
    )
    client = TestClient(app)
    client.post("/run")
    app.state.jobs.wait(timeout=20)

    r = client.get("/run/status")
    assert "done" in r.text
    assert "1 new" in r.text
    # Finished jobs must stop the htmx poll.
    assert "every 2s" not in r.text


def test_run_status_is_empty_before_any_run(client):
    r = client.get("/run/status")
    assert r.status_code == 200
    assert "every 2s" not in r.text


def test_run_now_for_one_search_only(app_and_config, monkeypatch):
    path, config = app_and_config
    app = create_app(path)
    seen = {}

    def fake_collect(cfg, search_names=None, sites=None, **kw):
        seen["names"], seen["sites"] = search_names, sites
        return []

    monkeypatch.setattr("carwatch.web.jobs.collect", fake_collect)
    client = TestClient(app)

    client.post("/run", data={"search_id": "1"})
    app.state.jobs.wait(timeout=20)

    assert seen["names"] == ["Qashqai"]
    assert seen["sites"] == ["autovit"]


def test_run_now_404_for_unknown_search(client):
    assert client.post("/run", data={"search_id": "999"}).status_code == 404


def test_second_press_joins_the_running_job(app_and_config, monkeypatch):
    """§14 asks us not to parallelise scraping, and two concurrent runs would
    race on the same listing rows."""
    import threading

    path, config = app_and_config
    app = create_app(path)
    release = threading.Event()
    calls = []

    def slow_collect(cfg, search_names=None, sites=None, **kw):
        calls.append(1)
        release.wait(timeout=10)
        return []

    monkeypatch.setattr("carwatch.web.jobs.collect", slow_collect)
    client = TestClient(app)

    client.post("/run")
    first = app.state.jobs.latest().id
    client.post("/run")           # pressed again while still running
    second = app.state.jobs.latest().id

    assert first == second
    release.set()
    app.state.jobs.wait(timeout=20)
    assert len(calls) == 1


def test_running_job_reports_progress_and_keeps_polling(app_and_config, monkeypatch):
    import threading

    path, config = app_and_config
    app = create_app(path)
    release = threading.Event()

    monkeypatch.setattr(
        "carwatch.web.jobs.collect",
        lambda cfg, search_names=None, sites=None, **kw: (release.wait(timeout=10), [])[1],
    )
    client = TestClient(app)
    client.post("/run")

    r = client.get("/run/status")
    assert "Collecting" in r.text
    assert "every 2s" in r.text      # still polling

    release.set()
    app.state.jobs.wait(timeout=20)


def test_job_failure_is_reported_not_swallowed(app_and_config, monkeypatch):
    path, config = app_and_config
    app = create_app(path)

    def boom(cfg, search_names=None, sites=None, **kw):
        raise RuntimeError("collector exploded")

    monkeypatch.setattr("carwatch.web.jobs.collect", boom)
    client = TestClient(app)
    client.post("/run")
    app.state.jobs.wait(timeout=20)

    r = client.get("/run/status")
    assert "Run failed" in r.text
    assert "collector exploded" in r.text
