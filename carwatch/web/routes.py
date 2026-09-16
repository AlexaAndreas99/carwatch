"""Dashboard routes.

`/` is the Listings page (front-end plan §6): market tabs over the merge layer,
as a card grid or compact rows. Overview retired into it — its "which sources
are healthy" half moved to `/runs`, and its counts-per-search half is answered
by the listings themselves.

Everything here reads. The merge that collapses an autovit ad and the olx copy
of it into one card is computed per request and never written back (§5).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from time import monotonic
from pathlib import Path
from typing import Annotated, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from pydantic import BeforeValidator
from starlette.concurrency import run_in_threadpool
from sqlmodel import Session, func, select

from carwatch import scheduling, search_urls, site_rules
from carwatch.adapters.base import AdapterError, BlockedError, get_adapter
from carwatch.collector.lock import CollectionBusy, collection_lock
from carwatch.config import (
    KNOWN_SITES,
    PLACEHOLDER,
    SearchConfig,
    Source,
    load_config,
)
from carwatch.config_writer import (
    ConfigWriteError,
    FileStamp,
    NoSuchSearchError,
    SearchDraft,
    delete_search,
    export_search,
    save_search,
    set_enabled,
)
from carwatch.db import (
    KEEP_DB_BACKUPS,
    backup_database,
    configuration_footprint,
    get_engine,
    purge_configuration,
    search_filters,
    session_scope,
    sync_searches,
)
from carwatch.models import (
    AppState,
    Event,
    EventType,
    Favorite,
    Listing,
    PriceHistory,
    Run,
    RunStatus,
    Search,
    utcnow,
)
from carwatch.web import configurations
from carwatch.web.merge import (
    MARKET_LABELS,
    MARKETS,
    group_events,
    market_of,
    SITE_PRIORITY,
    merge_listings,
    pick_representative,
)

log = logging.getLogger(__name__)

router = APIRouter()


# --------------------------------------------------- lenient query numbers

# An empty toolbar field submits as `price_min=`, not as an absent parameter,
# and a bare `Optional[float]` rejects "" as an invalid number - so pressing
# Apply with the price boxes blank answered with a raw 422 JSON dump instead of
# the page. Unparseable values are dropped for the same reason: a read-only
# dashboard should never answer a hand-edited URL with a validation traceback,
# and an ignored filter is visible anyway, because the box renders back empty.


def _opt_float(value):
    """Query value -> float, or None for blank and unusable input."""
    if value is None or isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _opt_int(value):
    """Query value -> int, or None for blank and unusable input."""
    if value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        # via float so "2024.0" from a round-tripped URL still reads as 2024
        return int(float(text))
    except ValueError:
        return None


# `Query()` has to sit *inside* the Annotated. Written the other way round -
# `price_min: OptFloat = Query(None)` - FastAPI rebuilds the field from the
# FieldInfo in the default and the BeforeValidator is silently dropped, so the
# blank-field 422 comes straight back with no sign of why.
OptFloat = Annotated[Optional[float], BeforeValidator(_opt_float), Query()]
OptInt = Annotated[Optional[int], BeforeValidator(_opt_int), Query()]
OptFormInt = Annotated[Optional[int], BeforeValidator(_opt_int), Form()]


def _clamp(value: Optional[int], default: int, maximum: int) -> int:
    """A row limit that always lands somewhere sane.

    `limit: int = Query(200, le=1000)` rejects a blank `?limit=` with a 422 and
    a hand-typed `?limit=99999` with another one. Neither is worth failing a
    page over: falling back to the default and clamping to the ceiling gives
    the caller something useful and still protects the query.
    """
    if value is None:
        return default
    return max(1, min(value, maximum))



# --------------------------------------------------------------------- helpers


def _session(request: Request) -> Session:
    config = request.app.state.config
    return Session(get_engine(config.db_file), expire_on_commit=False)


def _render(request: Request, template: str, **context) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request=request, name=template, context=context
    )


# ------------------------------------------------------------- the sidebar

# Which configuration the page is scoped to, and whether archived ones are
# listed. Both live in the query string, so a scoped view is a link you can
# send yourself — and so the scope survives switching market tab, changing a
# filter, and moving between Listings and Changes (§5).
CONFIG_PARAM = "config"
DISABLED_PARAM = "disabled"


@dataclass
class Sidebar:
    """What the left column renders, on whichever page is rendering it."""

    configurations: list  # every configuration, for the count on the toggle
    listed: list  # the ones actually shown
    selected: Optional[object] = None
    show_disabled: bool = False
    # Cars across every configuration, counted once. Not the sum of the rows:
    # a car matching two configurations belongs to both.
    total_cars: int = 0

    @property
    def selected_slug(self) -> str:
        return self.selected.slug if self.selected else configurations.ALL

    @property
    def hidden_count(self) -> int:
        return len(self.configurations) - len(self.listed)


def _sidebar(session: Session, config_key: str, show_disabled: bool) -> Sidebar:
    """Build the sidebar, and resolve `?config=` to a configuration.

    An unknown slug resolves to None — the "All configurations" scope — rather
    than 404ing. A configuration you archived and a link you kept are the
    common way to get here, and the sidebar right there showing what does exist
    is a better answer than an error page.
    """
    index = configurations.load_configurations(session)
    selected = configurations.find(index.configurations, config_key)
    return _sidebar_for(index, selected, show_disabled)


def _sidebar_for(index, selected, show_disabled: bool) -> Sidebar:
    """The sidebar around an already-resolved selection.

    Separate because `/search/<id>` resolves its configuration by walking the
    index rather than by matching a slug, and loading the index twice to render
    one column would be silly.
    """
    # A selected configuration is always listed, even when it is archived and
    # the toggle is off; scoping to something the sidebar doesn't show would
    # look like the page had lost its selection.
    listed = configurations.visible(index.configurations, show_disabled)
    if selected is not None and selected not in listed:
        listed = sorted(listed + [selected], key=lambda c: c.name.casefold())

    return Sidebar(
        configurations=index.configurations,
        listed=listed,
        selected=selected,
        show_disabled=show_disabled,
        total_cars=index.total_cars,
    )


def _configuration_of_search(sidebar: Sidebar) -> dict:
    """search_id -> the configuration it belongs to.

    Lets a card name the *other* configurations a car matches (§9.4). The
    relationship is already in the data — `search_ids` is on every merged
    record — but until now it was invisible: two configurations quietly
    describing the same car looked like one configuration doing so.
    """
    return {
        search_id: c
        for c in sidebar.configurations
        for search_id in c.search_ids
    }


def _also_in(car, by_search: dict, selected) -> list:
    """The configurations this car matches, other than the one you are in.

    Empty when nothing is scoped and the car matches only one: "also in" is
    only worth saying when it names something the current view does not.
    """
    found = {}
    for search_id in car.search_ids:
        configuration = by_search.get(search_id)
        if configuration is None:
            continue
        if selected is not None and configuration.slug == selected.slug:
            continue
        # An archived configuration is not collecting this car any more, so
        # "also in" it would be a claim about the past dressed as the present.
        if not configuration.enabled:
            continue
        found[configuration.slug] = configuration

    if selected is None and len(found) < 2:
        return []
    return sorted(found.values(), key=lambda c: c.name.casefold())


def _also_in_map(cars, sidebar: Sidebar) -> dict:
    """`car id -> other configurations it matches`, for the whole page."""
    by_search = _configuration_of_search(sidebar)
    return {c.id: _also_in(c, by_search, sidebar.selected) for c in cars}


def _scoped(cars, selected) -> list:
    """Narrow merged cars to one configuration.

    A merged car can come from several configurations at once; it belongs to
    this one if any of its rows does, because it genuinely turned up in it.
    """
    if selected is None:
        return list(cars)
    wanted = selected.search_ids
    return [c for c in cars if wanted & set(c.search_ids)]


@dataclass
class ListingRow:
    """A listing plus the price movement we can derive from its history."""

    listing: Listing
    first_price: Optional[float] = None
    previous_price: Optional[float] = None
    history_points: int = 0
    # Oldest-first observed prices, for the inline sparkline. Same series the
    # merged cards draw, so the two views can't disagree about a trace.
    prices: list = field(default_factory=list)

    @property
    def delta_since_first(self) -> Optional[float]:
        if self.first_price is None or self.listing.price is None:
            return None
        d = self.listing.price - self.first_price
        return d if abs(d) >= 0.01 else None

    @property
    def last_change(self) -> Optional[float]:
        if self.previous_price is None or self.listing.price is None:
            return None
        d = self.listing.price - self.previous_price
        return d if abs(d) >= 0.01 else None


def _price_context(session: Session, listings: list[Listing]) -> dict[int, ListingRow]:
    """Fetch price history for these listings in one query and fold it in.

    Doing this per listing would be a classic N+1; a search can hold hundreds of
    rows once it has been running a while.
    """
    rows = {x.id: ListingRow(listing=x) for x in listings}
    if not rows:
        return rows

    history = session.exec(
        select(PriceHistory)
        .where(PriceHistory.listing_id.in_(list(rows)))
        .order_by(PriceHistory.listing_id, PriceHistory.observed_at)
    ).all()

    by_listing: dict[int, list[PriceHistory]] = {}
    for h in history:
        by_listing.setdefault(h.listing_id, []).append(h)

    for listing_id, points in by_listing.items():
        row = rows[listing_id]
        row.history_points = len(points)
        row.first_price = points[0].price
        row.prices = [p.price for p in points]
        # The price before the current one — i.e. what the most recent change
        # moved away from.
        if len(points) >= 2:
            row.previous_price = points[-2].price

    return rows


SORT_KEYS = {
    "price": lambda r: (r.listing.price is None, r.listing.price),
    "year": lambda r: (r.listing.year is None, r.listing.year),
    "mileage": lambda r: (r.listing.mileage_km is None, r.listing.mileage_km),
    "title": lambda r: r.listing.title.casefold(),
    "location": lambda r: (r.listing.location or "").casefold(),
    "first_seen": lambda r: r.listing.first_seen,
    "last_seen": lambda r: r.listing.last_seen,
    "change": lambda r: (r.delta_since_first is None, r.delta_since_first or 0),
}


# ---------------------------------------------------------------------- routes


@dataclass
class MarketTab:
    """One market's tab on the Listings page."""

    key: str
    label: str
    count: int
    selected: bool
    url: str


