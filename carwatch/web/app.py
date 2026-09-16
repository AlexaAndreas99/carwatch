"""FastAPI application factory for the CarWatch dashboard (spec §9).

The dashboard is read-only in phase 4: it reads the SQLite database the
collector writes. It shares the DB and models with the collector and nothing
else — no scraping code is imported here.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from carwatch.config import Config, load_config
from carwatch.db import init_db, session_scope, sync_searches
from carwatch.models import utcnow

WEB_DIR = Path(__file__).parent
TEMPLATE_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


def _thousands(value: float) -> str:
    """1234567 -> '1 234 567'.

    A plain space, not a narrow no-break space: `.num` cells already set
    `white-space: nowrap`, so an exotic separator buys nothing and makes the
    output awkward to search, copy and assert on.
    """
    return f"{value:,.0f}".replace(",", " ")


def _format_price(value: float | None, currency: str | None = None) -> str:
    if value is None:
        return "—"
    text = _thousands(value)
    return f"{text} {currency}".strip() if currency else text


def _format_km(value: int | None) -> str:
    if value is None:
        return "—"
    return _thousands(value) + " km"


def _format_delta(value: float | None) -> str:
    if value is None:
        return ""
    text = _thousands(abs(value))
    return f"+{text}" if value > 0 else f"−{text}"


# Sparkline geometry. Small enough to sit beside a price without becoming a
# chart in its own right — the full Chart.js view stays on the detail expansion.
SPARK_WIDTH = 56
SPARK_HEIGHT = 18
SPARK_PAD = 2  # room for the stroke, so the top and bottom points aren't clipped


def _sparkline(prices, width: int = SPARK_WIDTH, height: int = SPARK_HEIGHT) -> str:
    """SVG polyline points for a price trace, or "" when there isn't one.

    Two observations is the minimum: one point is a dot, not a trace, and
    drawing it would suggest a history we don't have (front-end plan §7).

    The scale is per-listing, not global — the question a card answers is "which
    way has *this* price moved", and a shared scale would flatten every trace
    against the most volatile ad on the page.
    """
    points = [p for p in (prices or []) if p is not None]
    if len(points) < 2:
        return ""

    low, high = min(points), max(points)
    span = high - low
    step = width / (len(points) - 1)
    usable = height - 2 * SPARK_PAD

    out = []
    for index, price in enumerate(points):
        x = index * step
        # A price that never moved draws flat down the middle rather than
        # dividing by zero — and reads correctly, because it *is* flat.
        y = height / 2 if span == 0 else SPARK_PAD + usable * (1 - (price - low) / span)
        out.append(f"{x:.1f},{y:.1f}")
    return " ".join(out)


def _static_url(name: str) -> str:
    """`/static/<name>` with a cache-busting stamp from the file's mtime.

    Starlette's StaticFiles sends an ETag but no `Cache-Control`, so browsers
    fall back to *heuristic* freshness and will happily reuse a cached
    stylesheet without even revalidating it. The visible symptom is a CSS edit
    that appears to do nothing until you hard-reload - which wasted real time
    once. Changing the URL whenever the file changes sidesteps the whole
    question, and costs one stat() per render on a single-user local tool.
    """
    try:
        stamp = int((STATIC_DIR / name).stat().st_mtime)
    except OSError:
        return f"/static/{name}"
    return f"/static/{name}?v={stamp}"


def _facts(item) -> str:
    """Year, mileage, fuel and location as one line, skipping what we lack.

    Accepts a `Listing` or a `MergedListing` — the merged record proxies
    `canonical`, so both answer the same four questions.

    Missing values are omitted rather than rendered as a dash. A brand-new
    mobile.de car has no registration year and no odometer reading, and a line
    reading "— · — · Benzină · Berlin" looks like a rendering fault rather
    than a car we know four things less about.
    """
    row = getattr(item, "canonical", item)

    parts = []
    if row.year:
        parts.append(str(row.year))
    elif getattr(row, "condition", None) == "new":
        # An unregistered car has no first-registration year. Saying so beats a
        # gap the reader has to interpret.
        parts.append("New")
    if row.mileage_km is not None:
        parts.append(_format_km(row.mileage_km))
    if row.fuel:
        parts.append(row.fuel)
    if row.location:
        parts.append(row.location)
    return " · ".join(parts)


def _year_cell(item) -> str:
    """What a Year column should say for one listing.

    A brand-new car genuinely has no first-registration year, so the honest
    answer is not a number - but it is not an em dash either, which reads as
    "we failed to parse this".
    """
    row = getattr(item, "canonical", item)
    if row.year:
        return str(row.year)
    if getattr(row, "condition", None) == "new":
        return "New"
    return "—"


def _plain_number(value) -> str:
    """Render a number for a form field without a spurious ".0".

    Prices are floats because olx quotes cents ("24 649,52"), but a max-price
    box echoing back "30000.0" after every submit looks like a bug.
    """
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


SITE_LABELS = {"autovit": "AUTOVIT", "olx": "OLX", "mobilede": "MOBILE.DE"}


def _site_label(site: str) -> str:
    return SITE_LABELS.get(site, site.upper())


def _format_when(value) -> str:
    """Compact relative-ish timestamp for a local, single-user dashboard."""
    if value is None:
        return "never"

    # Same naive-UTC convention the models use, so the subtraction is valid.
    delta = utcnow() - value
    seconds = delta.total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    if seconds < 7 * 86400:
        return f"{int(seconds // 86400)}d ago"
    return value.strftime("%Y-%m-%d")


def _format_tracked(value) -> str:
    """How long CarWatch has been watching this car.

    The card said "seen 3d ago", which reads as *last* seen; it is the first
    sighting. How long a car has been sitting there is one of the few things
    the listing sites do not tell you, so it is worth saying plainly.
    """
    if value is None:
        return "not yet"
    hours = (utcnow() - value).total_seconds() / 3600
    if hours < 24:
        return "since today"
    days = int(hours // 24)
    return f"{days} day{'' if days == 1 else 's'}"


def create_app(config_path: str | Path = "config.yaml") -> FastAPI:
    config: Config = load_config(config_path)
    # Make sure the schema exists even if the collector has never run, so the
    # dashboard shows an empty state instead of an operational error.
    init_db(config.db_file)

    # Mirror config.yaml into the `search` table at startup, so a search you
    # just added shows up as "never run" instead of the dashboard claiming there
    # are no searches at all. Same sync the collector does.
    with session_scope(config.db_file) as session:
        sync_searches(session, config)

    app = FastAPI(title="CarWatch", docs_url=None, redoc_url=None)
    app.state.config = config

    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    templates.env.filters["price"] = _format_price
    templates.env.filters["km"] = _format_km
    templates.env.filters["delta"] = _format_delta
    templates.env.filters["when"] = _format_when
    templates.env.filters["tracked"] = _format_tracked
    templates.env.filters["sparkline"] = _sparkline
    templates.env.filters["site_label"] = _site_label
    templates.env.filters["plain"] = _plain_number
    templates.env.filters["facts"] = _facts
    templates.env.globals["static_url"] = _static_url
    templates.env.filters["year_cell"] = _year_cell
    templates.env.globals["SPARK_WIDTH"] = SPARK_WIDTH
    templates.env.globals["SPARK_HEIGHT"] = SPARK_HEIGHT
    app.state.templates = templates

    # "Run now" jobs share this one runner, so a second press while a
    # collection is in flight joins the running job instead of starting another.
    from carwatch.web.jobs import JobRunner

    app.state.jobs = JobRunner(config)

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from carwatch.web.routes import router

    app.include_router(router)
    return app
