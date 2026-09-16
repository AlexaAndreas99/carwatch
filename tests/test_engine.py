"""Collector engine tests — end to end over a real SQLite DB, no network."""

import textwrap

import pytest
from sqlmodel import select

from carwatch.adapters.base import AdapterError, BlockedError, RawListing
from carwatch.collector.engine import collect
from carwatch.config import load_config
from carwatch.models import Event, EventType, Listing, PriceHistory, Run, RunStatus

CONFIG = """
settings:
  db_path: "{db}"
  delist_after_missed_runs: {threshold}
searches:
  - name: "Qashqai"
    make: "Nissan"
    sources:
      - site: "autovit"
        url: "https://www.autovit.ro/autoturisme/nissan/qashqai"
"""


@pytest.fixture
def cfg(tmp_path):
    def build(threshold=1):
        p = tmp_path / "config.yaml"
        p.write_text(
            textwrap.dedent(CONFIG).format(
                db=(tmp_path / "t.db").as_posix(), threshold=threshold
            ),
            encoding="utf-8",
        )
        return load_config(p)

    return build


class FakeAdapter:
    """Serves a scripted sequence of results, one per run."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.last_relaxation = None

    def __call__(self, site, settings):
        return self

    def fetch_listings(self, url):
        result = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(result, Exception):
            raise result
        return result


def raw(ad_id, price, title="Nissan Qashqai", **kw):
    return RawListing(
        site_listing_id=ad_id,
        url=f"https://www.autovit.ro/anunt/ID{ad_id}.html",
        title=title,
        price=price,
        currency="EUR",
        **kw,
    )


def run_once(config, adapter):
    return collect(config, adapter_factory=adapter)


def db(config):
    """Open a fresh session on the config's DB."""
    from carwatch.db import session_scope

    return session_scope(config.db_file)


# ------------------------------------------------------------- first run


def test_first_run_inserts_listings_history_and_events(cfg):
    config = cfg()
    adapter = FakeAdapter([[raw("1", 20000), raw("2", 18000)]])

    summaries = run_once(config, adapter)

    assert len(summaries) == 1
    s = summaries[0]
    assert s.status is RunStatus.OK
    assert (s.listings_found, s.new_count, s.price_change_count, s.delisted_count) == (2, 2, 0, 0)

    with db(config) as session:
        listings = session.exec(select(Listing)).all()
        assert {x.site_listing_id for x in listings} == {"1", "2"}
        assert all(x.is_active for x in listings)
        assert all(x.raw_json is not None for x in listings)

        # One initial price_history row per listing.
        assert len(session.exec(select(PriceHistory)).all()) == 2
        events = session.exec(select(Event)).all()
        assert [e.type for e in events] == [EventType.NEW, EventType.NEW]

        run = session.exec(select(Run)).one()
        assert run.status is RunStatus.OK
        assert run.listings_found == 2
        assert run.finished_at is not None


# --------------------------------------------------------- price changes


def test_price_drop_across_two_runs(cfg):
    config = cfg()
    adapter = FakeAdapter([[raw("1", 20000)], [raw("1", 18500)]])

    run_once(config, adapter)
    summaries = run_once(config, adapter)

    assert summaries[0].price_change_count == 1
    assert summaries[0].new_count == 0

    with db(config) as session:
        listing = session.exec(select(Listing)).one()
        assert listing.price == 18500

        history = session.exec(
            select(PriceHistory).order_by(PriceHistory.observed_at)
        ).all()
        assert [h.price for h in history] == [20000, 18500]

        drop = session.exec(
            select(Event).where(Event.type == EventType.PRICE_DOWN)
        ).one()
        assert (drop.old_price, drop.new_price) == (20000, 18500)


def test_unchanged_price_writes_no_history_row(cfg):
    config = cfg()
    adapter = FakeAdapter([[raw("1", 20000)], [raw("1", 20000)]])

    run_once(config, adapter)
    run_once(config, adapter)

    with db(config) as session:
        assert len(session.exec(select(PriceHistory)).all()) == 1
        assert len(session.exec(select(Event)).all()) == 1  # just the initial NEW


def test_listing_fields_are_refreshed_on_reappearance(cfg):
    """Sellers edit ads in place; the stored row must track the live one."""
    config = cfg()
    adapter = FakeAdapter([
        [raw("1", 20000, title="Old title", mileage_km=50000)],
        [raw("1", 20000, title="New title", mileage_km=52000)],
    ])

    run_once(config, adapter)
    run_once(config, adapter)

    with db(config) as session:
        listing = session.exec(select(Listing)).one()
        assert listing.title == "New title"
        assert listing.mileage_km == 52000


