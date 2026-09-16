"""Read a pasted search URL back into a description.

Creating a configuration is links only: you run each search on the site
yourself and paste what the address bar says, because a URL you have watched
return the right cars is the only kind worth trusting. This module reads such
a link back into the form's description fields — make, model, years — so that
pasting one fills in what the link already says instead of making you type it
again.

It builds nothing. It used to: until 2026-09-10 CarWatch generated URLs from a
description and checked each one against the cars it returned. That was
retired because a wrong generated URL does not fail. Measured against autovit,
`nissan/not-a-real-model/2020` returned 820 assorted Nissans with the site's
own relaxation flag unset, and olx answered a Mercedes A-Klasse search with a
laptop. A check on the results can catch a wrong model; it cannot catch a year,
trim or drivetrain the URL quietly failed to carry.

Everything read here is display metadata. What it must never do is invent a
value that was not in the URL.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import parse_qsl, urlparse

from carwatch.config import KNOWN_SITES

# autovit's drivetrain values, as the form's free-text field would say them.
# Anything else is left blank rather than guessed at.
AUTOVIT_DRIVETRAIN = {
    "all-wheel-auto": "4x4",
    "front-wheel": "front",
    "rear-wheel": "rear",
}


def _int(value: Any) -> Optional[int]:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


@dataclass
class Criteria:
    """What a configuration is looking for, in names rather than site dialects.

    The same fields the configuration form has, so that reading a link back
    fills the form with nothing left over.
    """

    make: Optional[str] = None
    model: Optional[str] = None
    trim: Optional[str] = None
    drivetrain: Optional[str] = None
    year_min: Optional[int] = None
    year_max: Optional[int] = None
    price_min: Optional[int] = None
    price_max: Optional[int] = None
    currency: Optional[str] = None

    def as_metadata(self) -> dict:
        """The subset the configuration form and config.yaml store."""
        out = {
            "make": self.make,
            "model": self.model,
            "trim": self.trim,
            "drivetrain": self.drivetrain,
            "year_min": self.year_min,
            "year_max": self.year_max,
            "price_min": self.price_min,
            "price_max": self.price_max,
            "currency": self.currency,
        }
        return {k: v for k, v in out.items() if v is not None and v != ""}

    def suggested_name(self) -> str:
        """A name a person would have typed, for pre-filling the form."""
        parts = [self.make, self.model]
        if self.year_min and self.year_max and self.year_min == self.year_max:
            parts.append(str(self.year_min))
        elif self.year_min:
            parts.append(f"{self.year_min}+")
        if self.drivetrain:
            parts.append(self.drivetrain)
        if self.trim:
            parts.append(self.trim)
        return " ".join(p for p in parts if p).strip()


# ------------------------------------------------------------------ parsing


def _parse_autovit(parsed) -> Criteria:
    c = Criteria()
    # /autoturisme/<make>/<model>/de-la-<year>/q-<free text>
    segments = [s for s in parsed.path.split("/") if s]
    if segments and segments[0] == "autoturisme":
        segments = segments[1:]
    rest = []
    for segment in segments:
        if segment.startswith("de-la-"):
            c.year_min = _int(segment[len("de-la-") :])
        elif segment.startswith("pana-la-"):
            c.year_max = _int(segment[len("pana-la-") :])
        elif segment.startswith("q-"):
            c.trim = segment[2:].replace("-", " ")
        else:
            rest.append(segment)
    if rest:
        c.make = rest[0].replace("-", " ").title()
    if len(rest) > 1:
        c.model = rest[1].replace("-", " ").title()

    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    c.drivetrain = AUTOVIT_DRIVETRAIN.get(query.get("search[filter_enum_transmission]", ""))
    return c


def _parse_olx(parsed) -> Criteria:
    c = Criteria()
    segments = [s for s in parsed.path.split("/") if s]
    # /auto-masini-moto-ambarcatiuni/autoturisme/<make>/
    if "autoturisme" in segments:
        after = segments[segments.index("autoturisme") + 1 :]
        if after:
            c.make = after[0].replace("-", " ").title()

    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    model = query.get("search[filter_enum_model][0]")
    if model:
        c.model = model.replace("-", " ").title()
    c.year_min = _int(query.get("search[filter_float_year:from]"))
    c.year_max = _int(query.get("search[filter_float_year:to]"))
    c.price_min = _int(query.get("search[filter_float_price:from]"))
    c.price_max = _int(query.get("search[filter_float_price:to]"))
    if query.get("currency"):
        c.currency = query["currency"].upper()
    return c


def _parse_mobilede(parsed) -> Criteria:
    """Years, price cap and drivetrain. Never make or model.

    mobile.de names those with opaque numeric ids (`ms=18700;47` is Nissan
    Qashqai), and a name read off a number would be a guess.
    """
    c = Criteria()
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))

    c.year_min = _int(query.get("fr"))
    c.year_max = _int(query.get("to"))
    c.price_max = _int(query.get("pr:to") or query.get("price:to"))
    if query.get("dt") == "ALL_WHEEL":
        c.drivetrain = "4x4"
    return c


_PARSERS = {"autovit": _parse_autovit, "olx": _parse_olx, "mobilede": _parse_mobilede}


def parse_url(site: str, url: str) -> Criteria:
    """Read a pasted search URL back into criteria.

    Best-effort by design: a URL carries whatever filters the user happened to
    set, and everything here is display metadata anyway.
    """
    site = (site or "").strip().lower()
    parser = _PARSERS.get(site)
    if parser is None or not url:
        return Criteria()
    return parser(urlparse(url.strip()))


def site_of(url: str) -> Optional[str]:
    """Which known site a pasted URL belongs to, or None."""
    lowered = (url or "").strip().lower()
    for site, fragments in KNOWN_SITES.items():
        if any(fragment in lowered for fragment in fragments):
            return site
    return None
