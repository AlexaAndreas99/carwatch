"""Merge-layer tests (front-end plan §5).

Most of these need no database: `merge_listings` is pure, and building `Listing`
objects directly keeps each case readable. The last section is the exception —
the rule that merging never writes is only worth anything if it is checked
against a real file.
"""

import hashlib
import sqlite3
import textwrap
from datetime import datetime

import pytest
from sqlmodel import select

from carwatch.config import load_config
from carwatch.db import init_db, session_scope
from carwatch.models import Listing, PriceHistory, Search
from carwatch.web.merge import (
    MARKETS,
    autovit_ad_slug,
    market_of,
    merge_key,
    merge_listings,
)

# The same physical car, as autovit lists it and as olx links to it.
AUTOVIT_URL = "https://www.autovit.ro/autoturisme/anunt/nissan-qashqai-ID7HQf7M.html"
OLX_LINKS_TO_AUTOVIT = "https://www.autovit.ro/anunt/nissan-qashqai-ID7HQf7M.html"
OLX_NATIVE_URL = "https://www.olx.ro/d/oferta/nissan-qashqai-CID767-IDxyz.html"
MOBILEDE_URL = "https://www.mobile.de/ro/vehicule/detalii.html?id=461256563"

JAN = datetime(2026, 9, 1, 10, 0)
FEB = datetime(2026, 9, 2, 10, 0)


def listing(
    id,
    site,
    url,
    site_listing_id="1",
    search_id=1,
    price=25000.0,
    image_url=None,
    is_active=True,
    first_seen=JAN,
    last_seen=FEB,
    **kw,
):
    return Listing(
        id=id,
        search_id=search_id,
        site=site,
        site_listing_id=site_listing_id,
        url=url,
        title=kw.pop("title", "Nissan Qashqai"),
        price=price,
        currency="EUR",
        image_url=image_url,
        is_active=is_active,
        first_seen=first_seen,
        last_seen=last_seen,
        **kw,
    )


def points(listing_id, *prices, start=JAN):
    return [
        PriceHistory(
            id=listing_id * 100 + i,
            listing_id=listing_id,
            price=p,
            currency="EUR",
            observed_at=datetime(2026, 9, 1 + i, 10, 0),
        )
        for i, p in enumerate(prices)
    ]


# ------------------------------------------------------------------- the key


@pytest.mark.parametrize(
    "url,expected",
    [
        (AUTOVIT_URL, "7HQf7M"),
        (OLX_LINKS_TO_AUTOVIT, "7HQf7M"),
        ("https://www.autovit.ro/autoturisme/anunt/x-ID7HHYA3.html", "7HHYA3"),
        # Not autovit: a similar-looking slug elsewhere must never merge cars.
        ("https://www.example.com/car-ID7HQf7M.html", None),
        (OLX_NATIVE_URL, None),
        (MOBILEDE_URL, None),
        ("https://www.autovit.ro/autoturisme/nissan/qashqai", None),
        (None, None),
        ("", None),
    ],
)
def test_ad_slug_extraction(url, expected):
    assert autovit_ad_slug(url) == expected


def test_the_same_ad_on_two_sites_shares_a_key():
    a = listing(1, "autovit", AUTOVIT_URL, site_listing_id="7058793251")
    b = listing(2, "olx", OLX_LINKS_TO_AUTOVIT, site_listing_id="305871621")
    assert merge_key(a) == merge_key(b)


def test_a_listing_without_a_slug_keys_on_site_and_id():
    x = listing(1, "olx", OLX_NATIVE_URL, site_listing_id="305871621")
    assert merge_key(x) == ("olx", "305871621")


@pytest.mark.parametrize(
    "site,market", [("autovit", "ro"), ("olx", "ro"), ("mobilede", "de")]
)
def test_market_membership(site, market):
    assert market_of(site) == market


def test_an_unknown_site_gets_its_own_market_rather_than_joining_one():
    """Folding a new site silently into Romania would blend inventory the plan
    is explicit about keeping apart."""
    assert market_of("mobile.fr") == "mobile.fr"
    assert "mobile.fr" not in {s for sites in MARKETS.values() for s in sites}