# Sort options offered in the toolbar. Every one sorts ascending on its own key,
# with direction baked into the key function: "newest" and "biggest drop" are
# only meaningful one way round, and offering the reverse would be clutter.
LISTING_SORTS = {
    "newest": ("Newest first", lambda c: -c.first_seen.timestamp()),
    "price": ("Price", lambda c: (c.price is None, c.price or 0.0)),
    "mileage": (
        "Mileage",
        lambda c: (c.canonical.mileage_km is None, c.canonical.mileage_km or 0),
    ),
    "year": ("Year", lambda c: (c.canonical.year is None, -(c.canonical.year or 0))),
    "drop": (
        "Biggest drop",
        lambda c: (c.delta_since_first is None, c.delta_since_first or 0.0),
    ),
}

DEFAULT_SORT = "newest"

# Cards or compact rows. This is a per-viewer preference, not part of the
# linkable filter state, so it does not belong in the query string - but it does
# have to reach the server, because rendering both views doubled the Germany tab
# to 528KB for 173 cars. A cookie is the one place a per-browser preference can
# live that the server can also read. `localStorage` cannot: only the browser
# ever sees it.
VIEW_COOKIE = "carwatch-view"
VIEWS = ("cards", "compact")
DEFAULT_VIEW = "cards"


def _view_preference(request: Request) -> str:
    """Which of the two listing views to render. Unknown values fall back."""
    value = request.cookies.get(VIEW_COOKIE)
    return value if value in VIEWS else DEFAULT_VIEW


def _history_by_listing(
    session: Session, listings: list[Listing]
) -> dict[int, list[PriceHistory]]:
    """All price history for these listings, in one query, oldest first."""
    if not listings:
        return {}
    rows = session.exec(
        select(PriceHistory)
        .where(PriceHistory.listing_id.in_([x.id for x in listings]))
        .order_by(PriceHistory.listing_id, PriceHistory.observed_at)
    ).all()
    out: dict[int, list[PriceHistory]] = {}
    for row in rows:
        out.setdefault(row.listing_id, []).append(row)
    return out


def _median(values: list[float]) -> Optional[float]:
    """Plain median; a mean would be dragged around by one outlier ad."""
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _eur_prices(cars) -> list[float]:
    """Active EUR prices only — folding a RON figure into a EUR median would
    quietly produce a nonsense number."""
    return [
        c.price
        for c in cars
        if c.is_active and c.price is not None and (c.currency or "EUR") == "EUR"
    ]


def _left_out(cars) -> dict[str, int]:
    """Active cars priced in something other than EUR, counted by currency.

    `_eur_prices` keeps them out of every median and range rather than fold lei
    into euros — right, but silent: a RON-priced olx ad simply vanished from
    the figures. Counting them lets the page say so. Takes merged cars or
    listing rows; both carry `price`, `currency` and `is_active`.
    """
    out: dict[str, int] = {}
    for c in cars:
        if c.is_active and c.price is not None and (c.currency or "EUR") != "EUR":
            out[c.currency] = out.get(c.currency, 0) + 1
    return dict(sorted(out.items()))


