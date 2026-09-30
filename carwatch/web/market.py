"""The market behind one configuration: where the cars are, what mileage they
carry, what that mileage costs, and how quickly they go.

Descriptive only. Nothing here rates a single car — price by km band says what
the middle of each band asks, not whether any one ad is a good deal. That
question was tried and dropped: the spread inside a band is options and trim,
which CarWatch does not read.

Takes merged cars (`MergedListing`), so a car carried by two sites is counted
once, and works per market because Romanian and German inventory are never the
same car.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median
from typing import Optional

from carwatch.web.merge import MARKET_LABELS, MARKETS

# Mileage bands. 25 000 km is about a year and a half of ordinary driving: fine
# enough to separate a 2-year-old commuter from an ex-fleet car, coarse enough
# that each band holds more than one or two ads.
KM_BAND = 25_000
# Everything past this is one open-ended band. Past 150 000 km the handful of
# cars are the long tail, and a row per band would be mostly empty.
KM_OPEN_FROM = 150_000

# New and pre-registered dealer stock is its own market, not the bottom of the
# used one: on live data a third of the active ads had under 1 000 km, and only
# a few were flagged as new. Left in, they pulled one configuration's "median
# mileage" down to 30 km.
NEW_BELOW_KM = 1_000

# Below this many cars a median is one seller's opinion, not the market's — the
# same rule the price chart uses for a day. The row still shows its count.
MIN_FOR_MEDIAN = 3

# How far back "cut their price" and "gone" look.
CUT_WINDOW = timedelta(days=14)

# Time-to-gone needs cars we watched arrive *and* leave. Cars already listed on
# the first collection have an unknown start, so they are left out, and until
# this many have come and gone the figure would be anecdote.
MIN_GONE_FOR_SPEED = 5
# A car first seen within this long of the first collection counts as "already
# listed when watching started" — the first collection can span a few runs.
BASELINE_GRACE = timedelta(days=1)


def is_new(car) -> bool:
    """Unregistered, or registered but essentially undriven."""
    km = car.canonical.mileage_km
    return car.canonical.condition == "new" or (km is not None and km < NEW_BELOW_KM)


def _eur(car) -> bool:
    return car.price is not None and (car.currency or "EUR") == "EUR"


def _mid(values) -> Optional[float]:
    return median(values) if len(values) >= MIN_FOR_MEDIAN else None


def _mid_price(cars) -> Optional[float]:
    return _mid([c.price for c in cars if _eur(c)])


@dataclass
class MarketSplit:
    """One market's share of the configuration, used and new apart."""

    key: str
    label: str
    cars: int
    used: int
    used_price: Optional[float]
    used_km: Optional[float]
    new: int
    new_price: Optional[float]


@dataclass
class KmBand:
    # None/None: the "new" row. high None alone: the open-ended top band.
    low: Optional[int]
    high: Optional[int]
    # market key -> (cars, median EUR price or None)
    by_market: dict[str, tuple[int, Optional[float]]] = field(default_factory=dict)

    @property
    def cars(self) -> int:
        return sum(n for n, _ in self.by_market.values())

    @property
    def is_new(self) -> bool:
        return self.low is None

    @property
    def label(self) -> str:
        if self.low is None:
            return f"New / under {NEW_BELOW_KM:,} km".replace(",", " ")
        # The first used band starts where "new" stops, not at 0.
        low = "1k" if self.low == 0 else f"{self.low // 1000}k"
        return f"{low}+ km" if self.high is None else f"{low}–{self.high // 1000}k km"


@dataclass
class Selling:
    """How quickly cars leave, and how many are cutting their price."""

    # Days since the first collection — how much history the rest stands on.
    watched_days: int
    # Any car that stopped being listed within the window, however long we knew it.
    gone_recently: int
    # Cars that arrived while watched and have since gone.
    gone: int
    median_days_listed: Optional[float]
    # Active cars whose price went down within CUT_WINDOW, out of `active`.
    cut: int
    active: int

    @property
    def cut_share(self) -> Optional[float]:
        return self.cut / self.active if self.active else None


@dataclass
class Market:
    markets: list[MarketSplit]
    bands: list[KmBand]
    no_km: int  # active used cars whose ad gives no mileage
    selling: Selling

    @property
    def widest_band(self) -> int:
        return max((b.cars for b in self.bands), default=0)


def _market_order(keys) -> list[str]:
    return [k for k in MARKETS if k in keys] + sorted(k for k in keys if k not in MARKETS)


def _band_low(km: int) -> int:
    return min(km // KM_BAND * KM_BAND, KM_OPEN_FROM)


def _cut_recently(car, now: datetime) -> bool:
    """Did this car's asking price go down within the window?"""
    points = car.history
    return any(
        later.price < earlier.price and later.observed_at >= now - CUT_WINDOW
        for earlier, later in zip(points, points[1:])
    )


def _band_row(low, high, groups: dict[str, list], order) -> KmBand:
    row = KmBand(low=low, high=high)
    for key in order:
        group = groups.get(key, [])
        row.by_market[key] = (len(group), _mid_price(group))
    return row


def market_summary(cars, now: datetime) -> Optional[Market]:
    """Everything the Market section shows, or None with nothing to show."""
    active = [c for c in cars if c.is_active]
    if not active:
        return None

    by_market: dict[str, list] = {}
    for car in active:
        by_market.setdefault(car.market, []).append(car)
    order = _market_order(by_market)

    markets = []
    for key in order:
        new = [c for c in by_market[key] if is_new(c)]
        used = [c for c in by_market[key] if not is_new(c)]
        markets.append(
            MarketSplit(
                key=key,
                label=MARKET_LABELS.get(key, key),
                cars=len(by_market[key]),
                used=len(used),
                used_price=_mid_price(used),
                used_km=_mid(
                    [c.canonical.mileage_km for c in used if c.canonical.mileage_km is not None]
                ),
                new=len(new),
                new_price=_mid_price(new),
            )
        )

    new_by_market: dict[str, list] = {}
    bands: dict[int, dict[str, list]] = {}
    no_km = 0
    for car in active:
        if is_new(car):
            new_by_market.setdefault(car.market, []).append(car)
            continue
        km = car.canonical.mileage_km
        if km is None:
            no_km += 1
            continue
        bands.setdefault(_band_low(km), {}).setdefault(car.market, []).append(car)

    band_rows = []
    if new_by_market:
        band_rows.append(_band_row(None, None, new_by_market, order))
    if bands:
        # Every band from the lowest to the highest that has cars, so an empty
        # band in the middle shows as a gap rather than disappearing.
        for low in range(min(bands), max(bands) + 1, KM_BAND):
            high = None if low >= KM_OPEN_FROM else low + KM_BAND
            band_rows.append(_band_row(low, high, bands.get(low, {}), order))

    started = min(c.first_seen for c in cars)
    arrived_while_watched = [c for c in cars if c.first_seen > started + BASELINE_GRACE]
    gone = [c for c in arrived_while_watched if not c.is_active]
    days_listed = [(c.last_seen - c.first_seen).total_seconds() / 86400 for c in gone]

    selling = Selling(
        watched_days=(now - started).days,
        gone_recently=sum(
            1 for c in cars if not c.is_active and c.last_seen >= now - CUT_WINDOW
        ),
        gone=len(gone),
        median_days_listed=median(days_listed) if len(gone) >= MIN_GONE_FOR_SPEED else None,
        cut=sum(1 for c in active if _cut_recently(c, now)),
        active=len(active),
    )

    return Market(markets=markets, bands=band_rows, no_km=no_km, selling=selling)
