"""SQLModel models — the persistent state described in spec §6.

One config search that pulls from three sites becomes three `Search` rows
(one per site), so a single site breaking or getting blocked only affects
its own row. `(name, site)` is the stable identity used to sync config.yaml
into the DB on every run.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    """Naive UTC timestamp (SQLite has no tz-aware storage; we stay consistent)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class RunStatus(str, Enum):
    OK = "ok"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    ERROR = "error"


class EventType(str, Enum):
    NEW = "new"
    PRICE_UP = "price_up"
    PRICE_DOWN = "price_down"
    DELISTED = "delisted"


class Search(SQLModel, table=True):
    """One saved search, for one site."""

    __tablename__ = "search"
    __table_args__ = (UniqueConstraint("name", "site", name="uq_search_name_site"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    site: str = Field(index=True)
    url: str
    # make/model/year/price etc. — display + grouping only, real filtering is in `url`.
    filters_json: str = Field(default="{}")
    enabled: bool = Field(default=True)
    created_at: datetime = Field(default_factory=utcnow)


class Listing(SQLModel, table=True):
    """One car, identified stably across runs by (search_id, site, site_listing_id)."""

    __tablename__ = "listing"
    __table_args__ = (
        UniqueConstraint(
            "search_id", "site", "site_listing_id", name="uq_listing_identity"
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    search_id: int = Field(foreign_key="search.id", index=True)
    site: str = Field(index=True)
    site_listing_id: str = Field(index=True)
    url: str
    title: str

    make: Optional[str] = None
    model: Optional[str] = None
    year: Optional[int] = None
    price: Optional[float] = None
    currency: Optional[str] = None
    mileage_km: Optional[int] = None
    fuel: Optional[str] = None
    gearbox: Optional[str] = None
    location: Optional[str] = None
    # "new" for a vehicle the site says is unregistered, else None. Kept because
    # such a car has no first-registration date and so no `year` - without this
    # the Year column is simply blank, which reads as a parsing failure rather
    # than as the thing it means.
    condition: Optional[str] = None
    # Card photo, hot-linked from the site's own CDN (front-end plan §4). No
    # image is downloaded or cached; a delisted car's URL eventually 404s and
    # the card falls back to a placeholder.
    image_url: Optional[str] = None

    first_seen: datetime = Field(default_factory=utcnow)
    last_seen: datetime = Field(default_factory=utcnow, index=True)
    is_active: bool = Field(default=True, index=True)
    # How many consecutive successful runs this listing has been absent from.
    # Compared against settings.delist_after_missed_runs before delisting.
    missed_runs: int = Field(default=0)
    raw_json: Optional[str] = None


class PriceHistory(SQLModel, table=True):
    """Append-only. A row is written on first sighting and on every price change."""

    __tablename__ = "price_history"

    id: Optional[int] = Field(default=None, primary_key=True)
    listing_id: int = Field(foreign_key="listing.id", index=True)
    price: float
    currency: Optional[str] = None
    observed_at: datetime = Field(default_factory=utcnow, index=True)


class Run(SQLModel, table=True):
    """One collection run, for one search."""

    __tablename__ = "run"

    id: Optional[int] = Field(default=None, primary_key=True)
    search_id: int = Field(foreign_key="search.id", index=True)
    site: str
    started_at: datetime = Field(default_factory=utcnow, index=True)
    finished_at: Optional[datetime] = None
    status: RunStatus = Field(default=RunStatus.OK)
    listings_found: int = 0
    new_count: int = 0
    price_change_count: int = 0
    delisted_count: int = 0
    error_message: Optional[str] = None


class Favorite(SQLModel, table=True):
    """A car you starred.

    Keyed on the *ad* — site and the site's own ad id — not on a listing row. A
    listing row is one ad under one search: the same ad under two
    configurations is two rows, the card that joins an autovit ad to olx's copy
    of it is recomputed on every request, and deleting a configuration removes
    its rows. None of that should unstar a car.

    No foreign key, for the same reason: a favourite must outlive the rows that
    showed it. `title` and `url` are a snapshot taken when it was starred, so a
    favourite whose rows are gone can still say what it was.
    """

    __tablename__ = "favorite"
    __table_args__ = (
        UniqueConstraint("site", "site_listing_id", name="uq_favorite_ad"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    site: str = Field(index=True)
    site_listing_id: str = Field(index=True)
    title: str = ""
    url: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class AppState(SQLModel, table=True):
    """Small pieces of dashboard state, by name — today, when you last opened Changes.

    In the database rather than a cookie: it has to outlive the browser, and
    the runs it is measured against happen with no browser open at all.
    """

    __tablename__ = "app_state"

    name: str = Field(primary_key=True)
    value: str = ""


class Event(SQLModel, table=True):
    """One notable change — powers the dashboard's "what changed" feed."""

    __tablename__ = "event"

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="run.id", index=True)
    listing_id: int = Field(foreign_key="listing.id", index=True)
    type: EventType = Field(index=True)
    old_price: Optional[float] = None
    new_price: Optional[float] = None
    created_at: datetime = Field(default_factory=utcnow, index=True)