# ---------------------------------------------------------------- the merge


def test_syndicated_ad_becomes_one_car_on_two_sites():
    rows = [
        listing(1, "autovit", AUTOVIT_URL, price=26980.0),
        listing(2, "olx", OLX_LINKS_TO_AUTOVIT, price=27500.0),
    ]

    (car,) = merge_listings(rows)

    assert car.is_merged
    assert car.sites == ["autovit", "olx"]
    assert set(car.sources) == {"autovit", "olx"}
    # Decision 4: the autovit price is the canonical one.
    assert car.price == 26980.0
    assert car.canonical.site == "autovit"


def test_one_car_matching_two_searches_collapses_too():
    """An ad matching both configured searches is two rows on the same site."""
    rows = [
        listing(1, "autovit", AUTOVIT_URL, search_id=1),
        listing(2, "autovit", AUTOVIT_URL, search_id=3),
    ]

    (car,) = merge_listings(rows)

    assert car.is_merged
    assert car.sites == ["autovit"]
    assert car.search_ids == {1, 3}


def test_an_olx_native_ad_stands_alone():
    rows = [
        listing(1, "autovit", AUTOVIT_URL),
        listing(2, "olx", OLX_NATIVE_URL, site_listing_id="999"),
    ]

    cars = merge_listings(rows)

    assert len(cars) == 2
    assert not any(c.is_merged for c in cars)


def test_a_german_listing_never_merges_with_a_romanian_one():
    """§2: a Romanian ad and a German ad are never the same car."""
    rows = [
        listing(1, "autovit", AUTOVIT_URL),
        listing(2, "mobilede", MOBILEDE_URL, site_listing_id="461256563"),
    ]

    cars = merge_listings(rows)

    assert len(cars) == 2
    assert {c.market for c in cars} == {"ro", "de"}


def test_input_order_is_preserved():
    rows = [
        listing(1, "olx", OLX_NATIVE_URL, site_listing_id="a"),
        listing(2, "olx", OLX_NATIVE_URL.replace("xyz", "abc"), site_listing_id="b"),
        listing(3, "olx", OLX_NATIVE_URL.replace("xyz", "def"), site_listing_id="c"),
    ]
    assert [c.canonical.id for c in merge_listings(rows)] == [1, 2, 3]


def test_empty_input():
    assert merge_listings([]) == []


# ------------------------------------------------------------- canonical row


def test_autovit_wins_even_when_olx_comes_first():
    rows = [
        listing(1, "olx", OLX_LINKS_TO_AUTOVIT, price=27500.0),
        listing(2, "autovit", AUTOVIT_URL, price=26980.0),
    ]
    (car,) = merge_listings(rows)
    assert car.canonical.id == 2


def test_among_rows_from_one_site_the_one_with_a_photo_wins():
    rows = [
        listing(1, "autovit", AUTOVIT_URL, search_id=1),
        listing(2, "autovit", AUTOVIT_URL, search_id=3, image_url="https://cdn/x.jpg"),
    ]
    (car,) = merge_listings(rows)
    assert car.canonical.id == 2


def test_the_photo_can_come_from_any_member():
    """17 of 47 olx cards carry no photo; merged with autovit's row they get one."""
    rows = [
        listing(1, "olx", OLX_LINKS_TO_AUTOVIT, image_url=None),
        listing(2, "autovit", AUTOVIT_URL, image_url="https://cdn/car.jpg"),
    ]
    (car,) = merge_listings(rows)
    assert car.image_url == "https://cdn/car.jpg"


def test_no_photo_anywhere_is_none():
    (car,) = merge_listings([listing(1, "autovit", AUTOVIT_URL)])
    assert car.image_url is None


# -------------------------------------------------------------------- state


def test_a_car_is_active_while_any_site_still_lists_it():
    rows = [
        listing(1, "autovit", AUTOVIT_URL, is_active=False),
        listing(2, "olx", OLX_LINKS_TO_AUTOVIT, is_active=True),
    ]
    (car,) = merge_listings(rows)
    assert car.is_active is True


