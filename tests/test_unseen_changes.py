"""The number on the Changes tab: changes you have not looked at yet.

A run in the background — scheduled, or started from another tab — changes
the database with nobody watching. The number is how you find out, and
opening Changes is how it clears.
"""

import re
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
    sources:
      - site: "autovit"
        url: "https://www.autovit.ro/autoturisme/nissan/qashqai"
"""


class Scripted:
    """An adapter that returns whatever listings the test says."""

    def __init__(self, listings):
        self.listings = listings
        self.last_relaxation = None

    def __call__(self, site, settings):
        return self

    def fetch_listings(self, url, max_pages=None):
        return self.listings


def raw(ad_id, price):
    return RawListing(
        site_listing_id=ad_id,
        title=f"Nissan Qashqai {ad_id}",
        url=f"https://www.autovit.ro/anunt/ID{ad_id}.html",
        price=price,
        currency="EUR",
        year=2025,
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
        client = TestClient(create_app(path))

        def run(self, *listings):
            collect(config, adapter_factory=Scripted(list(listings)))

        def badge(self):
            html = self.client.get("/changes/unseen").text
            match = re.search(r'id="changes-badge"[^>]*>([^<]*)</span>', html)
            assert match, html
            return match.group(1).strip()

        def changes(self):
            return self.client.get("/changes").text

    return Project()


def test_every_page_asks_for_the_number(project):
    assert 'hx-get="/changes/unseen"' in project.client.get("/").text


def test_the_first_look_starts_counting_from_then(project):
    """Before anything is recorded, all history would be "unseen" — noise."""
    project.run(raw("1", 25000))
    project.run(raw("1", 24000))

    assert project.badge() == ""


def test_a_run_in_the_background_shows_up_as_a_number(project):
    project.run(raw("1", 25000), raw("2", 30000))
    assert project.badge() == ""

    project.run(raw("1", 24000), raw("2", 30000), raw("3", 27000))

    assert project.badge() == "2"  # one drop, one new car


def test_it_keeps_asking(project):
    assert "every 60s" in project.client.get("/changes/unseen").text


def test_first_run_arrivals_are_not_news(project):
    """The feed hides a search's starting position; so does the number."""
    project.badge()
    project.run(raw("1", 25000), raw("2", 30000))

    assert project.badge() == ""


def test_opening_changes_clears_it_and_marks_what_was_new(project):
    project.run(raw("1", 25000))
    project.badge()
    project.run(raw("1", 24000))
    assert project.badge() == "1"

    page = project.changes()
    assert "feed-row feed-price_down unseen" in page
    assert "1 change</span>" in page and "since your last visit" in page
    assert project.badge() == ""

    # Seen now: the next visit does not mark it again.
    again = project.changes()
    assert "feed-row feed-price_down unseen" not in again
    assert "since your last visit" not in again


def test_a_change_after_the_visit_counts_again(project):
    project.run(raw("1", 25000))
    project.badge()
    project.run(raw("1", 24000))
    project.changes()

    project.run(raw("1", 23000))

    assert project.badge() == "1"
