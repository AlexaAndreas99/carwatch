"""Collection progress, for the dashboard's status strip.

A full collection is about two minutes, most of it mobile.de's deliberate
pauses between pages. The strip used to show a spinner and a timer for all of
it; now it says which source and which page.
"""

import textwrap

import pytest
from fastapi.testclient import TestClient

from carwatch.adapters.base import RawListing, report_page
from carwatch.collector.engine import Progress, collect
from carwatch.config import load_config
from carwatch.web import jobs
from carwatch.web.app import create_app
from carwatch.web.jobs import Job, JobRunner

CONFIG = """
settings:
  db_path: "{db}"
searches:
  - name: "Qashqai"
    sources:
      - site: "autovit"
        url: "https://www.autovit.ro/autoturisme/nissan/qashqai"
      - site: "olx"
        url: "https://www.olx.ro/auto-masini-moto-ambarcatiuni/autoturisme/nissan/"
"""


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(CONFIG).format(db=(tmp_path / "t.db").as_posix()),
        encoding="utf-8",
    )
    return path


class ThreePages:
    """Reports three pages the way a real adapter does, and records what the
    shared Progress said at each one."""

    last_relaxation = None

    def __init__(self, progress):
        self.progress = progress
        self.seen = []

    def __call__(self, site, settings):
        return self

    def fetch_listings(self, url, max_pages=None):
        for page in (1, 2, 3):
            report_page(self, page)
            p = self.progress
            self.seen.append((p.step, p.steps, p.site, p.page))
        return [
            RawListing(
                site_listing_id=url[-6:],
                url="https://example.invalid/1",
                title="Nissan Qashqai",
                price=25000.0,
                currency="EUR",
            )
        ]


def test_progress_follows_each_source_and_its_pages(config_path):
    progress = Progress()
    adapter = ThreePages(progress)

    collect(load_config(config_path), adapter_factory=adapter, progress=progress)

    sites = {site for _, _, site, _ in adapter.seen}
    assert sites == {"autovit", "olx"}
    assert all(steps == 2 for _, steps, _, _ in adapter.seen)
    # Each source counts its own pages from 1.
    assert [page for _, _, _, page in adapter.seen] == [1, 2, 3, 1, 2, 3]
    assert [step for step, _, _, _ in adapter.seen] == [1, 1, 1, 2, 2, 2]


def test_collecting_without_progress_is_unchanged(config_path):
    adapter = ThreePages(Progress())
    summaries = collect(load_config(config_path), adapter_factory=adapter)
    assert all(s.ok for s in summaries)


def test_an_adapter_nobody_is_watching_reports_nowhere():
    report_page(object(), 1)  # no `on_page`: nothing happens, nothing raises


def test_a_broken_progress_callback_never_breaks_a_fetch():
    class Adapter:
        def on_page(self, page):
            raise RuntimeError("the strip is broken")

    report_page(Adapter(), 1)


def test_the_job_runner_hands_its_progress_to_the_collection(config_path, monkeypatch):
    received = {}

    def fake_collect(config, **kwargs):
        received.update(kwargs)
        return []

    monkeypatch.setattr(jobs, "collect", fake_collect)
    runner = JobRunner(load_config(config_path))
    job = runner.start(label="All configurations")
    runner.wait()

    assert received["progress"] is job.progress


def test_the_status_strip_says_where_the_run_has_got_to(config_path):
    client = TestClient(create_app(config_path))
    job = Job(
        id=1,
        label="All configurations",
        search_names=None,
        sites=None,
        progress=Progress(steps=5, step=3, search="Qashqai", site="mobilede", page=4),
    )

    class Runner:
        def latest(self):
            return job

    client.app.state.jobs = Runner()
    body = client.get("/run/status").text

    assert "MOBILE.DE · Qashqai · page 4" in body
    assert "(3 of 5)" in body


def test_refresh_reloads_the_page_rather_than_opening_the_poll(config_path):
    """The strip is rendered by the `/run/status` poll, so a link built from
    the request path pointed at the poll itself — "Refresh page" opened a bare
    status fragment."""
    client = TestClient(create_app(config_path))
    job = Job(id=1, label="All configurations", search_names=None, sites=None)
    job.finished_at = job.started_at

    class Runner:
        def latest(self):
            return job

    client.app.state.jobs = Runner()
    body = client.get("/run/status").text

    assert "Refresh page" in body
    assert 'href="/run/status"' not in body
    assert "window.location.reload()" in body


def test_the_strip_before_the_first_source_starts(config_path):
    client = TestClient(create_app(config_path))
    job = Job(id=1, label="All configurations", search_names=None, sites=None)

    class Runner:
        def latest(self):
            return job

    client.app.state.jobs = Runner()
    body = client.get("/run/status").text

    assert "Collecting All configurations" in body
    assert "of 0" not in body
