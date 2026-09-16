"""SQLite engine, session helper, and schema creation."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import delete, func, or_
from sqlalchemy import event as sa_event
from sqlmodel import Session, SQLModel, create_engine, select

# Import for the side effect of registering tables on SQLModel.metadata.
from carwatch import models  # noqa: F401
from carwatch.config import Config, SearchConfig
from carwatch.models import Event, Listing, PriceHistory, Run, Search

_engine = None
_engine_path: Optional[Path] = None


def get_engine(db_path: str | Path, echo: bool = False):
    """Create (or reuse) the engine for `db_path`."""
    global _engine, _engine_path

    path = Path(db_path).expanduser().resolve()
    if _engine is not None and _engine_path == path:
        return _engine

    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{path}",
        echo=echo,
        connect_args={"check_same_thread": False},
    )

    @sa_event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        # WAL lets the dashboard read while a collection run is writing.
        cur.execute("PRAGMA journal_mode=WAL")
        # Wait rather than failing instantly if the dashboard and a collection
        # touch the DB at the same moment.
        cur.execute("PRAGMA busy_timeout=10000")
        cur.close()

    _engine, _engine_path = engine, path
    return engine


# Columns added to an already-shipped table. `create_all` only ever creates
# missing *tables*, so without this an existing carwatch.db would keep working
# right up to the first query that touched a new column, then fail with
# "no such column". SQLite adds a nullable column in place, which is all we
# have needed so far — anything that requires rewriting a table (a NOT NULL
# column, a changed type, a dropped column) is not what this does, and would
# want a real migration tool.
ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "listing": {"image_url": "TEXT", "condition": "TEXT"},
}


def add_missing_columns(engine) -> list[str]:
    """Bring an existing database up to the current schema. Returns what it added."""
    added: list[str] = []
    with engine.begin() as conn:
        for table, columns in ADDED_COLUMNS.items():
            present = {
                row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")
            }
            if not present:
                # Table doesn't exist yet; create_all builds it complete.
                continue
            for name, decl in columns.items():
                if name in present:
                    continue
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                added.append(f"{table}.{name}")
    return added


def init_db(db_path: str | Path, echo: bool = False):
    """Create the database file and all tables if they don't exist yet."""
    engine = get_engine(db_path, echo=echo)
    SQLModel.metadata.create_all(engine)
    add_missing_columns(engine)
    return engine


@contextmanager
def session_scope(db_path: str | Path) -> Iterator[Session]:
    """Transactional session: commits on success, rolls back on exception.

    `expire_on_commit=False` so rows stay readable after the block exits — the
    dashboard and CLI both hand model objects to a renderer once the session is
    closed, and the default would raise DetachedInstanceError on every attribute.
    Safe here because CarWatch is a single-writer local app.
    """
    engine = get_engine(db_path)
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def sync_searches(session: Session, config: Config) -> list[Search]:
    """Mirror config.yaml's searches into the `search` table.

    One config search with N sources becomes N rows, keyed on (name, site).
    Rows whose config source has been removed are disabled rather than
    deleted, so their listings and price history survive.
    """
    wanted: dict[tuple[str, str], tuple[SearchConfig, str]] = {}
    for sc in config.searches:
        for src in sc.sources:
            wanted[(sc.name, src.site)] = (sc, src.url)

    existing = {(s.name, s.site): s for s in session.exec(select(Search)).all()}
    rows: list[Search] = []

    for (name, site), (sc, url) in wanted.items():
        row = existing.get((name, site))
        if row is None:
            row = Search(
                name=name,
                site=site,
                url=url,
                filters_json=sc.filters_json,
                enabled=sc.enabled,
            )
            session.add(row)
        else:
            row.url = url
            row.filters_json = sc.filters_json
            row.enabled = sc.enabled
            session.add(row)
        rows.append(row)

    for key, row in existing.items():
        if key not in wanted and row.enabled:
            row.enabled = False
            session.add(row)

    session.flush()
    return rows


# ------------------------------------------------------ database backups

