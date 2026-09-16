"""Collapsing duplicate listings into one card, on read (front-end plan §5).

Three separate things produce more listing rows than there are cars:

  * **olx syndicates autovit ads.** For these searches, an olx result card links
    straight to `autovit.ro`, so the same physical car exists as an autovit row
    and an olx row.
  * **One car can match two searches.** Listings are keyed per search, so a car
    matching both configured searches is two rows on the same site.
  * Both at once, which is the common case here.

The key is the **autovit ad slug in the URL** — `…/nissan-qashqai-ID7HQf7M.html`
-> `7HQf7M`. It is present on autovit's own URLs and on the autovit URLs olx
links out to, so it identifies one car across both sites *and* across searches.
Nothing fuzzy is attempted: no title, price or mileage matching (§3, decision 3).
A listing with no slug keys on `(site, site_listing_id)` and simply appears on
its own — which is what happens to the handful of olx ads that are native to olx
and to every mobile.de listing.

**This module never writes.** The database keeps one row per site with its own
independent price history (§3, decision 5); merging in the database would
destroy per-site history irreversibly, while merging in the view is a
presentation choice we can change any time. `tests/test_merge.py` asserts the
database file is byte-identical before and after a merged page renders.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Mapping, Optional, Sequence

from carwatch.models import Listing, PriceHistory

# Romanian and German inventory are never the same car (§2), so they are grouped
# separately and shown on separate tabs. A site not listed here gets its own
# market named after itself rather than being silently folded into one of these.
MARKETS: dict[str, tuple[str, ...]] = {
    "ro": ("autovit", "olx"),
    "de": ("mobilede",),
}

MARKET_LABELS = {"ro": "Romania", "de": "Germany"}

# Which row speaks for a merged car. autovit first: it carries the photo, the
# structured specs (make/model/fuel/year as fields rather than free text) and
# the price we treat as canonical (§3, decision 4).
SITE_PRIORITY = ("autovit", "olx", "mobilede")

# "…/nissan-qashqai-ID7HQf7M.html" -> "7HQf7M". The id is autovit's own ad
# reference and is what olx points at when it syndicates the ad.
_AUTOVIT_AD_RE = re.compile(r"-ID([0-9A-Za-z]+)\.html", re.IGNORECASE)


def autovit_ad_slug(url: Optional[str]) -> Optional[str]:
    """The autovit ad id embedded in a listing URL, or None.

    Deliberately requires the URL to be on autovit.ro: another site could use a
    similar-looking slug for something else entirely, and a false match here
    would merge two unrelated cars into one card.
    """
    if not url:
        return None
    if "autovit.ro" not in url.lower():
        return None
    match = _AUTOVIT_AD_RE.search(url)
    return match.group(1) if match else None


def market_of(site: str) -> str:
    """Which market tab a site belongs on."""
    for market, sites in MARKETS.items():
        if site in sites:
            return market
    return site


def merge_key(listing: Listing) -> tuple[str, str]:
    """The identity two rows must share to be shown as one car."""
    slug = autovit_ad_slug(listing.url)
    if slug:
        return ("autovit-ad", slug)
    return (listing.site, listing.site_listing_id)


@dataclass
class MergedListing:
    """One car, however many listing rows it turned out to be.

    `canonical` is the row whose fields the card renders; `members` is every row
    behind it, kept so the card can badge each site and so a filter can ask
    about any of them.
    """

    canonical: Listing
    members: list[Listing] = field(default_factory=list)
    history: list[PriceHistory] = field(default_factory=list)

    # ------------------------------------------------------------- identity

    @property
    def id(self) -> int:
        """The canonical row's id — what /listing/<id>/history is asked for."""
        return self.canonical.id

    @property
    def market(self) -> str:
        return market_of(self.canonical.site)

    @property
    def sites(self) -> list[str]:
        """Every site carrying this car, in display order."""
        seen = {m.site for m in self.members}
        ordered = [s for s in SITE_PRIORITY if s in seen]
        return ordered + sorted(seen - set(ordered))

    @property
    def sources(self) -> dict[str, str]:
        """site -> the URL we have for it.

        For a syndicated ad the olx URL *is* an autovit URL — that is what the
        olx card links to, and inventing an olx.ro address we never saw would be
        worse than showing the one we did.
        """
        out: dict[str, str] = {}
        for site in SITE_PRIORITY:
            for member in self.members:
                if member.site == site and site not in out:
                    out[site] = member.url
        for member in self.members:
            out.setdefault(member.site, member.url)
        return out

    @property
    def is_merged(self) -> bool:
        return len(self.members) > 1

    @property
    def search_ids(self) -> set[int]:
        """Every search this car turned up in — one car can match several."""
        return {m.search_id for m in self.members}

    # ---------------------------------------------------------------- state

    @property
    def is_active(self) -> bool:
        """Active if any site still lists it. One site dropping an ad while
        another keeps it up is not the car being gone."""
        return any(m.is_active for m in self.members)

    @property
    def first_seen(self) -> datetime:
        """When we first learned of this car, on any site."""
        return min(m.first_seen for m in self.members)

    @property
    def last_seen(self) -> datetime:
        return max(m.last_seen for m in self.members)

    # ---------------------------------------------------------------- price

    @property
    def price(self) -> Optional[float]:
        return self.canonical.price

    @property
    def currency(self) -> Optional[str]:
        return self.canonical.currency

    @property
    def image_url(self) -> Optional[str]:
        """The first photo any member has — a card without one is the worst
        outcome, and which site supplied it doesn't matter to the reader."""
        for site in SITE_PRIORITY:
            for member in self.members:
                if member.site == site and member.image_url:
                    return member.image_url
        for member in self.members:
            if member.image_url:
                return member.image_url
        return None

    @property
    def prices(self) -> list[float]:
        """Ordered price trace for the sparkline. Empty below 2 observations."""
        if len(self.history) < 2:
            return []
        return [p.price for p in self.history]

    @property
    def first_price(self) -> Optional[float]:
        return self.history[0].price if self.history else None

    @property
    def delta_since_first(self) -> Optional[float]:
        """How far the price has moved since we started watching, or None."""
        first = self.first_price
        if first is None or self.price is None:
            return None
        delta = self.price - first
        return delta if abs(delta) >= 0.01 else None

    @property
    def is_drop(self) -> bool:
        return (self.delta_since_first or 0) < 0


