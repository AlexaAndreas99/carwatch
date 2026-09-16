"""Filling in `listing.image_url` for rows collected before photos existed.

autovit's adapter has always stored the whole GraphQL node in `raw_json`, and
that node carries `thumbnail.x1` / `.x2` — so every autovit listing already in
the database has its photo on hand and needs no re-fetch. olx and mobile.de
store only text in `raw_json`, so their rows genuinely have to be re-collected;
this reports how many are in that position rather than pretending otherwise.

Nothing here touches price history, events or runs: it only ever fills a column
that was NULL.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from sqlmodel import Session, select

from carwatch.models import Listing


@dataclass
class BackfillResult:
    """What one backfill pass did, per site."""

    filled: dict[str, int]
    unavailable: dict[str, int]

    @property
    def total_filled(self) -> int:
        return sum(self.filled.values())

    @property
    def total_unavailable(self) -> int:
        return sum(self.unavailable.values())


def image_from_raw(raw_json: Optional[str]) -> Optional[str]:
    """Dig a usable photo URL out of a stored `raw_json` blob, or None.

    Only autovit's blob has one. Written against the shape rather than the site
    name so a future adapter that stores a richer blob is picked up for free.
    """
    if not raw_json:
        return None
    try:
        raw = json.loads(raw_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None

    thumbnail = raw.get("thumbnail")
    if not isinstance(thumbnail, dict):
        return None

    # x2 (640x480) over x1 (320x240) — same reasoning as the adapter: cards are
    # ~268px wide and displays are commonly 2x.
    for key in ("x2", "x1"):
        url = (thumbnail.get(key) or "").strip()
        if url:
            return url
    return None


def backfill_image_urls(session: Session) -> BackfillResult:
    """Fill `image_url` on every listing that has none but could have one."""
    rows = session.exec(select(Listing).where(Listing.image_url == None)).all()  # noqa: E711

    filled: dict[str, int] = {}
    unavailable: dict[str, int] = {}

    for listing in rows:
        url = image_from_raw(listing.raw_json)
        if url:
            listing.image_url = url
            session.add(listing)
            filled[listing.site] = filled.get(listing.site, 0) + 1
        else:
            unavailable[listing.site] = unavailable.get(listing.site, 0) + 1

    return BackfillResult(filled=filled, unavailable=unavailable)
