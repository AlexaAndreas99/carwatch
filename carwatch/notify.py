"""Email notifications after a collection (spec §15).

Off by default. When switched on from the Runs page, every collection — the
schedule's, "Run now", the CLI — ends by looking at what it changed and, if any
of it is something you asked to hear about, sends one email summing it up.

Which cars and which changes are yours to pick: only favorites or every car,
any mix of price drops, rises, new listings and cars gone, a minimum price
change, and configurations that stay quiet. The settings live in the database
(`app_state`), because the scheduled run has no dashboard to ask; the email
password lives in the system's credential store, never in a file.

Nothing here may break a collection. A notification that cannot be sent is
logged and recorded for the Runs page, and the collection's own result stands.
"""

from __future__ import annotations

import json
import logging
import smtplib
import ssl
from dataclasses import asdict, dataclass, field
from datetime import datetime
from email.message import EmailMessage
from html import escape
from pathlib import Path
from typing import Optional

from sqlmodel import Session, select

from carwatch.db import baseline_run_ids, session_scope
from carwatch.models import AppState, Event, EventType, Favorite, Listing, Run, Search, utcnow

log = logging.getLogger(__name__)

# Hidden for now (2026-10-06): no panel on the Runs page, no /notifications
# routes, and collections send nothing even if settings were saved earlier.
# Set to True to bring the feature back as it was built.
NOTIFICATIONS_VISIBLE = False

SETTINGS_KEY = "notifications"
LAST_KEY = "notifications_last"
# The credential store entry: one service, keyed by the SMTP login.
KEYRING_SERVICE = "CarWatch email"

EVENT_TYPES = (EventType.PRICE_DOWN, EventType.PRICE_UP, EventType.NEW, EventType.DELISTED)
EVENT_LABELS = {
    EventType.PRICE_DOWN: ("price drop", "price drops"),
    EventType.PRICE_UP: ("price rise", "price rises"),
    EventType.NEW: ("new listing", "new listings"),
    EventType.DELISTED: ("car gone", "cars gone"),
}

SMTP_TIMEOUT = 30


# ------------------------------------------------------------------ settings


@dataclass
class NotifySettings:
    enabled: bool = False
    # "favorites": only starred cars. "all": every car in a configuration
    # that is not muted.
    scope: str = "all"
    events: list[str] = field(default_factory=lambda: [EventType.PRICE_DOWN.value])
    # A price change smaller than this, in the ad's own currency, is not news.
    min_change: float = 500
    # Configuration names that never send anything.
    muted: list[str] = field(default_factory=list)

    smtp_host: str = ""
    smtp_port: int = 587
    security: str = "starttls"  # "starttls" | "ssl" | "none"
    username: str = ""
    sender: str = ""
    recipient: str = ""

    @property
    def is_configured(self) -> bool:
        return bool(self.smtp_host and self.recipient and (self.sender or self.username))

    def wants(self, event_type: EventType) -> bool:
        return event_type.value in self.events


def load_settings(session: Session) -> NotifySettings:
    row = session.get(AppState, SETTINGS_KEY)
    if row is None or not row.value:
        return NotifySettings()
    try:
        data = json.loads(row.value)
    except ValueError:
        return NotifySettings()
    known = NotifySettings.__dataclass_fields__
    return NotifySettings(**{k: v for k, v in data.items() if k in known})


def save_settings(session: Session, settings: NotifySettings) -> None:
    row = session.get(AppState, SETTINGS_KEY) or AppState(name=SETTINGS_KEY)
    row.value = json.dumps(asdict(settings))
    session.add(row)
    session.commit()


def get_password(username: str) -> Optional[str]:
    if not username:
        return None
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, username)
    except Exception:  # no backend, locked store: send without, and say so if it fails
        log.exception("could not read the email password from the credential store")
        return None


def set_password(username: str, password: str) -> None:
    import keyring

    keyring.set_password(KEYRING_SERVICE, username, password)


# ------------------------------------------------------------- last result


@dataclass
class LastResult:
    at: datetime
    ok: bool
    changes: int = 0
    error: str = ""
    test: bool = False


def load_last(session: Session) -> Optional[LastResult]:
    row = session.get(AppState, LAST_KEY)
    if row is None or not row.value:
        return None
    try:
        data = json.loads(row.value)
        data["at"] = datetime.fromisoformat(data["at"])
        return LastResult(**data)
    except (ValueError, KeyError, TypeError):
        return None


