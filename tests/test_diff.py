"""Diff logic tests (spec §8) — pure, no database."""

from dataclasses import dataclass
from typing import Optional

import pytest

from carwatch.adapters.base import RawListing
from carwatch.collector.diff import (
    ChangeKind,
    classify,
    diff,
    plan_delistings,
)
from carwatch.models import EventType


@dataclass
class FakeListing:
    site_listing_id: str
    price: Optional[float] = 20000.0
    is_active: bool = True
    missed_runs: int = 0


def raw(ad_id="1", price=20000.0):
    return RawListing(
        site_listing_id=ad_id,
        url=f"https://example.com/{ad_id}",
        title="Nissan Qashqai",
        price=price,
        currency="EUR",
    )


# ------------------------------------------------------------------ classify


def test_unseen_listing_is_new():
    c = classify(None, raw("1", 20000))
    assert c.kind is ChangeKind.NEW
    assert c.event_type is EventType.NEW
    assert c.old_price is None
    assert c.new_price == 20000
    assert c.writes_price_history is True  # initial history row


def test_same_price_is_unchanged():
    c = classify(FakeListing("1", price=20000.0), raw("1", 20000.0))
    assert c.kind is ChangeKind.UNCHANGED
    assert c.event_type is None
    assert c.writes_price_history is False  # no duplicate history row


def test_price_drop():
    c = classify(FakeListing("1", price=20000.0), raw("1", 18500.0))
    assert c.kind is ChangeKind.PRICE_DOWN
    assert c.event_type is EventType.PRICE_DOWN
    assert (c.old_price, c.new_price) == (20000.0, 18500.0)
    assert c.writes_price_history is True


def test_price_rise():
    c = classify(FakeListing("1", price=18500.0), raw("1", 19000.0))
    assert c.kind is ChangeKind.PRICE_UP
    assert c.event_type is EventType.PRICE_UP
    assert c.writes_price_history is True


def test_sub_cent_difference_is_not_a_price_change():
    """Float noise must not manufacture a price event on every run."""
    c = classify(FakeListing("1", price=20000.0), raw("1", 20000.0001))
    assert c.kind is ChangeKind.UNCHANGED


def test_missing_new_price_is_not_a_price_change():
    c = classify(FakeListing("1", price=20000.0), raw("1", None))
    assert c.kind is ChangeKind.UNCHANGED
    assert c.writes_price_history is False


def test_missing_old_price_is_not_a_direction():
    """We cannot say up or down without a previous price, but we do record the
    newly-known one in the history."""
    c = classify(FakeListing("1", price=None), raw("1", 20000.0))
    assert c.kind is ChangeKind.UNCHANGED
    assert c.event_type is None


def test_inactive_listing_that_reappears_is_relisted():
    c = classify(FakeListing("1", price=20000.0, is_active=False), raw("1", 19000.0))
    assert c.kind is ChangeKind.RELISTED
    assert c.is_new is True
    assert c.event_type is EventType.NEW
    assert c.writes_price_history is True


# ------------------------------------------------------------- delisting


def test_missing_listing_is_delisted_with_threshold_one():
    active = [FakeListing("1"), FakeListing("2")]
    out = plan_delistings(active, seen_ids={"1"}, delist_after_missed_runs=1)

    assert len(out) == 1
    assert out[0].listing.site_listing_id == "2"
    assert out[0].missed_runs == 1
    assert out[0].delist_now is True


def test_threshold_two_needs_two_consecutive_misses():
    """delist_after_missed_runs absorbs transient pagination flakiness."""
    listing = FakeListing("2", missed_runs=0)

    first = plan_delistings([listing], seen_ids=set(), delist_after_missed_runs=2)[0]
    assert first.missed_runs == 1
    assert first.delist_now is False

    listing.missed_runs = first.missed_runs
    second = plan_delistings([listing], seen_ids=set(), delist_after_missed_runs=2)[0]
    assert second.missed_runs == 2
    assert second.delist_now is True


def test_seen_listings_are_never_delisted():
    assert plan_delistings([FakeListing("1")], seen_ids={"1"}) == []


def test_already_inactive_listings_are_not_considered():
    """diff() only passes active listings; inactive ones must not be re-delisted."""
    existing = {"1": FakeListing("1", is_active=False)}
    result = diff(existing, fetched=[], delist_after_missed_runs=1)
    assert result.delistings == []


def test_threshold_below_one_is_clamped():
    out = plan_delistings([FakeListing("2")], seen_ids=set(), delist_after_missed_runs=0)
    assert out[0].delist_now is True


# ------------------------------------------------------------------- diff()


def test_full_diff_counts():
    existing = {
        "keep": FakeListing("keep", price=20000.0),
        "drop": FakeListing("drop", price=20000.0),
        "gone": FakeListing("gone", price=15000.0),
    }
    fetched = [
        raw("keep", 20000.0),   # unchanged
        raw("drop", 18000.0),   # price down
        raw("brand-new", 9000.0),
    ]

    result = diff(existing, fetched, delist_after_missed_runs=1)

    assert result.new_count == 1
    assert result.price_change_count == 1
    assert result.delisted_count == 1
    assert [d.listing.site_listing_id for d in result.delistings] == ["gone"]

    kinds = {c.raw.site_listing_id: c.kind for c in result.changes}
    assert kinds == {
        "keep": ChangeKind.UNCHANGED,
        "drop": ChangeKind.PRICE_DOWN,
        "brand-new": ChangeKind.NEW,
    }


def test_empty_fetch_delists_everything():
    """A genuinely empty *successful* fetch does delist. The protection against
    doing this on a failed fetch lives in the engine, which never calls diff()."""
    existing = {"1": FakeListing("1"), "2": FakeListing("2")}
    result = diff(existing, fetched=[], delist_after_missed_runs=1)
    assert result.delisted_count == 2