# How many `carwatch.db.bak-*` copies to keep. One is taken before every
# configuration delete; eight copies of a database under a megabyte is
# nothing, and reaches back further than anyone remembers deleting.
KEEP_DB_BACKUPS = 8
DB_BACKUP_PREFIX = ".bak-"


def backup_database(db_path: str | Path) -> Path:
    """Copy the whole database beside itself, prune old copies, return the copy.

    Through SQLite's online backup API rather than a file copy: the database
    runs in WAL mode, so the main file alone can be missing committed pages
    that still live in the `-wal` file. The backup API reads through SQLite
    and writes one self-contained, consistent file.
    """
    path = Path(db_path).expanduser().resolve()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_name(path.name + DB_BACKUP_PREFIX + stamp)
    # Two deletes inside one second would otherwise share a backup name.
    counter = 1
    while target.exists():
        target = path.with_name(f"{path.name}{DB_BACKUP_PREFIX}{stamp}-{counter}")
        counter += 1

    source = sqlite3.connect(path)
    try:
        copy = sqlite3.connect(target)
        try:
            source.backup(copy)
        finally:
            copy.close()
    finally:
        source.close()

    backups = sorted(
        path.parent.glob(path.name + DB_BACKUP_PREFIX + "*"),
        key=lambda p: p.name,
        reverse=True,
    )
    for old in backups[KEEP_DB_BACKUPS:]:
        try:
            old.unlink()
        except OSError:  # pragma: no cover - a backup we can't prune is harmless
            pass
    return target


# ------------------------------------------------- deleting a configuration


@dataclass
class Footprint:
    """What a configuration has accumulated in the database — what deleting it destroys."""

    sources: int = 0
    listings: int = 0
    active: int = 0
    prices: int = 0
    runs: int = 0
    events: int = 0


def _owned(name: str):
    """Subqueries for the `search`, `listing` and `run` ids a configuration owns."""
    searches = select(Search.id).where(Search.name == name)
    listings = select(Listing.id).where(Listing.search_id.in_(searches))
    runs = select(Run.id).where(Run.search_id.in_(searches))
    return searches, listings, runs


def configuration_footprint(session: Session, name: str) -> Footprint:
    """Count what a configuration owns, for the delete confirmation."""
    searches, listings, runs = _owned(name)

    def count(model, *where) -> int:
        return session.exec(select(func.count()).select_from(model).where(*where)).one()

    return Footprint(
        sources=count(Search, Search.name == name),
        listings=count(Listing, Listing.search_id.in_(searches)),
        active=count(
            Listing, Listing.search_id.in_(searches), Listing.is_active == True  # noqa: E712
        ),
        prices=count(PriceHistory, PriceHistory.listing_id.in_(listings)),
        runs=count(Run, Run.search_id.in_(searches)),
        events=count(
            Event, or_(Event.listing_id.in_(listings), Event.run_id.in_(runs))
        ),
    )


def purge_configuration(session: Session, name: str) -> Footprint:
    """Delete every row a configuration owns. Irreversible; the caller confirms first.

    Children before parents, because foreign keys are enforced: events, then
    price history, then listings and runs, then the `search` rows themselves.
    Subqueries rather than id lists, so a configuration with thousands of
    listings cannot run into SQLite's limit on bound parameters.

    Nothing shared is touched. A listing row belongs to exactly one `search`
    row, so a car another configuration also found keeps that configuration's
    own row, history and events — the merge that shows the two as one car is
    computed per request and never stored.
    """
    gone = configuration_footprint(session, name)
    searches, listings, runs = _owned(name)

    session.exec(
        delete(Event).where(or_(Event.listing_id.in_(listings), Event.run_id.in_(runs)))
    )
    session.exec(delete(PriceHistory).where(PriceHistory.listing_id.in_(listings)))
    session.exec(delete(Listing).where(Listing.search_id.in_(searches)))
    session.exec(delete(Run).where(Run.search_id.in_(searches)))
    session.exec(delete(Search).where(Search.name == name))
    session.flush()
    return gone


def search_filters(row: Search) -> dict:
    """Decode a Search row's filters_json, tolerating bad data."""
    try:
        data = json.loads(row.filters_json or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