def record_last(session: Session, result: LastResult) -> None:
    data = asdict(result)
    data["at"] = result.at.isoformat()
    row = session.get(AppState, LAST_KEY) or AppState(name=LAST_KEY)
    row.value = json.dumps(data)
    session.add(row)
    session.commit()


# ------------------------------------------------------- what to report


@dataclass
class Change:
    """One real-world change, however many sites reported it."""

    type: EventType
    title: str
    url: str
    configuration: str
    sites: list[str]
    favorite: bool
    old_price: Optional[float]
    new_price: Optional[float]
    currency: str
    year: Optional[int]
    mileage_km: Optional[int]
    location: Optional[str]

    @property
    def delta(self) -> Optional[float]:
        if self.old_price is None or self.new_price is None:
            return None
        return self.new_price - self.old_price


def select_changes(
    session: Session, run_ids: set[int], settings: NotifySettings
) -> list[Change]:
    """The changes these runs made that `settings` asks to hear about.

    Grouped per car as the Changes page does, so an ad carried by autovit and
    olx is one line. A search's first run is left out: when you start watching,
    every car on it is "new", and an email listing a hundred of them is noise.
    """
    from carwatch.web.merge import SITE_PRIORITY, group_events, pick_representative

    if not run_ids:
        return []
    first_runs = baseline_run_ids(session)
    pairs = session.exec(
        select(Event, Listing).where(Event.listing_id == Listing.id, Event.run_id.in_(run_ids))
    ).all()
    pairs = [(e, l) for e, l in pairs if e.run_id not in first_runs]
    if not pairs:
        return []

    names = {s.id: s.name for s in session.exec(select(Search)).all()}
    starred = {(f.site, f.site_listing_id) for f in session.exec(select(Favorite)).all()}

    out: list[Change] = []
    for group in group_events(pairs):
        event, listing = pick_representative(group)
        if not settings.wants(event.type):
            continue
        configs = {names.get(l.search_id, "") for _, l in group}
        if configs <= set(settings.muted):
            continue
        favorite = any((l.site, l.site_listing_id) in starred for _, l in group)
        if settings.scope == "favorites" and not favorite:
            continue
        if event.type in (EventType.PRICE_DOWN, EventType.PRICE_UP):
            if event.old_price is None or event.new_price is None:
                continue
            if abs(event.new_price - event.old_price) < settings.min_change:
                continue
        seen = {l.site for _, l in group}
        out.append(
            Change(
                type=event.type,
                title=listing.title,
                url=listing.url,
                configuration=names.get(listing.search_id, ""),
                sites=[s for s in SITE_PRIORITY if s in seen] + sorted(seen - set(SITE_PRIORITY)),
                favorite=favorite,
                old_price=event.old_price,
                new_price=event.new_price if event.new_price is not None else listing.price,
                currency=listing.currency or "EUR",
                year=listing.year,
                mileage_km=listing.mileage_km,
                location=listing.location,
            )
        )

    order = {t: i for i, t in enumerate(EVENT_TYPES)}
    out.sort(key=lambda c: (not c.favorite, order[c.type], c.configuration, c.title))
    return out


# ------------------------------------------------------------ the email


def _money(value: Optional[float], currency: str = "") -> str:
    if value is None:
        return "—"
    text = f"{value:,.0f}".replace(",", " ")
    return f"{text} {currency}".strip()


def _count(n: int, event_type: EventType) -> str:
    one, many = EVENT_LABELS[event_type]
    return f"{n} {one if n == 1 else many}"


def subject_for(changes: list[Change]) -> str:
    counts = [
        _count(sum(1 for c in changes if c.type is t), t)
        for t in EVENT_TYPES
        if any(c.type is t for c in changes)
    ]
    favorites = sum(1 for c in changes if c.favorite)
    tail = f" ({favorites} favorite{'' if favorites == 1 else 's'})" if favorites else ""
    return "CarWatch: " + ", ".join(counts) + tail


def _what(c: Change) -> str:
    if c.type in (EventType.PRICE_DOWN, EventType.PRICE_UP):
        pct = f" ({c.delta / c.old_price * 100:+.1f}%)" if c.old_price else ""
        sign = "−" if c.delta < 0 else "+"
        return (
            f"{sign}{_money(abs(c.delta))}{pct}: "
            f"{_money(c.old_price)} → {_money(c.new_price, c.currency)}"
        )
    if c.type is EventType.NEW:
        return f"new at {_money(c.new_price, c.currency)}"
    return f"gone, last asking {_money(c.old_price if c.old_price is not None else c.new_price, c.currency)}"