def test_a_car_is_gone_only_when_every_site_has_dropped_it():
    rows = [
        listing(1, "autovit", AUTOVIT_URL, is_active=False),
        listing(2, "olx", OLX_LINKS_TO_AUTOVIT, is_active=False),
    ]
    (car,) = merge_listings(rows)
    assert car.is_active is False


def test_first_seen_is_the_earliest_sighting_on_any_site():
    early = datetime(2026, 8, 20, 9, 0)
    rows = [
        listing(1, "autovit", AUTOVIT_URL, first_seen=JAN, last_seen=FEB),
        listing(2, "olx", OLX_LINKS_TO_AUTOVIT, first_seen=early, last_seen=JAN),
    ]
    (car,) = merge_listings(rows)
    assert car.first_seen == early
    assert car.last_seen == FEB


# ------------------------------------------------------------ price history


def test_the_longest_history_is_used():
    rows = [
        listing(1, "autovit", AUTOVIT_URL),
        listing(2, "olx", OLX_LINKS_TO_AUTOVIT),
    ]
    history = {1: points(1, 26980.0), 2: points(2, 27500.0, 27000.0, 26800.0)}

    (car,) = merge_listings(rows, history)

    assert car.prices == [27500.0, 27000.0, 26800.0]


def test_histories_from_two_sites_are_never_spliced_together():
    """Interleaving two independent series invents price movements that never
    happened — site A at 26 000 and site B at 26 500 would read as a rise."""
    rows = [
        listing(1, "autovit", AUTOVIT_URL),
        listing(2, "olx", OLX_LINKS_TO_AUTOVIT),
    ]
    history = {1: points(1, 26000.0, 25500.0), 2: points(2, 26500.0, 26400.0)}

    (car,) = merge_listings(rows, history)

    assert car.prices in ([26000.0, 25500.0], [26500.0, 26400.0])
    assert len(car.prices) == 2


def test_a_single_observation_draws_no_sparkline():
    """§7: the sparkline is a real price trace, so one point is not one."""
    (car,) = merge_listings([listing(1, "autovit", AUTOVIT_URL)], {1: points(1, 26980.0)})
    assert car.prices == []
    assert car.first_price == 26980.0


def test_no_history_at_all():
    (car,) = merge_listings([listing(1, "autovit", AUTOVIT_URL)])
    assert car.prices == []
    assert car.first_price is None
    assert car.delta_since_first is None
    assert car.is_drop is False


def test_delta_since_first_and_drop_flag():
    rows = [listing(1, "autovit", AUTOVIT_URL, price=25500.0)]
    (car,) = merge_listings(rows, {1: points(1, 26980.0, 25500.0)})

    assert car.delta_since_first == pytest.approx(-1480.0)
    assert car.is_drop is True


def test_a_sub_cent_move_is_float_noise_not_a_drop():
    rows = [listing(1, "autovit", AUTOVIT_URL, price=26980.001)]
    (car,) = merge_listings(rows, {1: points(1, 26980.0, 26980.001)})
    assert car.delta_since_first is None
    assert car.is_drop is False


# ------------------------------------------------- the database is read-only

CONFIG = """
settings:
  db_path: "{db}"
searches:
  - name: "Qashqai"
    sources:
      - site: "autovit"
        url: "https://www.autovit.ro/autoturisme/nissan/qashqai"
"""


@pytest.fixture
def populated_db(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        textwrap.dedent(CONFIG).format(db=(tmp_path / "t.db").as_posix()),
        encoding="utf-8",
    )
    config = load_config(p)
    init_db(config.db_file)
    with session_scope(config.db_file) as s:
        s.add(Search(id=1, name="Qashqai", site="autovit", url="https://x"))
    with session_scope(config.db_file) as s:
        s.add(listing(1, "autovit", AUTOVIT_URL, site_listing_id="7058793251"))
        s.add(listing(2, "olx", OLX_LINKS_TO_AUTOVIT, site_listing_id="305871621"))
        for point in points(1, 26980.0, 26500.0):
            s.add(point)
    return config