# -------------------------------------------------------------- delisting


def test_disappeared_listing_is_delisted(cfg):
    config = cfg()
    adapter = FakeAdapter([[raw("1", 20000), raw("2", 18000)], [raw("1", 20000)]])

    run_once(config, adapter)
    summaries = run_once(config, adapter)

    assert summaries[0].delisted_count == 1

    with db(config) as session:
        gone = session.exec(select(Listing).where(Listing.site_listing_id == "2")).one()
        assert gone.is_active is False
        # History is preserved, not deleted.
        assert len(session.exec(select(PriceHistory).where(PriceHistory.listing_id == gone.id)).all()) == 1

        ev = session.exec(select(Event).where(Event.type == EventType.DELISTED)).one()
        assert ev.old_price == 18000


def test_delist_threshold_two_requires_two_misses(cfg):
    config = cfg(threshold=2)
    adapter = FakeAdapter([
        [raw("1", 20000), raw("2", 18000)],
        [raw("1", 20000)],   # miss 1
        [raw("1", 20000)],   # miss 2 -> delist
    ])

    run_once(config, adapter)
    assert run_once(config, adapter)[0].delisted_count == 0

    with db(config) as session:
        still = session.exec(select(Listing).where(Listing.site_listing_id == "2")).one()
        assert still.is_active is True
        assert still.missed_runs == 1

    assert run_once(config, adapter)[0].delisted_count == 1

    with db(config) as session:
        gone = session.exec(select(Listing).where(Listing.site_listing_id == "2")).one()
        assert gone.is_active is False


def test_reappearing_listing_resets_miss_streak(cfg):
    config = cfg(threshold=2)
    adapter = FakeAdapter([
        [raw("1", 20000), raw("2", 18000)],
        [raw("1", 20000)],                    # 2 missed once
        [raw("1", 20000), raw("2", 18000)],   # 2 is back
    ])

    run_once(config, adapter)
    run_once(config, adapter)
    run_once(config, adapter)

    with db(config) as session:
        listing = session.exec(select(Listing).where(Listing.site_listing_id == "2")).one()
        assert listing.missed_runs == 0
        assert listing.is_active is True


def test_relisted_listing_reactivates_and_emits_new(cfg):
    config = cfg()
    adapter = FakeAdapter([
        [raw("1", 20000)],
        [],                    # delisted
        [raw("1", 17000)],     # back, cheaper
    ])

    run_once(config, adapter)
    run_once(config, adapter)
    summaries = run_once(config, adapter)

    assert summaries[0].new_count == 1

    with db(config) as session:
        listing = session.exec(select(Listing)).one()
        assert listing.is_active is True
        assert listing.price == 17000
        # first_seen is preserved across the gap; history keeps both prices.
        assert listing.first_seen < listing.last_seen
        assert [h.price for h in session.exec(
            select(PriceHistory).order_by(PriceHistory.observed_at)).all()] == [20000, 17000]


# ----------------------------------------------- failure must not delist


def test_blocked_fetch_does_not_delist_anything(cfg):
    """The single most important rule in §8: a blocked fetch must never be read
    as 'everything got delisted'."""
    config = cfg()
    adapter = FakeAdapter([[raw("1", 20000), raw("2", 18000)], BlockedError("bot wall")])

    run_once(config, adapter)
    summaries = run_once(config, adapter)

    assert summaries[0].status is RunStatus.BLOCKED
    assert summaries[0].delisted_count == 0
    assert "bot wall" in summaries[0].error_message

    with db(config) as session:
        listings = session.exec(select(Listing)).all()
        assert all(x.is_active for x in listings)
        assert all(x.missed_runs == 0 for x in listings)
        # No delisted events, and no new history rows.
        assert session.exec(select(Event).where(Event.type == EventType.DELISTED)).all() == []
        assert len(session.exec(select(PriceHistory)).all()) == 2

        run = session.exec(select(Run).where(Run.status == RunStatus.BLOCKED)).one()
        assert run.error_message == "bot wall"