def _facts(c: Change) -> str:
    parts = [str(c.year) if c.year else None,
             f"{c.mileage_km:,} km".replace(",", " ") if c.mileage_km is not None else None,
             c.location, " + ".join(s.upper() for s in c.sites)]
    return " · ".join(p for p in parts if p)


def _by_configuration(changes: list[Change]) -> dict[str, list[Change]]:
    out: dict[str, list[Change]] = {}
    for c in changes:
        out.setdefault(c.configuration, []).append(c)
    return out


def build_message(
    changes: list[Change], settings: NotifySettings, dashboard_url: str = "http://localhost:8009/changes"
) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject_for(changes)
    msg["From"] = settings.sender or settings.username
    msg["To"] = settings.recipient

    lines = []
    for name, group in _by_configuration(changes).items():
        lines.append(name)
        for c in group:
            star = "★ " if c.favorite else ""
            lines.append(f"  {star}{c.title}")
            lines.append(f"    {_what(c)}")
            lines.append(f"    {_facts(c)}")
            lines.append(f"    {c.url}")
        lines.append("")
    lines.append(f"All changes: {dashboard_url} (while the dashboard is running)")
    msg.set_content("\n".join(lines))

    colors = {
        EventType.PRICE_DOWN: "#15803d", EventType.PRICE_UP: "#b45309",
        EventType.NEW: "#2563eb", EventType.DELISTED: "#71717a",
    }
    html = ['<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#1a1a1c">']
    for name, group in _by_configuration(changes).items():
        html.append(f'<h3 style="margin:18px 0 6px;font-size:15px">{escape(name)}</h3>')
        for c in group:
            star = "★ " if c.favorite else ""
            html.append(
                f'<div style="margin:0 0 10px;padding-left:10px;border-left:3px solid {colors[c.type]}">'
                f'<a href="{escape(c.url)}" style="color:#1a1a1c;font-weight:600;text-decoration:none">'
                f"{star}{escape(c.title)}</a><br>"
                f'<span style="color:{colors[c.type]};font-weight:600">{escape(_what(c))}</span><br>'
                f'<span style="color:#6b6b73;font-size:12px">{escape(_facts(c))}</span></div>'
            )
    html.append(
        f'<p style="color:#6b6b73;font-size:12px;margin-top:16px">'
        f'<a href="{escape(dashboard_url)}">All changes in CarWatch</a> (while the dashboard is running)</p></div>'
    )
    msg.add_alternative("".join(html), subtype="html")
    return msg


def send(settings: NotifySettings, message: EmailMessage, password: Optional[str]) -> None:
    """Hand the message to the SMTP server. Raises on any failure."""
    host, port = settings.smtp_host, int(settings.smtp_port)
    context = ssl.create_default_context()
    if settings.security == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT)
    with server:
        if settings.security == "starttls":
            server.starttls(context=context)
        if settings.username:
            if not password:
                raise RuntimeError("no password saved for " + settings.username)
            server.login(settings.username, password)
        server.send_message(message)


def describe_error(exc: Exception) -> str:
    """A line for the Runs page — the SMTP reply when there is one."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "the email server refused the login — check the username and app password"
    if isinstance(exc, smtplib.SMTPResponseException):
        return f"the email server said: {exc.smtp_code} {exc.smtp_error.decode(errors='replace')[:200]}"
    if isinstance(exc, (OSError, smtplib.SMTPException)):
        return f"could not reach the email server: {exc}"[:240]
    return str(exc)[:240] or exc.__class__.__name__


# ------------------------------------------------------ after a collection


def notify_after_collection(db_file: str | Path, started_at: datetime) -> Optional[LastResult]:
    """Email what the collection since `started_at` changed, if asked to.

    Returns what happened, or None when nothing was due. Never raises.
    """
    if not NOTIFICATIONS_VISIBLE:
        return None
    try:
        with session_scope(db_file) as session:
            settings = load_settings(session)
            if not settings.enabled or not settings.is_configured:
                return None
            run_ids = set(
                session.exec(select(Run.id).where(Run.started_at >= started_at)).all()
            )
            changes = select_changes(session, run_ids, settings)
        if not changes:
            return None

        message = build_message(changes, settings)
        try:
            send(settings, message, get_password(settings.username))
            result = LastResult(at=utcnow(), ok=True, changes=len(changes))
        except Exception as exc:
            log.warning("notification email not sent: %s", exc)
            result = LastResult(at=utcnow(), ok=False, changes=len(changes), error=describe_error(exc))

        with session_scope(db_file) as session:
            record_last(session, result)
        return result
    except Exception:
        log.exception("notifications failed after a collection")
        return None
