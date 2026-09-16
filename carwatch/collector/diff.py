"""Pure diff logic (spec §8), deliberately free of any database access.

Everything here is a decision about what *should* happen, given the previous
state and what the adapter just fetched. `engine.py` applies those decisions.
Keeping the two apart means the rules that protect price history can be tested
exhaustively without a database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional, Protocol

from carwatch.adapters.base import RawListing
from carwatch.models import EventType


class ChangeKind(str, Enum):
    NEW = "new"
    RELISTED = "relisted"
    PRICE_UP = "price_up"
    PRICE_DOWN = "price_down"
    UNCHANGED = "unchanged"


class ExistingListing(Protocol):
    """The subset of `models.Listing` the diff actually reads."""

    site_listing_id: str
    price: Optional[float]
    is_active: bool
    missed_runs: int


@dataclass
class Change:
    """What to do about one fetched listing."""

    kind: ChangeKind
    raw: RawListing
    old_price: Optional[float] = None
    new_price: Optional[float] = None

    @property
    def is_new(self) -> bool:
        return self.kind in (ChangeKind.NEW, ChangeKind.RELISTED)

    @property
    def is_price_change(self) -> bool:
        return self.kind in (ChangeKind.PRICE_UP, ChangeKind.PRICE_DOWN)

    @property
    def event_type(self) -> Optional[EventType]:
        """The `event` row to emit, or None for an unremarkable sighting."""
        return {
            ChangeKind.NEW: EventType.NEW,
            # A listing that came back is reported as `new` — from the user's
            # point of view it is available again, and the model has no
            # dedicated relisted event type.
            ChangeKind.RELISTED: EventType.NEW,
            ChangeKind.PRICE_UP: EventType.PRICE_UP,
            ChangeKind.PRICE_DOWN: EventType.PRICE_DOWN,
        }.get(self.kind)

    @property
    def writes_price_history(self) -> bool:
        """Append-only history: a row on first sighting and on every change.

        A relisted car also gets one — it may have come back at a new price, and
        even at the same price the row records that we saw it again after a gap.
        """
        if self.new_price is None:
            return False
        return self.kind is not ChangeKind.UNCHANGED


@dataclass
class Delisting:
    """An active listing that was absent from a successful fetch."""

    listing: ExistingListing
    missed_runs: int          # the new count, after this run
    delist_now: bool          # whether it crosses the threshold this run


@dataclass
class DiffResult:
    changes: list[Change] = field(default_factory=list)
    delistings: list[Delisting] = field(default_factory=list)

    @property
    def new_count(self) -> int:
        return sum(1 for c in self.changes if c.is_new)

    @property
    def price_change_count(self) -> int:
        return sum(1 for c in self.changes if c.is_price_change)

    @property
    def delisted_count(self) -> int:
        return sum(1 for d in self.delistings if d.delist_now)


def classify(existing: Optional[ExistingListing], raw: RawListing) -> Change:
    """Decide what one fetched listing means versus its previous state."""
    if existing is None:
        return Change(ChangeKind.NEW, raw, old_price=None, new_price=raw.price)

    old_price = existing.price
    new_price = raw.price

    if not existing.is_active:
        # Was delisted (or missed), now back.
        return Change(ChangeKind.RELISTED, raw, old_price=old_price, new_price=new_price)

    # A missing price on either side is not a price movement — we cannot say
    # which way it went, so record it as unchanged and let the listing update
    # carry the new value.
    if old_price is None or new_price is None or _same_price(old_price, new_price):
        return Change(ChangeKind.UNCHANGED, raw, old_price=old_price, new_price=new_price)

    kind = ChangeKind.PRICE_UP if new_price > old_price else ChangeKind.PRICE_DOWN
    return Change(kind, raw, old_price=old_price, new_price=new_price)


def _same_price(a: float, b: float) -> bool:
    """Prices are whole-currency figures; a sub-cent delta is float noise."""
    return abs(a - b) < 0.01


def plan_delistings(
    active: Iterable[ExistingListing],
    seen_ids: set[str],
    delist_after_missed_runs: int = 1,
) -> list[Delisting]:
    """Work out which currently-active listings are missing from this fetch.

    Only ever call this for a *successful* fetch. §8 is explicit: a failed or
    blocked fetch must never be read as "everything got delisted".

    `delist_after_missed_runs` absorbs transient pagination flakiness — a
    listing must be absent from that many consecutive successful runs before it
    is marked inactive.
    """
    threshold = max(1, delist_after_missed_runs)
    out: list[Delisting] = []

    for listing in active:
        if listing.site_listing_id in seen_ids:
            continue
        missed = (listing.missed_runs or 0) + 1
        out.append(
            Delisting(listing=listing, missed_runs=missed, delist_now=missed >= threshold)
        )

    return out


def diff(
    existing_by_id: dict[str, ExistingListing],
    fetched: list[RawListing],
    delist_after_missed_runs: int = 1,
) -> DiffResult:
    """Full diff for one search: what changed, and what went missing."""
    changes = [classify(existing_by_id.get(raw.site_listing_id), raw) for raw in fetched]
    seen_ids = {raw.site_listing_id for raw in fetched}

    active = [x for x in existing_by_id.values() if x.is_active]
    delistings = plan_delistings(active, seen_ids, delist_after_missed_runs)

    return DiffResult(changes=changes, delistings=delistings)
