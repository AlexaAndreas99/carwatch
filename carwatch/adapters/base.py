"""The `SiteAdapter` contract every site module implements (spec §7).

Keeping this tiny is the point: a site breaking or getting blocked must never
break the others, so the collector only ever knows about `fetch_listings`,
`RawListing`, and the two error types below.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable


log = logging.getLogger(__name__)


class AdapterError(Exception):
    """A fetch failed for a reason that isn't the site deliberately blocking us."""


class BlockedError(AdapterError):
    """The site returned a bot wall, challenge, 403, or similar.

    The collector records status='blocked' and skips diffing for this search —
    a blocked fetch must never be read as "every listing got delisted".
    """


@dataclass
class RawListing:
    """Normalized listing shape, mapping onto the `listing` columns (§6).

    `site_listing_id` must be stable across runs — parse it from the site's own
    id or the canonical URL, never from position on the page. Diffing depends
    entirely on this.
    """

    site_listing_id: str
    url: str
    title: str
    price: Optional[float] = None
    currency: Optional[str] = None
    make: Optional[str] = None
    model: Optional[str] = None
    year: Optional[int] = None
    mileage_km: Optional[int] = None
    fuel: Optional[str] = None
    gearbox: Optional[str] = None
    location: Optional[str] = None
    # "new" when the site says the vehicle is unregistered, else None. Such a
    # car has no first-registration year, so this is what stops the Year column
    # from just being empty.
    condition: Optional[str] = None
    # The card photo's URL on the site's own CDN, hot-linked rather than
    # downloaded (front-end plan §4). None whenever a card carries no usable
    # image; the dashboard renders a placeholder for those.
    image_url: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SiteAdapter(Protocol):
    site_name: str

    def fetch_listings(
        self, search_url: str, max_pages: Optional[int] = None
    ) -> list[RawListing]:
        """Fetch ALL pages for the search URL and return normalized RawListings.

        Handles pagination internally. Raises BlockedError if the site returns a
        bot wall / 403 / challenge so the collector can record status='blocked'
        instead of treating it as 'no results'.
        """
        ...


class AdapterNotBuiltError(AdapterError):
    """The site is known and configured, but its adapter isn't written yet.

    Distinct from a fetch failure so the CLI can say "not built yet" instead of
    showing a ModuleNotFoundError traceback for a phase we haven't reached.
    """


def get_adapter(site: str, settings: Any) -> SiteAdapter:
    """Look up an adapter by config site key.

    Imports lazily so a broken or dependency-heavy adapter (e.g. mobile.de
    pulling in Playwright) can't stop the others from loading.
    """
    key = site.strip().lower()

    if key == "autovit":
        from carwatch.adapters.autovit import AutovitAdapter

        return AutovitAdapter(settings)

    if key == "olx":
        try:
            from carwatch.adapters.olx import OlxAdapter
        except ImportError as exc:
            raise AdapterNotBuiltError(_not_built("olx", 5)) from exc
        return OlxAdapter(settings)

    if key == "mobilede":
        try:
            from carwatch.adapters.mobilede import MobileDeAdapter
        except ImportError as exc:
            raise AdapterNotBuiltError(_not_built("mobilede", 8)) from exc
        return MobileDeAdapter(settings)

    raise AdapterError(f"No adapter for site {site!r}.")


def _not_built(site: str, phase: int) -> str:
    return (
        f"the {site} adapter is not built yet (planned for phase {phase}). "
        f"Remove or comment out the {site} source in config.yaml to silence this."
    )


def report_page(adapter: Any, page: int) -> None:
    """Tell whoever is watching which page `adapter` is about to fetch.

    The collector sets `adapter.on_page` so the dashboard's status strip can say
    "mobile.de, page 4" through the minute and a half that site takes. Optional
    by design: an adapter nobody is watching, or a test stub, has no `on_page`,
    and reporting progress must never be able to break a fetch.
    """
    callback = getattr(adapter, "on_page", None)
    if callback is None:
        return
    try:
        callback(page)
    except Exception:  # noqa: BLE001 - progress is decoration, the fetch is not
        log.debug("progress callback failed", exc_info=True)
