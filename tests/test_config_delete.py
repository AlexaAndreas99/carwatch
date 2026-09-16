"""Deleting a configuration, against a real collection's rows.

Deleting is the one thing in CarWatch that destroys price history, so these
seed a database the way collection does — listings, price observations, runs,
change events — and then check both halves of the promise: everything the
configuration owned is gone, and nothing anyone else owned was touched.

The file half (config.yaml, its comments, its backup) is tested in
test_config_writer.py and test_config_forms.py.
"""

import sqlite3
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from carwatch.adapters.base import RawListing
from carwatch.collector.engine import collect
from carwatch.collector.lock import collection_lock
from carwatch.config import load_config
from carwatch.db import (
    KEEP_DB_BACKUPS,
    backup_database,
    configuration_footprint,
    get_engine,
)
from carwatch.models import Listing, PriceHistory, Search
from carwatch.web import routes
from carwatch.web.app import create_app

CONFIG = """
settings:
  db_path: "{db}"
searches:
  - name: "Qashqai"
    sources:
      - site: "autovit"
        url: "https://www.autovit.ro/autoturisme/nissan/qashqai"
  - name: "Juke"
    sources:
      - site: "autovit"
        url: "https://www.autovit.ro/autoturisme/nissan/juke"
"""


def car(ad_id, price, title=None):
    return RawListing(
        site_listing_id=ad_id,
        url=f"https://www.autovit.ro/anunt/ID{ad_id}.html",
        title=title or f"Nissan {ad_id}",
        price=price,
        currency="EUR",
        year=2024,
    )


class Sites:
    """Each search URL returns its own listings. Nothing reaches the network."""

    last_relaxation = None

    def __init__(self, by_fragment):
        self.by_fragment = by_fragment

    def __call__(self, site, settings):
        return self

    def fetch_listings(self, url, max_pages=None):
        for fragment, listings in self.by_fragment.items():
            if fragment in url:
                return listings
        return []


# A car both configurations found: the same ad, collected once under each.
SHARED = "shared-1"


@pytest.fixture
def project(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(CONFIG).format(db=(tmp_path / "t.db").as_posix()),
        encoding="utf-8",
    )
    client = TestClient(create_app(path))
    config = load_config(path)

    # Two runs, so there is history to lose: a first sighting, then a drop.
    collect(
        config,
        adapter_factory=Sites(
            {
                "qashqai": [car("q1", 30000), car(SHARED, 25000)],
                "juke": [car("j1", 20000), car("j2", 21000), car(SHARED, 25000)],
            }
        ),
    )
    collect(
        config,
        adapter_factory=Sites(
            {
                "qashqai": [car("q1", 30000), car(SHARED, 24000)],
                "juke": [car("j1", 19000), car("j2", 21000), car(SHARED, 24000)],
            }
        ),
    )

    class Project:
        def __init__(self):
            self.client = client
            self.path = path
            self.db = config.db_file

        def footprint(self, name):
            with Session(get_engine(self.db)) as session:
                return configuration_footprint(session, name)

        def archive(self, slug="juke"):
            self.client.post(f"/config/{slug}/archive", follow_redirects=False)

        def stamp(self, slug="juke"):
            page = self.client.get(f"/config/{slug}/delete").text
            marker = 'name="stamp" value="'
            start = page.index(marker) + len(marker)
            return page[start : page.index('"', start)]

        def delete(self, slug="juke"):
            return self.client.post(
                f"/config/{slug}/delete",
                data={"stamp": self.stamp(slug)},
                follow_redirects=False,
            )

    return Project()


def test_the_footprint_counts_what_a_configuration_owns(project):
    f = project.footprint("Juke")

    assert f.sources == 1
    assert f.listings == 3
    assert f.active == 3
    # Three first sightings, then two drops (j1 and the shared car).
    assert f.prices == 5
    assert f.runs == 2
    assert f.events >= 2


def test_the_confirmation_shows_the_counts_before_asking(project):
    project.archive()
    page = project.client.get("/config/juke/delete").text

    assert "<strong>3</strong> listings" in page
    assert "<strong>5</strong> price observations" in page
    assert "<strong>2</strong> runs" in page
    assert ">Delete</button>" in page


def test_the_confirmation_refuses_while_still_collecting(project):
    page = project.client.get("/config/juke/delete").text

    assert "Archive it first" in page
    assert ">Delete</button>" not in page


def test_deleting_removes_every_row_the_configuration_owned(project):
    project.archive()

    r = project.delete()

    assert r.status_code == 303
    f = project.footprint("Juke")
    assert (f.sources, f.listings, f.prices, f.runs, f.events) == (0, 0, 0, 0, 0)


def test_deleting_leaves_the_other_configuration_untouched(project):
    before = project.footprint("Qashqai")
    project.archive()

    project.delete()

    assert project.footprint("Qashqai") == before