def digest(path):
    """Hash the database's *contents*, not just the file.

    The file bytes alone are not enough: CarWatch runs SQLite in WAL mode, so a
    write can sit in the `-wal` sidecar and leave the main file's bytes
    untouched. Dumping every row is what actually catches a stray write.
    """
    conn = sqlite3.connect(path)
    try:
        dump = "\n".join(conn.iterdump())
    finally:
        conn.close()
    return hashlib.sha256(
        dump.encode("utf-8") + path.read_bytes()
    ).hexdigest()


def test_merging_leaves_the_database_byte_identical(populated_db):
    """Decision 5 is the load-bearing one: merging in the database would
    destroy the per-site price history already collected and is not reversible.
    Merging on read is a presentation choice we can change any time — but only
    while it stays a read."""
    before = digest(populated_db.db_file)

    with session_scope(populated_db.db_file) as s:
        rows = s.exec(select(Listing)).all()
        history = {}
        for h in s.exec(select(PriceHistory)).all():
            history.setdefault(h.listing_id, []).append(h)
        cars = merge_listings(rows, history)
        # Touch everything a template would.
        for car in cars:
            _ = (car.price, car.image_url, car.sources, car.sites, car.prices,
                 car.first_seen, car.is_active, car.delta_since_first)

    assert len(cars) == 1
    assert digest(populated_db.db_file) == before


def test_the_two_site_rows_keep_their_own_identities(populated_db):
    """Merging is a view. Both rows must still be there, with their own ids."""
    with session_scope(populated_db.db_file) as s:
        rows = s.exec(select(Listing).order_by(Listing.id)).all()
        merge_listings(rows)
    with session_scope(populated_db.db_file) as s:
        after = s.exec(select(Listing).order_by(Listing.id)).all()

    assert [(x.id, x.site, x.site_listing_id) for x in after] == [
        (1, "autovit", "7058793251"),
        (2, "olx", "305871621"),
    ]


# ------------------------------------------------- clustering repeated events

from datetime import timedelta  # noqa: E402

from carwatch.models import Event, EventType  # noqa: E402
from carwatch.web.merge import group_events, pick_representative  # noqa: E402


def event(id, listing_id, when, kind=EventType.PRICE_DOWN, old=26980.0, new=25500.0):
    return Event(
        id=id,
        run_id=1,
        listing_id=listing_id,
        type=kind,
        old_price=old,
        new_price=new,
        created_at=when,
    )


AUTOVIT_ROW = listing(1, "autovit", AUTOVIT_URL)
OLX_ROW = listing(2, "olx", OLX_LINKS_TO_AUTOVIT)


def test_one_change_reported_by_two_sites_is_one_group():
    pairs = [
        (event(1, 1, JAN), AUTOVIT_ROW),
        (event(2, 2, JAN + timedelta(minutes=2)), OLX_ROW),
    ]

    groups = group_events(pairs)

    assert len(groups) == 1
    assert len(groups[0]) == 2


def test_sites_collected_hours_apart_still_group():
    """The measured case: autovit and olx are collected in separate runs, so the
    duplicate reports of one drop sat ~130 minutes apart. A time-window rule got
    this wrong, which is why there isn't one."""
    pairs = [
        (event(1, 1, JAN), AUTOVIT_ROW),
        (event(2, 2, JAN + timedelta(hours=2, minutes=10)), OLX_ROW),
    ]

    assert len(group_events(pairs)) == 1


def test_sites_collected_days_apart_still_group():
    """Collection is manual; the gap between running one site and another is
    whatever the user makes it."""
    pairs = [
        (event(1, 1, JAN), AUTOVIT_ROW),
        (event(2, 2, JAN + timedelta(days=4)), OLX_ROW),
    ]

    assert len(group_events(pairs)) == 1


def test_the_same_change_across_two_searches_also_collapses():
    a = listing(1, "autovit", AUTOVIT_URL, search_id=1)
    b = listing(5, "autovit", AUTOVIT_URL, search_id=3)
    pairs = [(event(1, 1, JAN), a), (event(2, 5, JAN + timedelta(seconds=30)), b)]

    assert len(group_events(pairs)) == 1