def merge_listings(
    listings: Iterable[Listing],
    history: Optional[Mapping[int, Sequence[PriceHistory]]] = None,
) -> list[MergedListing]:
    """Group listing rows into one record per car.

    Order of the input is preserved for the first sighting of each car, so a
    caller that hasn't sorted yet gets something stable rather than
    dictionary-order noise. Sorting is the caller's job.
    """
    history = history or {}
    groups: dict[tuple[str, str], list[Listing]] = {}

    for listing in listings:
        groups.setdefault(merge_key(listing), []).append(listing)

    merged: list[MergedListing] = []
    for members in groups.values():
        canonical = _pick_canonical(members)
        merged.append(
            MergedListing(
                canonical=canonical,
                members=members,
                history=list(_longest_history(members, history)),
            )
        )
    return merged


def _pick_canonical(members: list[Listing]) -> Listing:
    """The row whose fields the card shows.

    Site priority first (§3, decision 4 makes the autovit price canonical); then
    the row with a photo, since a card without one is a visibly worse card; then
    the oldest, so the choice doesn't flip between renders.
    """
    return min(
        members,
        key=lambda m: (
            SITE_PRIORITY.index(m.site) if m.site in SITE_PRIORITY else len(SITE_PRIORITY),
            0 if m.image_url else 1,
            m.first_seen,
            m.id or 0,
        ),
    )


def _longest_history(
    members: list[Listing],
    history: Mapping[int, Sequence[PriceHistory]],
) -> Sequence[PriceHistory]:
    """The most informative single price trace among the members.

    Histories are *not* interleaved: two sites observing the same car produce
    two independent series, and splicing them would invent price movements that
    never happened (site A at 26 000, site B at 26 500, merged, reads as a rise).
    So we pick one series — the one with the most observations, and among equals
    the one that started earliest.
    """
    best: Sequence[PriceHistory] = ()
    best_key = (0, None)

    for member in members:
        points = history.get(member.id) or ()
        if not points:
            continue
        key = (len(points), points[0].observed_at)
        if key[0] > best_key[0] or (
            key[0] == best_key[0] and best_key[1] is not None and key[1] < best_key[1]
        ):
            best, best_key = points, key

    return best


# ------------------------------------------------------ the same change, twice

# One physical car is several listing rows, so every collection emits an event
# per row: a price drop on an ad that autovit and olx both carry, and that
# matches two of your searches, lands in the feed three times. Measured on live
# data: 87 event rows behind 42 Romanian cars, 38 of them repeated.
#
# Grouping them needs a rule for "the same change, reported twice" versus "two
# changes". A time window was the obvious first guess and it is wrong: the
# duplicates in this database are ~130 minutes apart, because autovit and olx
# were collected in separate runs, and since collection is manual that gap is
# whatever the user makes it - minutes or days. Any fixed window is a guess
# about their habits.
#
# The rule that needs no clock: duplicates always come from *different listing
# rows* for the same car, because that is what produces them. A second, genuine
# change to the same car necessarily produces another event on the *same* row.
# So a group takes at most one event per row, and the moment a row repeats, that
# is the next real change. Correct however far apart the sites were collected,
# and it never folds two real drops into one.


def _row_identity(listing: Listing):
    """A stable per-row key, usable before the row has been given an id."""
    if listing.id is not None:
        return listing.id
    return (listing.site, listing.site_listing_id, listing.search_id)


def group_events(pairs) -> list[list]:
    """Cluster `(event, listing)` pairs into one group per real-world change.

    Grouped on the same car identity the listings use, plus the event type, then
    split wherever a listing row reports twice - so a drop in September and
    another in October stay two rows however many sites saw each of them.

    Returns groups newest-first, each group ordered oldest-first.
    """
    buckets: dict[tuple, list] = {}
    for event, listing in pairs:
        buckets.setdefault((merge_key(listing), event.type), []).append((event, listing))

    groups: list[list] = []
    for members in buckets.values():
        members.sort(key=lambda pair: (pair[0].created_at, pair[0].id or 0))

        current: list = []
        seen: set = set()
        for pair in members:
            row = _row_identity(pair[1])
            if row in seen:
                groups.append(current)
                current, seen = [], set()
            current.append(pair)
            seen.add(row)
        if current:
            groups.append(current)

    groups.sort(
        key=lambda group: (group[-1][0].created_at, group[-1][0].id or 0), reverse=True
    )
    return groups


def pick_representative(pairs):
    """The `(event, listing)` pair a merged feed row should speak with.

    Same precedence as a merged card: autovit first, so the price quoted in the
    feed is the price shown on the Listings page. Nothing here averages or
    reconciles the sites' numbers - the sites do disagree, by a euro or two, on
    8 of the 38 syndicated ads in this database. One row is chosen and the rest
    are recorded as "also seen on".
    """
    return min(
        pairs,
        key=lambda pair: (
            SITE_PRIORITY.index(pair[1].site)
            if pair[1].site in SITE_PRIORITY
            else len(SITE_PRIORITY),
            pair[0].created_at,
            pair[0].id or 0,
        ),
    )