def _plain(value):
    """A whole float renders as an int, so query strings stay readable."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


@dataclass
class ListingFilters:
    """The toolbar state. Lives in the query string, so a view is linkable."""

    q: str = ""
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    year_min: Optional[int] = None
    year_max: Optional[int] = None
    mileage_max: Optional[int] = None
    fuel: str = ""
    # The sidebar's selection, carried so that every link the toolbar builds
    # keeps the scope you are in (§5). A slug, not a name — see web/configurations.
    config: str = ""
    # Whether the sidebar is listing archived configurations. Not a filter on
    # the cars at all; it rides here because every link is built from this
    # object and losing it would collapse the sidebar on the next click.
    disabled: bool = False
    sort: str = DEFAULT_SORT

    @property
    def active(self) -> bool:
        """Whether anything is narrowing the list — drives the "clear" link."""
        return bool(
            self.q
            or self.fuel
            or self.config
            or self.price_min is not None
            or self.price_max is not None
            or self.year_min is not None
            or self.year_max is not None
            or self.mileage_max is not None
        )

    def query(self, **overrides) -> str:
        """This state as a query string, with overrides applied.

        The market tabs are built from it, so switching market keeps the filters
        you set rather than silently resetting them.
        """
        pairs = {
            "q": self.q,
            # Whole prices go into the URL as "30000", not "30000.0".
            "price_min": _plain(self.price_min),
            "price_max": _plain(self.price_max),
            "year_min": self.year_min,
            "year_max": self.year_max,
            "mileage_max": self.mileage_max,
            "fuel": self.fuel,
            "config": self.config,
            "disabled": "1" if self.disabled else "",
            # The default is left out so a plain "/" stays a plain "/".
            "sort": self.sort if self.sort != DEFAULT_SORT else "",
        }
        pairs.update(overrides)
        return urlencode({k: v for k, v in pairs.items() if v not in (None, "")})


def _apply_filters(cars, filters: ListingFilters):
    """Narrow the merged cars in Python.

    Tens to a few hundred records for a single-user local tool, so filtering
    here costs nothing — and the merge has already happened in memory, so a SQL
    filter could not see a merged car's other sites in any case.
    """
    out = list(cars)

    needle = filters.q.strip().casefold()
    if needle:
        out = [
            c
            for c in out
            if needle in c.canonical.title.casefold()
            or needle in (c.canonical.location or "").casefold()
        ]

    if filters.price_min is not None:
        out = [c for c in out if c.price is not None and c.price >= filters.price_min]
    if filters.price_max is not None:
        out = [c for c in out if c.price is not None and c.price <= filters.price_max]

    if filters.year_min is not None:
        out = [
            c
            for c in out
            if c.canonical.year is not None and c.canonical.year >= filters.year_min
        ]
    if filters.year_max is not None:
        out = [
            c
            for c in out
            if c.canonical.year is not None and c.canonical.year <= filters.year_max
        ]

    if filters.mileage_max is not None:
        out = [
            c
            for c in out
            if c.canonical.mileage_km is not None
            and c.canonical.mileage_km <= filters.mileage_max
        ]

    if filters.fuel:
        wanted = filters.fuel.casefold()
        out = [c for c in out if (c.canonical.fuel or "").casefold() == wanted]

    return out


@dataclass(frozen=True)
class ListingsPage:
    """Which of the two pages built from the listing grid is rendering.

    Favorites is the Listings page narrowed to the cars you starred — same
    toolbar, tabs, cards and rows — so it is the same code with a different
    path. The path matters because every link the page builds (tabs, "clear",
    the address bar htmx pushes) has to stay on the page you are on.
    """

    key: str
    path: str
    title: str


LISTINGS = ListingsPage("listings", "/", "Listings")
FAVORITES = ListingsPage("favorites", "/favorites", "Favorites")


def _page_url(page: ListingsPage, query: str) -> str:
    return f"{page.path}?{query}" if query else page.path


@router.get("/", response_class=HTMLResponse)
def listings(
    request: Request,
    market: str = Query(""),
    q: str = Query(""),
    price_min: OptFloat = None,
    price_max: OptFloat = None,
    year_min: OptInt = None,
    year_max: OptInt = None,
    mileage_max: OptInt = None,
    fuel: str = Query(""),
    config: str = Query(""),
    disabled: OptInt = None,
    sort: str = Query(DEFAULT_SORT),
) -> HTMLResponse:
    """The Listings page: one card per car, per market (front-end plan §6)."""
    return _listings_page(
        request, LISTINGS, market=market, q=q, price_min=price_min,
        price_max=price_max, year_min=year_min, year_max=year_max,
        mileage_max=mileage_max, fuel=fuel, config=config, disabled=disabled,
        sort=sort,
    )


@router.get("/favorites", response_class=HTMLResponse)
def favorites(
    request: Request,
    market: str = Query(""),
    q: str = Query(""),
    price_min: OptFloat = None,
    price_max: OptFloat = None,
    year_min: OptInt = None,
    year_max: OptInt = None,
    mileage_max: OptInt = None,
    fuel: str = Query(""),
    config: str = Query(""),
    disabled: OptInt = None,
    sort: str = Query(DEFAULT_SORT),
) -> HTMLResponse:
    """The cars you starred, as the Listings page shows cars.

    Sold ones stay, in their own section: that a favourite has gone is exactly
    what you want to know about it. So do favourites whose rows no longer
    exist at all (a deleted configuration), listed by the title they had.
    """
    return _listings_page(
        request, FAVORITES, market=market, q=q, price_min=price_min,
        price_max=price_max, year_min=year_min, year_max=year_max,
        mileage_max=mileage_max, fuel=fuel, config=config, disabled=disabled,
        sort=sort,
    )


def _is_favorite(car, keys: set[tuple[str, str]]) -> bool:
    """A car is a favourite if any of its ads is — star the olx copy of an
    autovit ad and the merged card is starred."""
    return any((m.site, m.site_listing_id) in keys for m in car.members)


def _listings_page(
    request: Request,
    page: ListingsPage,
    *,
    market: str,
    q: str,
    price_min: Optional[float],
    price_max: Optional[float],
    year_min: Optional[int],
    year_max: Optional[int],
    mileage_max: Optional[int],
    fuel: str,
    config: str,
    disabled: Optional[int],
    sort: str,
) -> HTMLResponse:
    if sort not in LISTING_SORTS:
        sort = DEFAULT_SORT

    with _session(request) as session:
        sidebar = _sidebar(session, config, bool(disabled))
        rows = session.exec(select(Listing)).all()
        history = _history_by_listing(session, rows)
        favorite_rows = session.exec(select(Favorite).order_by(Favorite.created_at)).all()

    favorite_keys = {(f.site, f.site_listing_id) for f in favorite_rows}

    filters = ListingFilters(
        q=q,
        price_min=price_min,
        price_max=price_max,
        year_min=year_min,
        year_max=year_max,
        mileage_max=mileage_max,
        fuel=fuel,
        # The resolved slug, not what was typed: a stale `?config=` in a
        # bookmark otherwise reappears in every link this page builds.
        config=sidebar.selected.slug if sidebar.selected else "",
        disabled=sidebar.show_disabled,
        sort=sort,
    )

    # Group first, merge second: Romanian and German inventory are never the
    # same car, so they must never share a merge group (§2).
    by_market: dict[str, list[Listing]] = {}
    for row in rows:
        by_market.setdefault(market_of(row.site), []).append(row)

    # Scoping happens before anything is counted, so the market tabs, the
    # totals and the Romanian reference all describe the configuration you
    # selected rather than the whole database.
    merged = {
        key: _scoped(merge_listings(v, history), sidebar.selected)
        for key, v in by_market.items()
    }

    # The Romanian reference is the whole Romanian market on either page: a
    # median over only your favourites would describe your taste, not prices.
    reference_pool = merged.get("ro", [])

    if page is FAVORITES:
        merged = {
            key: kept
            for key, cars_in_market in merged.items()
            if (kept := [c for c in cars_in_market if _is_favorite(c, favorite_keys)])
        }

    # Known markets in their declared order, then anything unexpected, so a site
    # added to config.yaml without a market shows up rather than vanishing.
    ordered = [k for k in MARKETS if k in merged] + sorted(
        k for k in merged if k not in MARKETS
    )

    if market not in ordered:
        market = ordered[0] if ordered else "ro"

    cars = merged.get(market, [])
    all_active = [c for c in cars if c.is_active]
    delisted = [c for c in cars if not c.is_active]

    # Fuel choices come from what this market actually holds, before filtering:
    # a dropdown that drops an option the moment you use another filter is
    # infuriating to operate.
    fuel_options = sorted({c.canonical.fuel for c in all_active if c.canonical.fuel})

    active = _apply_filters(all_active, filters)
    active.sort(key=LISTING_SORTS[sort][1])
    delisted.sort(key=lambda c: c.last_seen, reverse=True)

    tabs = [
        MarketTab(
            key=key,
            label=MARKET_LABELS.get(key, key),
            count=sum(1 for c in merged[key] if c.is_active),
            selected=key == market,
            url=_page_url(page, filters.query(market=key)),
        )
        for key in ordered
    ]

    prices = _eur_prices(active)
    stats = {
        "count": len(active),
        "total": len(all_active),
        "min": min(prices) if prices else None,
        "max": max(prices) if prices else None,
        "median": _median(prices),
        "drops": sum(1 for c in active if c.is_drop),
        "left_out": _left_out(active),
    }

    # The cross-market reference the Germany tab marks against. Deliberately
    # computed over all Romanian cars rather than a filtered subset — the
    # reference must not move when you narrow the German list. It is a raw
    # list-price median: German ads are frequently pre-VAT and exclude import
    # and registration cost, so this is not landed cost, and the page says so.
    reference_median = (
        _median(_eur_prices(reference_pool)) if market != "ro" else None
    )

    # The toolbar filters live, so htmx asks for the same URL and swaps in only
    # the part a filter changes. Rendering the whole page and letting the
    # browser replace it would take the focus out of the box being typed in;
    # this way the toolbar is never re-rendered at all.
    fragment = request.headers.get("HX-Request") == "true"

    response = _render(
        request,
        "partials/listing_results.html" if fragment else "listings.html",
        # The tabs and the "clear" link sit outside the swapped region but
        # depend on the filters, so the fragment sends them back out of band.
        oob=fragment,
        title=page.title,
        page=page,
        favorite_ids={c.id for c in cars if _is_favorite(c, favorite_keys)},
        # Favourites with no row left anywhere — their configuration was
        # deleted. Only Favorites lists them; they are nobody else's business.
        orphans=(
            [
                fav
                for fav in favorite_rows
                if (fav.site, fav.site_listing_id)
                not in {(r.site, r.site_listing_id) for r in rows}
            ]
            if page is FAVORITES
            else []
        ),
        view=_view_preference(request),
        tabs=tabs,
        market=market,
        market_label=MARKET_LABELS.get(market, market),
        cars=active,
        delisted=delisted,
        stats=stats,
        reference_median=reference_median,
        filters=filters,
        sorts=[(key, label) for key, (label, _) in LISTING_SORTS.items()],
        fuel_options=fuel_options,
        sidebar=sidebar,
        also_in=_also_in_map(active, sidebar),
    )

    if fragment:
        # htmx would otherwise push the form's own serialisation, which includes
        # every empty box — `?q=tekna&price_min=&price_max=&year_min=...`. The
        # address bar is a thing the user reads and copies, so it gets the same
        # tidy URL the market tabs are built from.
        response.headers["HX-Push-Url"] = _page_url(page, filters.query(market=market))

    return response


# ------------------------------------------------------------- favourites


def _merged_members(session: Session, listing: Listing) -> list[Listing]:
    """Every row shown as the same car as `listing` — the merge, recomputed.

    Merging is per request and never stored (§5), so the only way to know which
    ads a card stands for is to merge its market again.
    """
    market = market_of(listing.site)
    rows = [r for r in session.exec(select(Listing)).all() if market_of(r.site) == market]
    for car in merge_listings(rows):
        if any(m.id == listing.id for m in car.members):
            return car.members
    return [listing]


@router.post("/favorite/{listing_id}", response_class=HTMLResponse)
def toggle_favorite(request: Request, listing_id: int) -> HTMLResponse:
    """Star or unstar a car. Returns the star, which htmx swaps in place.

    Starring records the clicked row's ad (see `models.Favorite` for why an ad
    and not a row). Unstarring clears every ad in the car, so a star added from
    the olx copy of an autovit ad comes off from either.
    """
    db_file = request.app.state.config.db_file
    with session_scope(db_file) as session:
        listing = session.get(Listing, listing_id)
        if listing is None:
            raise HTTPException(status_code=404, detail="No such listing")

        ads = {(m.site, m.site_listing_id) for m in _merged_members(session, listing)}
        starred = [
            fav
            for fav in session.exec(
                select(Favorite).where(
                    Favorite.site_listing_id.in_([ad_id for _, ad_id in ads])
                )
            ).all()
            if (fav.site, fav.site_listing_id) in ads
        ]

        if starred:
            for fav in starred:
                session.delete(fav)
            on = False
        else:
            session.add(
                Favorite(
                    site=listing.site,
                    site_listing_id=listing.site_listing_id,
                    title=listing.title,
                    url=listing.url,
                )
            )
            on = True

    return _render(request, "partials/fav_star.html", car_id=listing_id, on=on)


@router.post("/favorites/forget", response_class=HTMLResponse)
def forget_favorite(
    request: Request, site: str = Form(...), site_listing_id: str = Form(...)
) -> HTMLResponse:
    """Drop a favourite whose car is no longer in the database at all.

    Those have no card to unstar, so the Favorites page lists them with this
    button instead.
    """
    db_file = request.app.state.config.db_file
    with session_scope(db_file) as session:
        for fav in session.exec(
            select(Favorite).where(
                Favorite.site == site, Favorite.site_listing_id == site_listing_id
            )
        ).all():
            session.delete(fav)
    return RedirectResponse("/favorites", status_code=303)


# ------------------------------------------------------ the configuration page


# How far back "this week" reaches, for the price summary and the change counts
# on a configuration's page. Seven days because collection is daily and manual:
# a shorter window would be empty on any day you didn't run it.
WEEK = timedelta(days=7)


def _price_summary(
    cars, history: dict[int, list[PriceHistory]], now: datetime
) -> dict:
    """What this configuration's asking prices have done over the week (§6).

    Medians, not means: one mispriced ad drags a mean around, and the question
    is where the middle of the segment sits.
    """
    active = [c for c in cars if c.is_active]
    current = _eur_prices(active)

    # The same cars' prices a week ago, so the comparison is like for like — a
    # median over a different set of cars would move when the inventory
    # changed rather than when prices did.
    then: list[float] = []
    for car in active:
        if (car.currency or "EUR") != "EUR":
            continue
        points = [
            p
            for listing in car.members
            for p in history.get(listing.id, [])
            if p.observed_at <= now - WEEK
        ]
        if points:
            then.append(max(points, key=lambda p: p.observed_at).price)

    median_now = _median(current)
    median_then = _median(then)

    return {
        "cars": len(active),
        "median": median_now,
        "median_then": median_then,
        # Only when both exist, and only over the cars present for both — a
        # "down 4 000" that is really "the expensive ones sold" would be a lie.
        "median_delta": (
            median_now - median_then
            if median_now is not None and median_then is not None and len(then) >= 3
            else None
        ),
        "compared": len(then),
        "left_out": _left_out(active),
        "low": min(current) if current else None,
        "high": max(current) if current else None,
    }


# ---------------------------------------------- creating and editing (§7)

# A configuration holds at most one source per site and there are three known
# sites, so the form is three fixed rows rather than a repeatable list. It
# cannot then express "the same site twice", which is a rule the config parser
# would otherwise have to reject after the fact.
FORM_SITES = tuple(KNOWN_SITES)


def _config_path(request: Request):
    return request.app.state.config.path


def _reload_config(request: Request) -> None:
    """Re-read config.yaml after a write, and mirror it into the database.

    Without this the app would keep serving the configuration list it loaded at
    startup: a configuration you just created would not appear, and collecting
    it would collect the old file. Same `sync_searches` the collector runs, so
    a new source becomes a `search` row exactly as it would on the next run.
    """
    config = load_config(_config_path(request))
    request.app.state.config = config
    request.app.state.jobs.config = config
    with session_scope(config.db_file) as session:
        sync_searches(session, config)


def _draft_from_form(
    form, base: Optional[SearchConfig] = None, enabled: bool = True
) -> SearchDraft:
    """The posted form as a draft. No validation here — that is the writer's.

    The form is a name and three links, nothing else. Whether a configuration
    collects is Archive's and Restore's to say, so the caller passes it in; the
    description is read off the links (`_description`). Anything else posted
    is ignored rather than written.
    """
    sources = []
    for site in FORM_SITES:
        url = (form.get(f"url_{site}") or "").strip()
        if url:
            sources.append(Source(site=site, url=url))

    return SearchDraft(
        name=form.get("name") or "",
        sources=sources,
        enabled=enabled,
        metadata=_description(sources, base),
    )


def _urls(sources) -> dict[str, str]:
    return {s.site: s.url for s in sources}


def _description(sources: list[Source], base: Optional[SearchConfig]) -> dict:
    """A configuration's display-only description, read off its links.

    Links unchanged — a rename, or a duplicate saved as it was — and the
    description is kept exactly as it is, so a save never rewrites what
    someone wrote into config.yaml by hand. Otherwise it is re-read: each key
    from the first link, in form order, that states it, with anything no link
    states (a trim written by hand, a year cap autovit's URL cannot carry)
    carried over from before.
    """
    before = dict(base.metadata) if base is not None else {}
    if base is not None and _urls(base.sources) == _urls(sources):
        return before

    read: dict = {}
    for source in sources:
        for key, value in search_urls.parse_url(source.site, source.url).as_metadata().items():
            read.setdefault(key, value)
    return {**before, **read}


def _form_context(request: Request, draft: SearchDraft, **extra) -> dict:
    """Everything `configuration_form.html` needs, however it was reached."""
    path = _config_path(request)
    return {
        "draft": draft,
        "urls": {s.site: s.url for s in draft.sources},
        "sites": FORM_SITES,
        # The file as it was when this form was rendered. Checked on save, so
        # the dashboard cannot silently clobber an editor window (§4).
        "stamp": FileStamp.of(path).token,
        "config_file": path.name,
        # Each site's standing rules, shown against the box you are about to
        # paste into. Standing rather than URL-specific on purpose: they must
        # reach you *before* you build a search you cannot use, and hint text
        # that rewrote itself as you typed would be worse than useless.
        "site_notes": {site: site_rules.standing(site) for site in FORM_SITES},
        **extra,
    }


@router.get("/config/new", response_class=HTMLResponse)
def new_configuration(request: Request, duplicate: str = Query("")) -> HTMLResponse:
    """A blank form, or one pre-filled from an existing configuration.

    Duplicating is how the "same but 2024+" variant in the current config came
    about in the first place (§7), so it is a link rather than a copy-paste job.
    """
    source_name = ""
    draft = SearchDraft(name="", sources=[], enabled=True)

    if duplicate:
        with _session(request) as session:
            index = configurations.load_configurations(session)
        found = configurations.find(index.configurations, duplicate)
        if found is not None:
            existing = request.app.state.config.find_search(found.name)
            if existing is not None:
                draft = SearchDraft.from_config(existing)
                source_name = existing.name
                draft.name = f"{existing.name} (copy)"

    with _session(request) as session:
        sidebar = _sidebar(session, duplicate, False)

    return _render(
        request,
        "configuration_form.html",
        title="New configuration",
        sidebar=sidebar,
        original_name="",  # a duplicate creates, it does not overwrite
        duplicate_of=source_name,
        **_form_context(request, draft),
    )


@router.get("/config/{slug}/edit", response_class=HTMLResponse)
def edit_configuration(request: Request, slug: str, disabled: OptInt = None) -> HTMLResponse:
    with _session(request) as session:
        sidebar = _sidebar(session, slug, bool(disabled))
    if sidebar.selected is None:
        raise HTTPException(status_code=404, detail="No such configuration")

    existing = request.app.state.config.find_search(sidebar.selected.name)
    if existing is None:
        # In the database but not in config.yaml: a configuration whose block
        # was deleted by hand. Its rows and history survive, but there is
        # nothing to edit.
        raise HTTPException(
            status_code=404,
            detail=f"{sidebar.selected.name!r} is no longer in the config file.",
        )

    return _render(
        request,
        "configuration_form.html",
        title=f"Edit {existing.name}",
        sidebar=sidebar,
        original_name=existing.name,
        duplicate_of="",
        **_form_context(request, SearchDraft.from_config(existing)),
    )


@router.post("/config/save", response_class=HTMLResponse)
async def save_configuration(request: Request) -> HTMLResponse:
    """Write the form to config.yaml, then collect it.

    Read straight off the request rather than through `Form(...)` parameters:
    the URL boxes are one per known site, and declaring them as arguments would
    mean editing this signature whenever a site is added.

    A refused write re-renders the form with the message and everything the
    user typed still in the boxes. Losing a pasted URL to a validation error
    would be a small betrayal every time it happened.
    """
    form = await request.form()
    original_name = (form.get("original_name") or "").strip()
    duplicate_of = (form.get("duplicate_of") or "").strip()

    config = request.app.state.config
    existing = config.find_search(original_name) if original_name else None
    # A duplicate starts from what it copied, so its description comes along.
    base = existing or (config.find_search(duplicate_of) if duplicate_of else None)
    # An edit keeps whatever state it was in; a new configuration collects.
    draft = _draft_from_form(
        form, base, enabled=existing.enabled if existing is not None else True
    )

    try:
        save_search(
            _config_path(request),
            draft,
            original_name=original_name or None,
            expected=FileStamp.parse(form.get("stamp")),
        )
    except ConfigWriteError as exc:
        with _session(request) as session:
            sidebar = _sidebar(session, original_name, False)
        return _render(
            request,
            "configuration_form.html",
            title="New configuration" if not original_name else f"Edit {original_name}",
            sidebar=sidebar,
            original_name=original_name,
            duplicate_of=duplicate_of,
            error=str(exc),
            **_form_context(request, draft),
        )

    _reload_config(request)

    # Collect it straight away, so you find out whether the URLs work now
    # rather than after the next manual run. The job is a background thread —
    # this redirect does not wait for it, and the run strip reports it. Not for
    # an archived configuration: `collect()` would skip it and say nothing.
    if draft.enabled:
        _runner(request).start(label=draft.name, search_names=[draft.name])

    return RedirectResponse(
        f"/config/{configurations.slugify(draft.name)}", status_code=303
    )


@router.post("/config/{slug}/archive", response_class=HTMLResponse)
def archive_configuration(request: Request, slug: str) -> HTMLResponse:
    """Retire a configuration — the reversible step.

    Archiving disables it in config.yaml; `sync_searches` then disables its
    `search` rows, and its listings and price history stay exactly where they
    are. Restore brings it back intact. Deleting is a separate page, offered
    only once a configuration is archived, so destroying history always takes
    two decisions rather than one click.
    """
    return _set_configuration_enabled(request, slug, False)


@router.post("/config/{slug}/restore", response_class=HTMLResponse)
def restore_configuration(request: Request, slug: str) -> HTMLResponse:
    """Switch an archived configuration back on, history intact."""
    return _set_configuration_enabled(request, slug, True)


def _set_configuration_enabled(request: Request, slug: str, enabled: bool):
    with _session(request) as session:
        sidebar = _sidebar(session, slug, False)
    if sidebar.selected is None:
        raise HTTPException(status_code=404, detail="No such configuration")

    name = sidebar.selected.name
    try:
        set_enabled(_config_path(request), name, enabled)
    except ConfigWriteError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    _reload_config(request)
    return RedirectResponse(f"/config/{slug}?disabled=1", status_code=303)


@router.get("/config/{slug}/delete", response_class=HTMLResponse)
def confirm_delete(request: Request, slug: str) -> HTMLResponse:
    """The one page that destroys price history, and exactly how much.

    Linked only from an archived configuration's page. It counts the rows
    first, so the decision is made looking at "212 price observations" rather
    than at a name.
    """
    return _delete_page(request, slug)


def _delete_page(
    request: Request, slug: str, error: Optional[str] = None, status_code: int = 200
) -> HTMLResponse:
    with _session(request) as session:
        sidebar = _sidebar(session, slug, False)
        if sidebar.selected is None:
            raise HTTPException(status_code=404, detail="No such configuration")
        footprint = configuration_footprint(session, sidebar.selected.name)

    config = request.app.state.config
    in_file = config.find_search(sidebar.selected.name) is not None
    path = _config_path(request)

    response = _render(
        request,
        "configuration_delete.html",
        title=f"Delete {sidebar.selected.name}",
        sidebar=sidebar,
        configuration=sidebar.selected,
        footprint=footprint,
        in_file=in_file,
        # The writer refuses this too; saying so up front beats a button that
        # can only fail.
        only_one=in_file and len(config.searches) == 1,
        stamp=FileStamp.of(path).token,
        config_file=path.name,
        db_name=Path(config.db_file).name,
        keep_db_backups=KEEP_DB_BACKUPS,
        error=error,
    )
    response.status_code = status_code
    return response


@router.post("/config/{slug}/delete", response_class=HTMLResponse)
def delete_configuration(
    request: Request, slug: str, stamp: str = Form("")
) -> HTMLResponse:
    """Delete an archived configuration: its block in config.yaml, then its rows.

    The file goes first, because that half carries the guards — the stamp, the
    running-collection check, the refusal to leave `searches:` empty — and a
    backup. If it refuses, nothing has been touched. If the rows then fail to
    go (a collection started in the gap), the configuration is out of the file
    but still in the database, where it shows as archived and this same page
    finishes the job.

    The rows are deleted holding the collection lock, so a run cannot start
    halfway through and write events against listings that are going away.
    """
    with _session(request) as session:
        sidebar = _sidebar(session, slug, False)
    selected = sidebar.selected
    if selected is None:
        raise HTTPException(status_code=404, detail="No such configuration")
    if selected.enabled:
        return _delete_page(
            request,
            slug,
            error=(
                "It is still collecting. Archive it first — only an archived "
                "configuration can be deleted."
            ),
            status_code=409,
        )

    name = selected.name
    db_file = request.app.state.config.db_file

    # Before anything goes: the copy that makes a delete undoable. First, so a
    # backup that fails stops the delete with nothing touched.
    try:
        backup = backup_database(db_file)
    except (OSError, sqlite3.Error) as exc:
        return _delete_page(
            request,
            slug,
            error=f"The database could not be backed up, so nothing was deleted: {exc}",
            status_code=409,
        )

    try:
        if request.app.state.config.find_search(name) is not None:
            try:
                delete_search(
                    _config_path(request), name, expected=FileStamp.parse(stamp)
                )
            except NoSuchSearchError:
                pass  # removed by hand since the app loaded it; the rows remain
            _reload_config(request)

        with collection_lock(db_file, source="dashboard (delete)"):
            with session_scope(db_file) as session:
                gone = purge_configuration(session, name)
    except (ConfigWriteError, CollectionBusy) as exc:
        return _delete_page(request, slug, error=str(exc), status_code=409)

    log.info(
        "Deleted configuration %r: %d listings, %d price observations, %d runs "
        "(database backed up to %s)",
        name,
        gone.listings,
        gone.prices,
        gone.runs,
        backup.name,
    )
    return RedirectResponse("/", status_code=303)


@router.get("/config/{slug}/export")
def export_configuration(request: Request, slug: str) -> PlainTextResponse:
    """This configuration as a YAML snippet, comments and all (§9.6).

    Useful for moving one between machines, and the fallback if the write path
    ever misbehaves: whatever else breaks, a configuration can be read out and
    pasted in by hand.
    """
    with _session(request) as session:
        sidebar = _sidebar(session, slug, False)
    if sidebar.selected is None:
        raise HTTPException(status_code=404, detail="No such configuration")

    try:
        snippet = export_search(_config_path(request), sidebar.selected.name)
    except ConfigWriteError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return PlainTextResponse(snippet)


@router.post("/config/test-url", response_class=HTMLResponse)
async def test_url(request: Request) -> HTMLResponse:
    """Fetch a pasted URL once and report what it would find. Writes nothing.

    The feature that earns this phase (§7). A URL that silently returns zero is
    the commonest way a configuration fails, and until now you only found out
    after a full collection — or, in the case autovit relaxing a search, four
    days later and only if you read a note on the Runs page.

    One page, not the whole paginated search: the question is "does this URL
    parse and match anything", and asking for fifty pages to answer it would be
    rude to the site for no extra information.

    That was this docstring's claim before it was true — `fetch_listings`
    paginates to its cap, so every press of this button walked the entire
    search. `max_pages` is what makes the sentence honest.
    """
    form = await request.form()
    site = (form.get("site") or "").strip().lower()
    # The form's URL boxes are named per site (`url_autovit`), because all
    # three are in one form; `hx-include` sends the box under that name. A
    # plain `url` is accepted too, so the endpoint can be driven directly.
    url = (form.get(f"url_{site}") or form.get("url") or "").strip()

    settings = request.app.state.config.settings

    result = {"site": site, "url": url, "notes": []}
    if site not in KNOWN_SITES:
        result["error"] = f"Unknown site {site!r}."
    elif not url:
        result["error"] = "Paste a search URL first."
    elif PLACEHOLDER in url:
        result["error"] = f"That is still the {PLACEHOLDER} placeholder."
    else:
        # Checked *before* the request. The whole point of a rule about what we
        # may fetch is that we do not fetch it in order to find out.
        result["notes"] = site_rules.check_url(site, url)

    fetchable = "error" not in result and not any(
        n.blocking for n in result["notes"]
    )
    if fetchable:
        try:
            adapter = get_adapter(site, settings.for_site(site))
            # In a threadpool, not on the event loop. olx renders in a real
            # browser through Playwright's *sync* API, which refuses to run
            # inside a running asyncio loop — "use the Async API instead". So
            # this button worked for autovit and mobile.de and failed for olx
            # with a Playwright error, which is exactly the site it exists for.
            listings = await run_in_threadpool(
                adapter.fetch_listings, url, max_pages=1
            )
        except BlockedError as exc:
            result["blocked"] = str(exc)
        except AdapterError as exc:
            result["error"] = str(exc)
        except Exception as exc:  # noqa: BLE001 - a probe must never 500 the page
            log.exception("Test-URL failed for %s", url)
            result["error"] = f"{type(exc).__name__}: {exc}"
        else:
            relaxation = getattr(adapter, "last_relaxation", None)
            result["found"] = len(listings)
            result["relaxed"] = bool(relaxation)
            result["rule"] = (relaxation or {}).get("rule")
            result["sample"] = [
                (x.title, x.price, x.currency, x.year) for x in listings[:5]
            ]

    return _render(request, "partials/url_test.html", result=result)


# --------------------------------------------------- the price trend (§9.3)

# How far the trend looks back. Two weeks is long enough to see a direction and
# short enough that every point has cars behind it — the database is days old,
# not years, and a chart mostly made of gaps would suggest a precision the data
# doesn't have.
TREND_DAYS = 14

# Below this many cars a median is one ad's opinion, not the market's. Such a
# day is left out of the trace rather than drawn as a spike.
TREND_MIN_CARS = 3


@dataclass
class TrendPoint:
    day: date
    median: float
    cars: int
    # The middle half of that day's asking prices. Drawn as a band behind the
    # median, so a line that barely moves over a widening market reads as what
    # it is. Min and max would draw one optimistic seller as the market.
    low: float = 0.0
    high: float = 0.0


def _percentile(values: list[float], fraction: float) -> float:
    """Linear-interpolated percentile, for the quartile band."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


# The chart's own coordinate system. Drawn at this size and scaled by CSS, so
# the numbers below are whatever reads well rather than pixels on any screen.
CHART_WIDTH = 720
CHART_HEIGHT = 210
CHART_LEFT = 66      # room for the price labels
CHART_RIGHT = 10
CHART_TOP = 12
CHART_BOTTOM = 26    # room for the dates


@dataclass
class TrendChart:
    """Ready-to-draw geometry for the median-price chart.

    Computed here rather than in the template: the old chart was a polyline
    whose points were worked out in Jinja, which is why it had no scale, no
    points and nothing to read a value off.
    """

    dots: list = field(default_factory=list)   # one per day: x, y, and its point
    line: str = ""                             # polyline points for the median
    band: str = ""                             # polygon points for the quartile band
    ticks: list = field(default_factory=list)  # price gridlines: value and y
    width: int = CHART_WIDTH
    height: int = CHART_HEIGHT
    left: int = CHART_LEFT
    right: int = CHART_WIDTH - CHART_RIGHT


def _trend_chart(points: list) -> Optional[TrendChart]:
    """Turn daily medians into coordinates, or None when there is nothing to draw."""
    if len(points) < 2:
        return None

    low = min(p.low for p in points)
    high = max(p.high for p in points)
    if high - low < 1:                     # a flat fortnight still needs a scale
        low, high = low - 1, high + 1
    span = high - low

    plot_w = CHART_WIDTH - CHART_LEFT - CHART_RIGHT
    plot_h = CHART_HEIGHT - CHART_TOP - CHART_BOTTOM

    def x_of(index: int) -> float:
        return round(CHART_LEFT + plot_w * index / (len(points) - 1), 2)

    def y_of(value: float) -> float:
        return round(CHART_TOP + plot_h * (1 - (value - low) / span), 2)

    dots = [
        {"x": x_of(i), "y": y_of(p.median), "point": p}
        for i, p in enumerate(points)
    ]
    line = " ".join(f"{d['x']},{d['y']}" for d in dots)
    # Up along the top of the band, back along the bottom: one closed shape.
    tops = [f"{x_of(i)},{y_of(p.high)}" for i, p in enumerate(points)]
    bottoms = [f"{x_of(i)},{y_of(p.low)}" for i, p in reversed(list(enumerate(points)))]
    band = " ".join(tops + bottoms)

    ticks = [
        {"value": value, "y": y_of(value)}
        for value in (high, (high + low) / 2, low)
    ]
    return TrendChart(dots=dots, line=line, band=band, ticks=ticks)


def _price_on(car, history: dict[int, list[PriceHistory]], moment: datetime):
    """What this car was asking at `moment`, or None if it wasn't listed yet.

    The last observation at or before the moment — a price holds until it
    changes, which is the same assumption the per-listing chart draws with a
    stepped line.
    """
    best = None
    for listing in car.members:
        for point in history.get(listing.id, []):
            if point.observed_at <= moment and (
                best is None or point.observed_at > best.observed_at
            ):
                best = point
    return best.price if best else None


def _price_trend(cars, history: dict[int, list[PriceHistory]], now: datetime):
    """Median EUR asking price per day, for one configuration (§9.3).

    "Is this segment getting cheaper?" is the question the whole tool exists to
    answer, and it only became answerable once there was a week of history.

    Medians, and only over cars actually listed that day. A mean would follow
    one mispriced ad; including cars that had not appeared yet would draw the
    inventory growing as if it were prices moving.
    """
    points: list[TrendPoint] = []
    today = now.date()

    for offset in range(TREND_DAYS - 1, -1, -1):
        day = today - timedelta(days=offset)
        # End of that day, capped at now: today's point is "as of now", not a
        # projection to midnight.
        moment = min(datetime.combine(day, time.max), now)

        prices = []
        for car in cars:
            if (car.currency or "EUR") != "EUR":
                continue
            if car.first_seen > moment:
                continue  # not found yet
            if not car.is_active and car.last_seen < moment - timedelta(days=1):
                continue  # already gone
            price = _price_on(car, history, moment)
            if price is not None:
                prices.append(price)

        if len(prices) >= TREND_MIN_CARS:
            points.append(
                TrendPoint(
                    day=day,
                    median=_median(prices),
                    cars=len(prices),
                    low=_percentile(prices, 0.25),
                    high=_percentile(prices, 0.75),
                )
            )

    return points


# ---------------------------------------------------- reading a pasted link


@router.post("/config/read-link", response_class=HTMLResponse)
async def read_link(request: Request) -> HTMLResponse:
    """Say what a URL just pasted into a site's box reads as, and suggest a name.

    Creating a configuration is links only (2026-09-10): CarWatch builds no
    URLs, because a generated one that is wrong does not fail — it widens
    quietly. The form is a name and the links; the description is read off
    the links when you save (`_description`). This shows you what it will read
    before you do, and fills in the name if the box is still empty.

    Reads the URL string and fetches nothing. "Test this URL" is the button
    that asks the site.
    """
    form = await request.form()
    site = (form.get("site") or "").strip().lower()
    url = (form.get(f"url_{site}") or "").strip()

    if site not in KNOWN_SITES or not url:
        return HTMLResponse("")

    found_site = search_urls.site_of(url)
    read: dict = {}
    name = ""
    if found_site == site:
        criteria = search_urls.parse_url(site, url)
        read = criteria.as_metadata()
        if not (form.get("name") or "").strip():
            name = criteria.suggested_name()

    return _render(
        request,
        "partials/link_read.html",
        site=site,
        found_site=found_site,
        read=read,
        name=name,
    )


@router.get("/config/{slug}", response_class=HTMLResponse)
def configuration_page(
    request: Request,
    slug: str,
    disabled: OptInt = None,
) -> HTMLResponse:
    """One configuration: how it is doing, and what it has found (§6).

    This is the answer to "is this configuration working?", which until now was
    spread across the Runs health table, the Listings search picker and a note
    nobody read.
    """
    with _session(request) as session:
        sidebar = _sidebar(session, slug, bool(disabled))
        selected = sidebar.selected
        if selected is None:
            raise HTTPException(status_code=404, detail="No such configuration")

        rows = session.exec(
            select(Listing).where(Listing.search_id.in_(selected.search_ids))
        ).all()
        history = _history_by_listing(session, rows)

        events = session.exec(
            select(Event, Listing)
            .where(
                Event.listing_id == Listing.id,
                Listing.search_id.in_(selected.search_ids),
                Event.created_at >= utcnow() - WEEK,
            )
            .order_by(Event.created_at.desc(), Event.id.desc())
        ).all()
        baseline_run_ids = _baseline_run_ids(session)

    # Merged per market, as everywhere else: Romanian and German inventory are
    # never the same car.
    by_market: dict[str, list[Listing]] = {}
    for row in rows:
        by_market.setdefault(market_of(row.site), []).append(row)

    cars = [
        car
        for key in [k for k in MARKETS if k in by_market]
        + sorted(k for k in by_market if k not in MARKETS)
        for car in merge_listings(by_market[key], history)
    ]
    active = sorted([c for c in cars if c.is_active], key=lambda c: -c.first_seen.timestamp())
    delisted = sorted([c for c in cars if not c.is_active], key=lambda c: c.last_seen, reverse=True)

    groups = group_events(list(events))
    groups = [g for g in groups if not _is_baseline(g, baseline_run_ids)]
    changes = []
    for group in groups:
        event, listing = pick_representative(group)
        changes.append(
            ChangeRow(event=event, listing=listing, search=None, members=group)
        )

    week = {
        "drops": sum(1 for c in changes if c.event.type is EventType.PRICE_DOWN),
        "rises": sum(1 for c in changes if c.event.type is EventType.PRICE_UP),
        "arrivals": sum(1 for c in changes if c.event.type is EventType.NEW),
        "gone": sum(1 for c in changes if c.event.type is EventType.DELISTED),
    }

    trend = _price_trend(cars, history, utcnow())

    return _render(
        request,
        "configuration.html",
        title=selected.name,
        sidebar=sidebar,
        configuration=selected,
        cars=active,
        delisted=delisted,
        # The page is a summary, not a second Listings: enough cars to see what
        # the configuration holds, with a link to the full grid for the rest.
        shown=active[:CONFIG_PAGE_CARS],
        more=max(0, len(active) - CONFIG_PAGE_CARS),
        changes=changes[:CONFIG_PAGE_CHANGES],
        more_changes=max(0, len(changes) - CONFIG_PAGE_CHANGES),
        week=week,
        summary=_price_summary(cars, history, utcnow()),
        trend=trend,
        chart=_trend_chart(trend),
    )


# How much of a configuration's cars and changes the page shows before handing
# off to the full Listings grid and Changes feed.
CONFIG_PAGE_CARS = 12
CONFIG_PAGE_CHANGES = 10


@router.get("/search/{search_id}", response_class=HTMLResponse)
def search_detail(
    request: Request,
    search_id: int,
    sort: str = Query("price"),
    dir: str = Query("asc"),
    q: str = Query(""),
    price_max: OptFloat = None,
    year_min: OptInt = None,
    disabled: OptInt = None,
) -> HTMLResponse:
    """One source of one configuration, unmerged (§8).

    The only page reached by `search` row id rather than by configuration, and
    the only place you can see exactly what a single site returned at that
    site's own prices. It is a drill-down *from* a configuration, so it carries
    the sidebar and names the configuration it belongs to — otherwise the one
    row that links here is the one place the new navigation drops you out of.
    """
    if sort not in SORT_KEYS:
        sort = "price"
    descending = dir == "desc"

    with _session(request) as session:
        search = session.get(Search, search_id)
        if search is None:
            raise HTTPException(status_code=404, detail="No such search")

        index = configurations.load_configurations(session)
        configuration, source = configurations.owning(index.configurations, search_id)
        sidebar = _sidebar_for(index, configuration, bool(disabled))

        all_listings = session.exec(
            select(Listing).where(Listing.search_id == search_id)
        ).all()
        rows = _price_context(session, all_listings)

        active = [rows[x.id] for x in all_listings if x.is_active]
        delisted = [rows[x.id] for x in all_listings if not x.is_active]

        last_run = session.exec(
            select(Run).where(Run.search_id == search_id).order_by(Run.started_at.desc())
        ).first()
        filters = search_filters(search)

    # Filtering happens in Python: these are tens-to-hundreds of rows for a
    # single-user local tool, and it keeps the query simple.
    needle = q.strip().casefold()
    if needle:
        active = [
            r
            for r in active
            if needle in r.listing.title.casefold()
            or needle in (r.listing.location or "").casefold()
        ]
    if price_max is not None:
        active = [r for r in active if r.listing.price is not None and r.listing.price <= price_max]
    if year_min is not None:
        active = [r for r in active if r.listing.year is not None and r.listing.year >= year_min]

    active.sort(key=SORT_KEYS[sort], reverse=descending)
    delisted.sort(key=lambda r: r.listing.last_seen, reverse=True)

    # EUR only, like every other median in the dashboard: a RON price folded in
    # would produce a number that is neither. Anything else is counted instead.
    prices = _eur_prices([r.listing for r in active])
    stats = {
        "count": len(active),
        "total_active": sum(1 for x in all_listings if x.is_active),
        "min": min(prices) if prices else None,
        "max": max(prices) if prices else None,
        "median": _median(prices),
        "left_out": _left_out([r.listing for r in active]),
        "drops": sum(1 for r in active if (r.delta_since_first or 0) < 0),
    }

    return _render(
        request,
        "detail.html",
        title=f"{search.name} / {search.site}",
        search=search,
        sidebar=sidebar,
        configuration=configuration,
        source=source,
        filters=filters,
        rows=active,
        delisted=delisted,
        last_run=last_run,
        stats=stats,
        sort=sort,
        dir=dir,
        q=q,
        price_max=price_max,
        year_min=year_min,
    )


# ------------------------------------------------------------- the schedule


def _schedule_backend(request: Request):
    """The system scheduler for this machine. Tests put a fake on app.state."""
    return getattr(request.app.state, "schedule_backend", None) or scheduling.backend_for()


def _now(request: Request) -> datetime:
    """Local wall-clock time — what the schedule is set in. Tests fix it."""
    clock = getattr(request.app.state, "clock", None)
    return clock() if clock else datetime.now()


# How long the top bar trusts an answer. Asking Task Scheduler starts
# PowerShell — about a second — and the line is on every page.
SCHEDULE_CACHE_SECONDS = 60
# While a run is under way the line changes ("collecting at 19:07", then
# "collecting now"), so it is asked more often.
SCHEDULE_CACHE_LIVE_SECONDS = 20


def _cached_schedule_status(request: Request):
    cached = getattr(request.app.state, "schedule_cache", None)
    if cached is not None:
        taken, status = cached
        live = status is not None and (status.running or status.current)
        ttl = SCHEDULE_CACHE_LIVE_SECONDS if live else SCHEDULE_CACHE_SECONDS
        if monotonic() - taken < ttl:
            return status
    try:
        status = _schedule_backend(request).status()
    except scheduling.ScheduleError:
        status = None
    request.app.state.schedule_cache = (monotonic(), status)
    return status


def _seconds_to_collect(status, now: datetime) -> Optional[int]:
    """For the countdown: seconds until the run under way collects.

    Negative once it has begun — the browser then counts up instead. The
    browser counts from this number rather than from a time of day, so its
    clock never has to agree with this one.
    """
    current = status.current if status is not None else None
    if current is None:
        return None
    return int((current.collect_at - now).total_seconds())


def _schedule_panel(
    request: Request, message: Optional[str] = None, error: Optional[str] = None
) -> HTMLResponse:
    backend = _schedule_backend(request)
    try:
        status = backend.status()
        # Fresh from the source, so the top bar may as well have it too.
        request.app.state.schedule_cache = (monotonic(), status)
    except scheduling.ScheduleError as exc:
        status = scheduling.ScheduleStatus(
            system=getattr(backend, "system", ""),
            problem=f"Could not read the current schedule: {exc}",
        )

    # The scheduled command runs the wrapper in the project folder, which
    # collects the project's config.yaml. A dashboard started on another file
    # would otherwise show a schedule that does not collect what it shows.
    scheduled_config = (scheduling.PROJECT_ROOT / "config.yaml").resolve()
    other_config = Path(request.app.state.config.path).resolve() != scheduled_config
    now = _now(request)

    return _render(
        request,
        "partials/schedule_panel.html",
        status=status,
        message=message,
        error=error,
        every_hours=scheduling.EVERY_HOURS,
        jitter=scheduling.JITTER_MINUTES,
        other_config=other_config,
        scheduled_config=scheduled_config,
        now=now,
        seconds=_seconds_to_collect(status, now),
        day_label=scheduling.day_label,
    )


@router.get("/schedule", response_class=HTMLResponse)
def schedule_panel(request: Request) -> HTMLResponse:
    """The Runs page's schedule panel, fetched after the page has loaded.

    Asking Task Scheduler means starting PowerShell, which takes about a
    second; the run log should not wait for it.
    """
    return _schedule_panel(request)


@router.get("/schedule/next", response_class=HTMLResponse)
def next_scheduled_run(request: Request) -> HTMLResponse:
    """The top bar's line: the next run's window, or the run under way.

    "Next run 18:50" would be half true — each run starts collecting at a
    random minute in the twenty after that — so it says the window, and once
    a run has started and picked its minute, that minute.
    """
    status = _cached_schedule_status(request)
    now = _now(request)
    return _render(
        request,
        "partials/next_run.html",
        line=scheduling.summary(status, now),
        live=bool(status is not None and (status.running or status.current)),
        seconds=_seconds_to_collect(status, now),
    )


@router.post("/schedule", response_class=HTMLResponse)
def save_schedule(
    request: Request,
    mode: str = Form("off"),
    at: str = Form("08:00"),
    every_hours: OptFormInt = None,
) -> HTMLResponse:
    """Register the schedule with the system scheduler, or remove it.

    Not written anywhere else: the registration is the setting (see
    `carwatch/scheduling.py`).
    """
    try:
        schedule = scheduling.Schedule.from_form(mode, at, every_hours)
        _schedule_backend(request).apply(schedule)
    except (ValueError, scheduling.ScheduleError) as exc:
        return _schedule_panel(request, error=str(exc))
    finally:
        request.app.state.schedule_cache = None  # whatever happened, ask again

    if schedule.is_on:
        message = f"Saved — {schedule.describe().lower()}."
    else:
        message = "Schedule turned off. CarWatch collects only when you press Run."
    log.info("Schedule set: %s", schedule.describe())
    return _schedule_panel(request, message=message)


@router.get("/runs", response_class=HTMLResponse)
def runs_log(request: Request, limit: OptInt = None) -> HTMLResponse:
    """The run log: every run, newest first, whatever it belonged to.

    The per-source health table this page used to carry moved to each
    configuration's own page (§8) — "is this configuration collecting?" is a
    question about the configuration, and answering it here meant the answer
    sat two pages away from everything else about it.
    """
    limit = _clamp(limit, default=60, maximum=500)

    with _session(request) as session:
        runs = session.exec(select(Run).order_by(Run.started_at.desc()).limit(limit)).all()
        searches = session.exec(select(Search)).all()
        names = {s.id: s.name for s in searches}
        index = configurations.load_configurations(session)

    totals = {
        "searches": len(index.configurations),
        "sources": len(searches),
        # Rows, not cars: this page is about whether each source is collecting,
        # so the un-merged count is the honest one here.
        "active": sum(s.active_count for c in index.configurations for s in c.sources),
        "problems": sum(1 for c in index.configurations if c.problems),
    }

    return _render(
        request,
        "runs.html",
        runs=runs,
        names=names,
        config_links=[(c.name, c.slug) for c in index.configurations],
        totals=totals,
        title="Runs",
    )


# ------------------------------------------------------------- changes feed


@dataclass
class ChangeRow:
    """One real-world change, however many listing rows reported it.

    A price drop on an ad that autovit and olx both carry, matching two of your
    searches, is three `event` rows. They describe one thing that happened, so
    the feed shows one row: `event`/`listing` are the representative pair (see
    `pick_representative`) and `members` keeps the rest, which is what lets the
    row badge every site that saw it.
    """

    event: Event
    listing: Listing
    search: Search
    members: list = field(default_factory=list)
    # Arrived after you last opened Changes — marked on the page, once.
    unseen: bool = False

    @property
    def sites(self) -> list[str]:
        """Every site that reported this change, in display order."""
        seen = {listing.site for _, listing in self.members} or {self.listing.site}
        ordered = [s for s in SITE_PRIORITY if s in seen]
        return ordered + sorted(seen - set(ordered))

    @property
    def is_merged(self) -> bool:
        return len(self.members) > 1

    @property
    def delta(self) -> Optional[float]:
        if self.event.old_price is None or self.event.new_price is None:
            return None
        d = self.event.new_price - self.event.old_price
        return d if abs(d) >= 0.01 else None

    @property
    def pct(self) -> Optional[float]:
        d = self.delta
        if d is None or not self.event.old_price:
            return None
        return d / self.event.old_price * 100


# A collection runs its sources one after another — three sites, a few minutes
# all told, with mobile.de's gentle pacing the slow part. Runs closer together
# than this belong to the same collection; anything further apart is a separate
# one. Generous, because there is no collection id in the database to key on and
# guessing too small would split one collection into several headings.
COLLECTION_GAP = timedelta(minutes=30)


@dataclass
class FeedGroup:
    """One collection's worth of changes, as one collapsible block.

    The feed was a flat list of every change ever recorded, which reads fine on
    the day and turns into a wall after a week. Grouping by collection restores
    the thing you actually want to know — "what did this morning's run turn up"
    — and lets the rest fold away.
    """

    started_at: datetime
    rows: list = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        by_type: dict[str, int] = {}
        for row in self.rows:
            by_type[row.event.type.value] = by_type.get(row.event.type.value, 0) + 1
        return by_type

    @property
    def sites(self) -> list[str]:
        """Every site that reported something in this collection, in display order."""
        seen = {site for row in self.rows for site in row.sites}
        ordered = [s for s in SITE_PRIORITY if s in seen]
        return ordered + sorted(seen - set(ordered))

    @property
    def unseen_count(self) -> int:
        return sum(1 for row in self.rows if row.unseen)


def _collection_of_run(session: Session) -> dict[int, datetime]:
    """run_id -> when the collection it belonged to started.

    There is no collection id in the schema: `collect()` writes one `Run` per
    search and site and nothing ties them together. Clustering on start time
    reconstructs it, which is enough for a heading and costs one query.
    """
    runs = session.exec(select(Run.id, Run.started_at).order_by(Run.started_at, Run.id)).all()

    batches: dict[int, datetime] = {}
    anchor: Optional[datetime] = None
    previous: Optional[datetime] = None
    for run_id, started in runs:
        if anchor is None or started - previous > COLLECTION_GAP:
            anchor = started
        batches[run_id] = anchor
        previous = started
    return batches


def _group_by_collection(rows: list, collection_of: dict[int, datetime]) -> list[FeedGroup]:
    """Fold feed rows into collections, keeping the feed's newest-first order.

    A merged row can span several runs — the same drop reported by autovit and
    olx — but they are runs of the same collection, so the representative's is
    the right one to key on.
    """
    groups: list[FeedGroup] = []
    for row in rows:
        started = collection_of.get(row.event.run_id) or row.event.created_at
        if not groups or groups[-1].started_at != started:
            groups.append(FeedGroup(started_at=started))
        groups[-1].rows.append(row)
    return groups


def _baseline_run_ids(session: Session) -> set[int]:
    """Each search's first successful run — the one that established where it started.

    Its arrivals are not news: when you begin watching a search, every listing on
    it is "new" by definition. On this database that was 215 of 216 rows on the
    Changes page, burying the one thing that had actually happened.

    Anchored on the first run that **completed successfully**, and deliberately
    not on the first run that produced events. The difference matters for a
    search that collects cleanly but finds nothing: the strict "2025 4x4 Tekna"
    autovit search ran 19 times, every one `ok`, every one 0 results, because
    autovit relaxes it. Keyed on events, it would have had no baseline until the
    day real matches finally appeared - and would then have hidden them as
    "already there". They are the opposite of that: we had been watching
    successfully for days and genuinely finding none, so their arrival is the
    most newsworthy thing that search can do.

    A blocked or errored first run is still skipped, since it establishes
    nothing about what was there.

    Per search rather than globally, so a source added later gets its own
    baseline instead of its whole inventory arriving as breaking news.
    """
    rows = session.exec(
        select(Run.id, Run.search_id)
        .where(Run.status == RunStatus.OK)
        .order_by(Run.search_id, Run.started_at, Run.id)
    ).all()

    first: dict[int, int] = {}
    for run_id, search_id in rows:
        first.setdefault(search_id, run_id)
    return set(first.values())


def _is_baseline(group, baseline_run_ids: set[int]) -> bool:
    """True when every event behind a feed row came from a first run.

    All of them, not any: if one site reported a car as part of its baseline
    while another saw it arrive for real later, the arrival is the news.
    """
    return all(event.run_id in baseline_run_ids for event, _ in group)


# ------------------------------------------------ what you have not seen yet

# When you last opened Changes, in naive UTC like every timestamp in the
# database. A run in the background — scheduled, or started from another tab —
# changes things with nobody watching; this is what the Changes tab's number
# is measured from.
CHANGES_SEEN = "changes_seen_at"


def _changes_seen_at(session: Session) -> Optional[datetime]:
    row = session.get(AppState, CHANGES_SEEN)
    try:
        return datetime.fromisoformat(row.value) if row and row.value else None
    except ValueError:
        return None


def _mark_changes_seen(session: Session, at: datetime) -> None:
    row = session.get(AppState, CHANGES_SEEN) or AppState(name=CHANGES_SEEN)
    row.value = at.isoformat()
    session.add(row)
    session.commit()


def _unseen_changes(session: Session, since: datetime) -> int:
    """How many feed rows arrived after `since`, counted the way the feed counts.

    Rows, not event records: one drop that autovit and olx both reported is one
    row on the page, and the number must not promise two. First-run arrivals
    are left out, as the feed leaves them out.
    """
    pairs = list(
        session.exec(
            select(Event, Listing).where(
                Event.listing_id == Listing.id, Event.created_at > since
            )
        ).all()
    )
    if not pairs:
        return 0
    baseline = _baseline_run_ids(session)
    return sum(1 for g in group_events(pairs) if not _is_baseline(g, baseline))


@router.get("/changes/unseen", response_class=HTMLResponse)
def unseen_changes(request: Request) -> HTMLResponse:
    """The number on the Changes tab: what arrived since you last opened it.

    Asked for on every page and again every minute, so a run that finishes
    while the dashboard is open shows up without a reload.
    """
    with _session(request) as session:
        since = _changes_seen_at(session)
        if since is None:
            # The first time ever. Everything before now would be "unseen" only
            # because nothing was recorded, so start counting from here.
            _mark_changes_seen(session, utcnow())
            count = 0
        else:
            count = _unseen_changes(session, since)
    return _render(request, "partials/changes_badge.html", count=count)


@router.get("/changes", response_class=HTMLResponse)
def changes_feed(
    request: Request,
    type: str = Query("all"),
    config: str = Query(""),
    disabled: OptInt = None,
    limit: OptInt = None,
    baseline: OptInt = None,
) -> HTMLResponse:
    """Reverse-chronological feed of what changed — §9's "most-used screen".

    The first run for each search is excluded by default: those arrivals are the
    starting position rather than a change. `?baseline=1` folds them back in.

    Scoped by the sidebar, on the same `?config=` the Listings page uses, so
    picking a configuration on one page and moving to the other keeps it. The
    old `?search_id=` picker scoped to one *source* of a configuration; this
    scopes to the configuration, which is what the two dropdowns disagreed
    about (§8).
    """
    limit = _clamp(limit, default=200, maximum=1000)
    show_baseline = bool(baseline)

    wanted: Optional[list[EventType]] = None
    if type == "drops":
        wanted = [EventType.PRICE_DOWN]
    elif type == "prices":
        wanted = [EventType.PRICE_DOWN, EventType.PRICE_UP]
    elif type == "new":
        wanted = [EventType.NEW]
    elif type == "delisted":
        wanted = [EventType.DELISTED]

    with _session(request) as session:
        sidebar = _sidebar(session, config, bool(disabled))
        scope = sidebar.selected.search_ids if sidebar.selected else None

        # Opening Changes is looking at it, so the tab's number clears. Marked
        # up to the newest change that exists before the feed is read — not
        # "now" — so one a run writes while this page renders stays unseen
        # rather than being cleared unshown.
        seen_before = _changes_seen_at(session)
        newest = session.exec(select(func.max(Event.created_at))).one()
        if newest is not None and (seen_before is None or newest > seen_before):
            _mark_changes_seen(session, newest)
        elif seen_before is None:
            _mark_changes_seen(session, utcnow())

        query = select(Event, Listing, Search).where(
            Event.listing_id == Listing.id, Listing.search_id == Search.id
        )
        if wanted:
            query = query.where(Event.type.in_(wanted))
        if scope is not None:
            query = query.where(Search.id.in_(scope))
        # Fetch before limiting: the limit is a limit on *rows shown*, and
        # grouping happens after, so slicing the events first would leave the
        # last row missing the sites it was reported by.
        query = query.order_by(Event.created_at.desc(), Event.id.desc())

        fetched = list(session.exec(query).all())
        searches_by_id = {
            row.id: row
            for row in session.exec(select(Search)).all()
        }

        # Counts for the filter chips, over the same search scope but ignoring
        # the type filter — so each chip shows how many rows it would show.
        counts_q = select(Event, Listing).where(
            Event.listing_id == Listing.id, Listing.search_id == Search.id
        )
        if scope is not None:
            counts_q = counts_q.where(Search.id.in_(scope))
        countable = list(session.exec(counts_q).all())

        baseline_run_ids = _baseline_run_ids(session)
        collection_of = _collection_of_run(session)

    groups = group_events([(e, l) for e, l, _ in fetched])
    if not show_baseline:
        groups = [g for g in groups if not _is_baseline(g, baseline_run_ids)]
    groups = groups[:limit]

    rows = []
    for group in groups:
        event, listing = pick_representative(group)
        rows.append(
            ChangeRow(
                event=event,
                listing=listing,
                search=searches_by_id.get(listing.search_id),
                members=group,
                unseen=seen_before is not None
                and any(e.created_at > seen_before for e, _ in group),
            )
        )

    # Chip counts are counts of feed rows, not of event records, or they would
    # promise more than the feed shows: 87 events, 42 rows. They also honour the
    # baseline setting, or every chip would over-promise by the whole first run.
    countable_groups = group_events(list(countable))
    baseline_count = sum(
        1 for g in countable_groups if _is_baseline(g, baseline_run_ids)
    )
    if not show_baseline:
        countable_groups = [
            g for g in countable_groups if not _is_baseline(g, baseline_run_ids)
        ]
    grouped_types = [pick_representative(group)[0].type for group in countable_groups]
    counts = {
        "all": len(grouped_types),
        "drops": sum(1 for t in grouped_types if t is EventType.PRICE_DOWN),
        "prices": sum(
            1 for t in grouped_types if t in (EventType.PRICE_DOWN, EventType.PRICE_UP)
        ),
        "new": sum(1 for t in grouped_types if t is EventType.NEW),
        "delisted": sum(1 for t in grouped_types if t is EventType.DELISTED),
    }

    return _render(
        request,
        "changes.html",
        title="Changes",
        groups=_group_by_collection(rows, collection_of),
        rows=rows,
        counts=counts,
        type=type,
        sidebar=sidebar,
        show_baseline=show_baseline,
        baseline_count=baseline_count,
    )


# --------------------------------------------------------- price history API


@router.get("/listing/{listing_id}/history", response_class=HTMLResponse)
def listing_history(request: Request, listing_id: int) -> HTMLResponse:
    """htmx fragment: the price-history chart for one listing."""
    with _session(request) as session:
        listing = session.get(Listing, listing_id)
        if listing is None:
            raise HTTPException(status_code=404, detail="No such listing")
        points = session.exec(
            select(PriceHistory)
            .where(PriceHistory.listing_id == listing_id)
            .order_by(PriceHistory.observed_at)
        ).all()

    series = [
        {"t": p.observed_at.strftime("%Y-%m-%d %H:%M"), "p": p.price} for p in points
    ]
    prices = [p.price for p in points]

    return _render(
        request,
        "partials/history.html",
        listing=listing,
        series=series,
        first_price=prices[0] if prices else None,
        low=min(prices) if prices else None,
        high=max(prices) if prices else None,
    )


# ------------------------------------------------------------- "Run now"


def _runner(request: Request):
    return request.app.state.jobs


def _run_label(names: list[str]) -> str:
    """What the status strip calls this run.

    Two names read fine; a list of five does not, so past that it counts them.
    """
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return " + ".join(names)
    return f"{len(names)} configurations"


@router.post("/run", response_class=HTMLResponse)
def run_now(
    request: Request,
    search_id: OptFormInt = None,
    config: list[str] = Form(default=[]),
) -> HTMLResponse:
    """Start a collection. Returns the status fragment htmx swaps in.

    Three scopes: everything (no `config`), the configurations named by
    `config` — one from its own page, or several from the run picker — or one
    source of one (`search_id`, from a row in a health table).

    `config` is a list because the picker posts a checkbox per configuration.
    A single value still arrives as a one-item list, so the configuration
    page's own button needs no special case.
    """
    runner = _runner(request)
    wanted = [c for c in config if c.strip()]

    if wanted:
        with _session(request) as session:
            index = configurations.load_configurations(session)

        chosen = []
        for key in wanted:
            found = configurations.find(index.configurations, key)
            if found is None:
                raise HTTPException(
                    status_code=404, detail=f"No configuration {key!r}"
                )
            if found.name not in chosen:
                chosen.append(found.name)

        # Ticking every box is the same as ticking none: collect everything.
        # Passing the full list would work too, but "All configurations" is
        # what the status strip should say.
        if len(chosen) == len(index.configurations):
            runner.start(label="All configurations")
        else:
            runner.start(label=_run_label(chosen), search_names=chosen)
    elif search_id is None:
        runner.start(label="All configurations")
    else:
        with _session(request) as session:
            search = session.get(Search, search_id)
            if search is None:
                raise HTTPException(status_code=404, detail="No such search")
            name, site = search.name, search.site
        runner.start(label=f"{name} / {site}", search_names=[name], sites=[site])

    return _render(request, "partials/run_status.html", job=runner.latest())


@router.get("/run/picker", response_class=HTMLResponse)
def run_picker(request: Request) -> HTMLResponse:
    """The checkbox list behind the caret next to "Run all now".

    Fetched when the menu is first opened rather than rendered into every page,
    so the topbar — which lives in base.html and has no database context — does
    not have to grow one.

    Only enabled configurations are offered: `collect()` skips the archived ones
    whatever it is asked for, so listing them would be offering something that
    silently does nothing.
    """
    with _session(request) as session:
        index = configurations.load_configurations(session)

    return _render(
        request,
        "partials/run_picker.html",
        options=[c for c in index.configurations if c.enabled],
    )


@router.get("/run/status", response_class=HTMLResponse)
def run_status(request: Request) -> HTMLResponse:
    """htmx polls this while a job runs; it stops polling when finished."""
    return _render(request, "partials/run_status.html", job=_runner(request).latest())