def test_one_row_reporting_twice_is_two_changes():
    """The rule: a second real change necessarily produces another event on the
    same listing row. That repeat is what splits a group."""
    pairs = [
        (event(1, 1, JAN), AUTOVIT_ROW),
        (event(2, 1, JAN + timedelta(days=30)), AUTOVIT_ROW),
    ]

    assert len(group_events(pairs)) == 2


def test_two_drops_each_seen_by_both_sites_stay_two_rows():
    pairs = [
        (event(1, 1, JAN), AUTOVIT_ROW),
        (event(2, 2, JAN + timedelta(hours=2)), OLX_ROW),
        (event(3, 1, JAN + timedelta(days=30)), AUTOVIT_ROW),
        (event(4, 2, JAN + timedelta(days=30, hours=2)), OLX_ROW),
    ]

    groups = group_events(pairs)

    assert len(groups) == 2
    assert all(len(g) == 2 for g in groups)


def test_two_quick_drops_on_one_site_are_never_merged():
    """A time window would have folded these together; the row-repeat rule
    cannot, however close they are."""
    pairs = [
        (event(1, 1, JAN), AUTOVIT_ROW),
        (event(2, 1, JAN + timedelta(seconds=90)), AUTOVIT_ROW),
    ]

    assert len(group_events(pairs)) == 2


def test_different_event_types_never_group():
    pairs = [
        (event(1, 1, JAN, kind=EventType.NEW), AUTOVIT_ROW),
        (event(2, 2, JAN, kind=EventType.PRICE_DOWN), OLX_ROW),
    ]

    assert len(group_events(pairs)) == 2


def test_different_cars_never_group():
    other = listing(3, "autovit", "https://www.autovit.ro/anunt/x-IDzzz999.html")
    pairs = [(event(1, 1, JAN), AUTOVIT_ROW), (event(2, 3, JAN), other)]

    assert len(group_events(pairs)) == 2


def test_a_relisting_is_a_second_arrival_not_a_duplicate():
    pairs = [
        (event(1, 1, JAN, kind=EventType.NEW), AUTOVIT_ROW),
        (event(2, 1, JAN + timedelta(days=20), kind=EventType.NEW), AUTOVIT_ROW),
    ]

    assert len(group_events(pairs)) == 2


def test_groups_come_back_newest_first():
    pairs = [
        (event(1, 1, JAN), AUTOVIT_ROW),
        (event(2, 1, JAN + timedelta(days=5)), AUTOVIT_ROW),
    ]

    groups = group_events(pairs)

    assert groups[0][0][0].id == 2
    assert groups[1][0][0].id == 1


def test_rows_without_ids_are_still_told_apart():
    """Grouping must work on objects that have not been through the database."""
    a = listing(None, "autovit", AUTOVIT_URL, site_listing_id="700")
    b = listing(None, "olx", OLX_LINKS_TO_AUTOVIT, site_listing_id="305871621")
    pairs = [(event(1, 1, JAN), a), (event(2, 2, JAN), b)]

    assert len(group_events(pairs)) == 1


def test_empty_input():
    assert group_events([]) == []


# ------------------------------------------------------- who speaks for a row


def test_autovit_speaks_for_a_merged_change():
    """So the feed and the Listings page never quote different numbers. The
    sites really do disagree: 8 of 38 syndicated ads here differ by a euro or
    two, because olx rounds the conversion differently."""
    pairs = [
        (event(2, 2, JAN, old=27500.0, new=26000.0), OLX_ROW),
        (event(1, 1, JAN, old=26980.0, new=25500.0), AUTOVIT_ROW),
    ]

    chosen_event, chosen_listing = pick_representative(pairs)

    assert chosen_listing.site == "autovit"
    assert (chosen_event.old_price, chosen_event.new_price) == (26980.0, 25500.0)


def test_with_no_autovit_row_the_earliest_speaks():
    pairs = [
        (event(2, 2, JAN + timedelta(minutes=5)), OLX_ROW),
        (event(1, 2, JAN), OLX_ROW),
    ]

    chosen_event, _ = pick_representative(pairs)

    assert chosen_event.id == 1
