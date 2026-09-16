"""DB init + config->DB sync tests."""

import sqlite3
import textwrap

from sqlmodel import select

from carwatch.config import load_config
from carwatch.db import add_missing_columns, get_engine, init_db, session_scope, sync_searches
from carwatch.models import Listing, Search

BASE = """
settings:
  db_path: "{db}"
searches:
  - name: "Qashqai"
    make: "Nissan"
    sources:
      - site: "autovit"
        url: "https://www.autovit.ro/autoturisme/nissan/qashqai"
      - site: "olx"
        url: "https://www.olx.ro/autoturisme/nissan/"
"""


def make_config(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(
        textwrap.dedent(text).format(db=(tmp_path / "t.db").as_posix()), encoding="utf-8"
    )
    return load_config(p)


def test_init_creates_tables(tmp_path):
    cfg = make_config(tmp_path, BASE)
    init_db(cfg.db_file)
    assert cfg.db_file.exists()
    with session_scope(cfg.db_file) as s:
        assert s.exec(select(Search)).all() == []


def test_sync_creates_one_row_per_source(tmp_path):
    cfg = make_config(tmp_path, BASE)
    init_db(cfg.db_file)

    with session_scope(cfg.db_file) as s:
        sync_searches(s, cfg)
    with session_scope(cfg.db_file) as s:
        rows = s.exec(select(Search)).all()

    assert sorted(r.site for r in rows) == ["autovit", "olx"]
    assert all(r.name == "Qashqai" for r in rows)
    assert all(r.enabled for r in rows)


def test_sync_is_idempotent_and_updates_url(tmp_path):
    cfg = make_config(tmp_path, BASE)
    init_db(cfg.db_file)
    with session_scope(cfg.db_file) as s:
        sync_searches(s, cfg)
    with session_scope(cfg.db_file) as s:
        first_ids = sorted(r.id for r in s.exec(select(Search)).all())

    cfg2 = make_config(
        tmp_path,
        BASE.replace(
            "https://www.autovit.ro/autoturisme/nissan/qashqai",
            "https://www.autovit.ro/autoturisme/nissan/qashqai?page=2",
        ),
    )
    with session_scope(cfg2.db_file) as s:
        sync_searches(s, cfg2)
    with session_scope(cfg2.db_file) as s:
        rows = s.exec(select(Search)).all()

    assert sorted(r.id for r in rows) == first_ids  # no duplicate rows
    autovit = next(r for r in rows if r.site == "autovit")
    assert autovit.url.endswith("?page=2")


def test_removed_source_is_disabled_not_deleted(tmp_path):
    cfg = make_config(tmp_path, BASE)
    init_db(cfg.db_file)
    with session_scope(cfg.db_file) as s:
        sync_searches(s, cfg)

    trimmed = """
    settings:
      db_path: "{db}"
    searches:
      - name: "Qashqai"
        sources:
          - site: "autovit"
            url: "https://www.autovit.ro/autoturisme/nissan/qashqai"
    """
    cfg2 = make_config(tmp_path, trimmed)
    with session_scope(cfg2.db_file) as s:
        sync_searches(s, cfg2)
    with session_scope(cfg2.db_file) as s:
        rows = {r.site: r for r in s.exec(select(Search)).all()}

    assert set(rows) == {"autovit", "olx"}  # olx row survives
    assert rows["autovit"].enabled is True
    assert rows["olx"].enabled is False


# ------------------------------------------------------- additive migrations


def _columns(db_file, table="listing"):
    conn = sqlite3.connect(db_file)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _old_schema_db(db_file):
    """A `listing` table as it stood before photos existed, with a row in it."""
    conn = sqlite3.connect(db_file)
    conn.executescript(
        """
        CREATE TABLE listing (
            id INTEGER PRIMARY KEY,
            search_id INTEGER NOT NULL,
            site TEXT NOT NULL,
            site_listing_id TEXT NOT NULL,
            url TEXT NOT NULL,
            title TEXT NOT NULL,
            make TEXT,
            model TEXT,
            year INTEGER,
            price FLOAT,
            currency TEXT,
            mileage_km INTEGER,
            fuel TEXT,
            gearbox TEXT,
            location TEXT,
            first_seen DATETIME NOT NULL,
            last_seen DATETIME NOT NULL,
            is_active BOOLEAN NOT NULL,
            missed_runs INTEGER NOT NULL,
            raw_json TEXT
        );
        INSERT INTO listing VALUES
            (1, 1, 'autovit', '700', 'https://x/1', 'Qashqai',
             'Nissan', 'Qashqai', 2024, 25000.0, 'EUR', 54000,
             'Benzina', NULL, 'Bucuresti',
             '2026-09-01 10:00:00', '2026-09-04 10:00:00', 1, 0, '{}');
        """
    )
    conn.commit()
    conn.close()


def test_init_adds_a_column_missing_from_an_existing_database(tmp_path):
    """`create_all` only creates missing tables, so a database built before
    `image_url` existed would work until the first query touched it."""
    cfg = make_config(tmp_path, BASE)
    _old_schema_db(cfg.db_file)
    assert "image_url" not in _columns(cfg.db_file)

    init_db(cfg.db_file)

    assert "image_url" in _columns(cfg.db_file)


def test_migration_keeps_existing_rows_and_leaves_the_new_column_null(tmp_path):
    cfg = make_config(tmp_path, BASE)
    _old_schema_db(cfg.db_file)

    init_db(cfg.db_file)

    with session_scope(cfg.db_file) as s:
        (row,) = s.exec(select(Listing)).all()
    assert row.title == "Qashqai"
    assert row.price == 25000.0
    assert row.image_url is None


def test_migration_is_idempotent(tmp_path):
    cfg = make_config(tmp_path, BASE)
    _old_schema_db(cfg.db_file)

    init_db(cfg.db_file)
    assert add_missing_columns(get_engine(cfg.db_file)) == []


def test_reports_what_it_added(tmp_path):
    cfg = make_config(tmp_path, BASE)
    _old_schema_db(cfg.db_file)

    assert sorted(add_missing_columns(get_engine(cfg.db_file))) == [
        "listing.condition",
        "listing.image_url",
    ]


def test_a_fresh_database_needs_no_migration(tmp_path):
    cfg = make_config(tmp_path, BASE)
    init_db(cfg.db_file)
    assert "image_url" in _columns(cfg.db_file)
    assert add_missing_columns(get_engine(cfg.db_file)) == []