def test_a_car_both_configurations_found_keeps_the_other_ones_history(project):
    """A listing row belongs to one search. The shared ad was collected once
    under each configuration, so deleting one leaves the other's row — and its
    price drop — exactly where it was."""
    project.archive()
    project.delete()

    with Session(get_engine(project.db)) as session:
        rows = session.exec(
            select(Listing).where(Listing.site_listing_id == SHARED)
        ).all()
        assert len(rows) == 1
        history = session.exec(
            select(PriceHistory).where(PriceHistory.listing_id == rows[0].id)
        ).all()
        assert [p.price for p in history] == [25000, 24000]


def test_a_deleted_configuration_is_gone_from_every_page(project):
    project.archive()
    project.delete()

    assert project.client.get("/config/juke").status_code == 404
    assert 'href="/config/juke"' not in project.client.get("/?disabled=1").text
    assert project.client.get("/config/juke/delete").status_code == 404


def test_a_configuration_already_gone_from_the_file_can_still_be_deleted(project):
    """Removed by hand: sync disables its rows, it shows as archived, and the
    delete page finishes the job on the database alone."""
    text = project.path.read_text(encoding="utf-8")
    project.path.write_text(text[: text.index('  - name: "Juke"')], encoding="utf-8")
    client = TestClient(create_app(project.path))

    page = client.get("/config/juke/delete").text
    assert "already gone from the file" in page

    marker = 'name="stamp" value="'
    start = page.index(marker) + len(marker)
    r = client.post(
        "/config/juke/delete",
        data={"stamp": page[start : page.index('"', start)]},
        follow_redirects=False,
    )

    assert r.status_code == 303
    assert project.footprint("Juke").listings == 0


def test_a_running_collection_blocks_the_delete_and_touches_nothing(project):
    project.archive()
    stamp = project.stamp()
    before_file = project.path.read_text(encoding="utf-8")
    before_rows = project.footprint("Juke")

    with collection_lock(project.db, source="test"):
        r = project.client.post(
            "/config/juke/delete", data={"stamp": stamp}, follow_redirects=False
        )

    assert r.status_code == 409
    assert "collection is running" in r.text
    assert project.path.read_text(encoding="utf-8") == before_file
    assert project.footprint("Juke") == before_rows


def test_the_search_rows_themselves_are_gone(project):
    """Not merely disabled — a disabled row is what archiving leaves."""
    project.archive()
    project.delete()

    with Session(get_engine(project.db)) as session:
        names = {s.name for s in session.exec(select(Search)).all()}
    assert names == {"Qashqai"}


# ----------------------------------------------------------------- backup


def test_deleting_backs_up_the_whole_database_first(project):
    """The copy that makes a delete undoable — taken before anything goes, so
    it still holds the configuration that was deleted."""
    project.archive()
    project.delete()

    db = Path(project.db)
    backups = sorted(db.parent.glob(db.name + ".bak-*"))
    assert len(backups) == 1

    copy = sqlite3.connect(backups[0])
    try:
        names = {n for (n,) in copy.execute("select name from search")}
        listings = copy.execute("select count(*) from listing").fetchone()[0]
    finally:
        copy.close()
    assert names == {"Qashqai", "Juke"}
    assert listings == 5  # q1 and the shared ad under Qashqai; j1, j2, shared under Juke


def test_the_confirmation_says_a_backup_is_taken(project):
    project.archive()
    page = project.client.get("/config/juke/delete").text

    assert "t.db.bak-" in page
    assert "To undo" in page


def test_a_failed_backup_deletes_nothing(project, monkeypatch):
    project.archive()
    before_file = project.path.read_text(encoding="utf-8")
    before_rows = project.footprint("Juke")

    def full_disk(path):
        raise OSError("disk full")

    monkeypatch.setattr(routes, "backup_database", full_disk)
    r = project.delete()

    assert r.status_code == 409
    assert "could not be backed up" in r.text
    assert project.path.read_text(encoding="utf-8") == before_file
    assert project.footprint("Juke") == before_rows


def test_old_database_backups_are_pruned(tmp_path):
    db = tmp_path / "x.db"
    sqlite3.connect(db).close()

    for _ in range(KEEP_DB_BACKUPS + 3):
        backup_database(db)

    assert len(list(tmp_path.glob("x.db.bak-*"))) == KEEP_DB_BACKUPS


def test_a_backup_is_a_database_that_opens(tmp_path):
    db = tmp_path / "x.db"
    con = sqlite3.connect(db)
    con.execute("create table t (x)")
    con.execute("insert into t values (42)")
    con.commit()
    con.close()

    copy = sqlite3.connect(backup_database(db))
    try:
        assert copy.execute("select x from t").fetchone() == (42,)
    finally:
        copy.close()

