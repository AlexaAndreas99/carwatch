"""The Market section's figures: market split, mileage bands, and selling speed."""

from datetime import datetime, timedelta
from types import SimpleNamespace

from carwatch.web.market import market_summary

NOW = datetime(2026, 9, 30, 12, 0)


def car(
    market="de",
    price=25_000.0,
    km=50_000,
    active=True,
    first=NOW - timedelta(days=20),
    last=NOW,
    prices=(),
    condition=None,
    currency="EUR",
):
    """A stand-in for a merged car: just the attributes market_summary reads."""
    history = [SimpleNamespace(price=p, observed_at=at) for p, at in prices]
    return SimpleNamespace(
        market=market,
        price=price,
        currency=currency,
        is_active=active,
        first_seen=first,
        last_seen=last,
        history=history,
        canonical=SimpleNamespace(mileage_km=km, condition=condition),
    )


def test_nothing_active_means_no_section():
    assert market_summary([car(active=False)], NOW) is None


def test_markets_are_split_and_romania_comes_first():
    m = market_summary([car("de"), car("de"), car("ro")], NOW)

    assert [(s.key, s.cars) for s in m.markets] == [("ro", 1), ("de", 2)]


def test_new_and_nearly_new_cars_are_kept_out_of_the_used_figures():
    """Dealer stock at 10 km once dragged a "median mileage" down to 30 km."""
    cars = [car(km=10, price=35_000), car(km=None, condition="new", price=36_000),
            car(km=999, price=34_000)] + [car(km=60_000, price=25_000)] * 3
    de = market_summary(cars, NOW).markets[0]

    assert (de.new, de.used) == (3, 3)
    assert de.new_price == 35_000
    assert de.used_km == 60_000
    assert de.used_price == 25_000


def test_bands_run_without_gaps_and_new_cars_get_their_own_row():
    cars = [car(km=5), car(km=10_000), car(km=80_000)]
    bands = market_summary(cars, NOW).bands

    assert [b.label for b in bands] == [
        "New / under 1 000 km", "1k–25k km", "25k–50k km", "50k–75k km", "75k–100k km",
    ]
    assert [b.cars for b in bands] == [1, 1, 0, 0, 1]


def test_high_mileage_shares_one_open_band():
    bands = market_summary([car(km=160_000), car(km=400_000)], NOW).bands

    assert [(b.label, b.cars) for b in bands] == [("150k+ km", 2)]


def test_a_band_median_needs_three_cars_but_keeps_its_count():
    cars = [car(km=30_000, price=p) for p in (20_000, 22_000)]
    band = market_summary(cars, NOW).bands[0]

    assert band.by_market["de"] == (2, None)


def test_prices_in_other_currencies_stay_out_of_the_medians():
    cars = [car(price=20_000)] * 3 + [car(price=150_000, currency="RON")]

    assert market_summary(cars, NOW).markets[0].used_price == 20_000


def test_a_cut_counts_only_inside_the_window():
    recent = car(prices=[(30_000, NOW - timedelta(days=20)), (29_000, NOW - timedelta(days=3))])
    old = car(prices=[(30_000, NOW - timedelta(days=40)), (29_000, NOW - timedelta(days=30))])
    rise = car(prices=[(30_000, NOW - timedelta(days=9)), (31_000, NOW - timedelta(days=2))])
    selling = market_summary([recent, old, rise], NOW).selling

    assert (selling.cut, selling.active) == (1, 3)


def test_time_to_sell_ignores_cars_already_listed_when_watching_began():
    """Their start is unknown, so their listing time would be made up."""
    start = NOW - timedelta(days=30)
    cars = [car(first=start)]  # the first collection
    cars += [car(first=start, last=start + timedelta(days=25), active=False)]
    cars += [
        car(first=start + timedelta(days=5), last=start + timedelta(days=5 + d), active=False)
        for d in (2, 3, 4, 6, 8)
    ]
    selling = market_summary(cars, NOW).selling

    assert selling.gone == 5
    assert selling.median_days_listed == 4
    assert selling.watched_days == 30


def test_time_to_sell_waits_for_enough_cars():
    start = NOW - timedelta(days=10)
    cars = [car(first=start)] + [
        car(first=start + timedelta(days=3), last=start + timedelta(days=4), active=False)
    ] * 4
    selling = market_summary(cars, NOW).selling

    assert selling.gone == 4
    assert selling.median_days_listed is None


def test_gone_recently_counts_every_car_that_left_in_the_window():
    cars = [
        car(),
        car(active=False, last=NOW - timedelta(days=2)),
        car(active=False, last=NOW - timedelta(days=20)),
    ]

    assert market_summary(cars, NOW).selling.gone_recently == 1