def test_adapter_error_does_not_delist_anything(cfg):
    config = cfg()
    adapter = FakeAdapter([[raw("1", 20000)], AdapterError("page structure changed")])

    run_once(config, adapter)
    summaries = run_once(config, adapter)

    assert summaries[0].status is RunStatus.ERROR
    with db(config) as session:
        assert session.exec(select(Listing)).one().is_active is True


def test_unexpected_exception_is_contained(cfg):
    """One site blowing up must not take the run down."""
    config = cfg()
    adapter = FakeAdapter([[raw("1", 20000)], ValueError("boom")])

    run_once(config, adapter)
    summaries = run_once(config, adapter)

    assert summaries[0].status is RunStatus.ERROR
    assert "ValueError" in summaries[0].error_message
    with db(config) as session:
        assert session.exec(select(Listing)).one().is_active is True


def test_recovery_after_a_block_resumes_normally(cfg):
    config = cfg()
    adapter = FakeAdapter([
        [raw("1", 20000)],
        BlockedError("bot wall"),
        [raw("1", 19000)],
    ])

    run_once(config, adapter)
    run_once(config, adapter)
    summaries = run_once(config, adapter)

    assert summaries[0].status is RunStatus.OK
    assert summaries[0].price_change_count == 1
    with db(config) as session:
        assert [h.price for h in session.exec(
            select(PriceHistory).order_by(PriceHistory.observed_at)).all()] == [20000, 19000]


# -------------------------------------------------------------- empty / misc


def test_genuinely_empty_successful_fetch_does_delist(cfg):
    config = cfg()
    adapter = FakeAdapter([[raw("1", 20000)], []])

    run_once(config, adapter)
    summaries = run_once(config, adapter)

    assert summaries[0].status is RunStatus.OK
    assert summaries[0].delisted_count == 1


def test_relaxation_note_is_recorded_on_the_run(cfg):
    config = cfg()
    adapter = FakeAdapter([[]])
    adapter.last_relaxation = {"rule": "price_mileage_year_criteria_changed"}

    summaries = run_once(config, adapter)

    assert "relaxed" in summaries[0].note
    with db(config) as session:
        assert "relaxed" in session.exec(select(Run)).one().error_message


def test_site_filter_limits_what_runs(cfg):
    config = cfg()
    adapter = FakeAdapter([[raw("1", 20000)]])

    assert collect(config, sites=["olx"], adapter_factory=adapter) == []
    assert len(collect(config, sites=["autovit"], adapter_factory=adapter)) == 1


# ------------------------------------------------------------ card photos


PHOTO = "https://ireland.apollo.olxcdn.com/v1/files/abc-AUTOVITRO/image;s=640x480"
PHOTO2 = "https://ireland.apollo.olxcdn.com/v1/files/xyz-AUTOVITRO/image;s=640x480"


def test_image_url_is_stored_on_a_new_listing(cfg):
    config = cfg()
    run_once(config, FakeAdapter([[raw("1", 20000, image_url=PHOTO)]]))

    with db(config) as s:
        (listing,) = s.exec(select(Listing)).all()
    assert listing.image_url == PHOTO


def test_a_changed_photo_replaces_the_old_one(cfg):
    """Sellers re-shoot ads; the newest photo is the right one to show."""
    config = cfg()
    adapter = FakeAdapter(
        [[raw("1", 20000, image_url=PHOTO)], [raw("1", 20000, image_url=PHOTO2)]]
    )
    run_once(config, adapter)
    run_once(config, adapter)

    with db(config) as s:
        (listing,) = s.exec(select(Listing)).all()
    assert listing.image_url == PHOTO2


def test_a_run_that_finds_no_photo_keeps_the_one_we_have(cfg):
    """Unlike every other field, a missing photo is far more likely to be our
    parse missing it (lazy loading, a layout tweak) than the seller pulling it.
    Blanking the column would empty the grid on one bad run."""
    config = cfg()
    adapter = FakeAdapter([[raw("1", 20000, image_url=PHOTO)], [raw("1", 20000)]])
    run_once(config, adapter)
    run_once(config, adapter)

    with db(config) as s:
        (listing,) = s.exec(select(Listing)).all()
    assert listing.image_url == PHOTO


def test_a_listing_with_no_photo_is_still_collected(cfg):
    config = cfg()
    run_once(config, FakeAdapter([[raw("1", 20000)]]))

    with db(config) as s:
        (listing,) = s.exec(select(Listing)).all()
    assert listing.image_url is None
    assert listing.price == 20000
