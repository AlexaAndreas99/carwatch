"""Picking a listing photo out of an HTML card.

Shared by the two DOM-scraping adapters (olx, mobile.de). autovit needs none of
this — its embedded JSON hands over `thumbnail.x1` / `.x2` directly.

Two things make this less trivial than reading `src`:

  * **Lazy loading.** Below the fold, an `<img>` often carries a 1x1 GIF or an
    inline `data:` placeholder in `src`, with the real URL parked in `data-src`
    or `srcset`. Reading `src` alone gives a page of grey squares for every card
    that had not been scrolled to.
  * **`srcset` is a list, and bigger is not better.** mobile.de offers the same
    photo from 32w up to 1600w. Taking the largest is easy and wrong: cards are
    268px, so a 173-card page then pulls 173 images at 1600px. We ask for the
    smallest size that still covers a 2x card, and only fall back to the
    largest on offer when nothing reaches it.

Everything here is hot-linked, never downloaded (front-end plan §4), so the
result must be an absolute URL a browser can fetch on its own.
"""

from __future__ import annotations

import re
from typing import Any, Optional

# Fallback attributes, in order, for an image with no usable `srcset`. `src`
# first: when it holds a real URL it is unambiguously what the page is showing.
_URL_ATTRS = ("src", "data-src", "data-original", "data-lazy-src")

_SRCSET_DESCRIPTOR = re.compile(r"^(\d+(?:\.\d+)?)([wx])$")

# Cards are 268px wide (front-end plan §7) and displays are commonly 2x, so
# ~536px is the real requirement; 640 is the next size sites actually offer.
TARGET_WIDTH = 640


def image_url_from(node: Any, target_width: int = TARGET_WIDTH) -> Optional[str]:
    """The best absolute image URL on one `<img>`-like node, or None.

    `srcset` wins when it offers something usable: it is the site telling us
    which sizes exist, and it is the only way to avoid downloading a 1600px
    photo for a 268px card. `src` is the fallback, and is what olx and autovit
    end up using since neither offers a sized set on its result cards.

    Duck-typed on `.attributes` so this stays independent of the HTML parser and
    is trivial to test with a plain dict.
    """
    attrs = getattr(node, "attributes", None) or {}

    chosen = _from_srcset(attrs.get("srcset"), target_width)
    if chosen:
        return chosen

    for attr in _URL_ATTRS:
        url = _clean(attrs.get(attr))
        if url:
            return url

    return None


def _clean(value: Optional[str]) -> Optional[str]:
    """Normalise one candidate URL, rejecting placeholders."""
    if not value:
        return None
    url = value.strip()
    if not url:
        return None

    # `data:` is the inline placeholder, not the car. `blob:` can't be
    # hot-linked from another page at all.
    lowered = url.lower()
    if lowered.startswith(("data:", "blob:", "about:", "javascript:")):
        return None

    # Protocol-relative "//cdn/…" works inside the origin page but not in a
    # stored value we render from localhost.
    if url.startswith("//"):
        return "https:" + url

    if not lowered.startswith(("http://", "https://")):
        # A site-relative path has no host here, and guessing one per adapter
        # would be wrong as often as right. Let the caller fall back.
        return None

    return url


def _from_srcset(srcset: Optional[str], target_width: int) -> Optional[str]:
    """The entry of a `srcset` closest to what a card needs, or None.

    Entries are "<url> <descriptor>", comma-separated, where the descriptor is
    a width ("640w"), a pixel density ("2x"), or absent. Densities are converted
    to widths against a nominal 1x card so both kinds can be compared at all —
    rough on purpose, since the choice only has to land on the right rung of a
    site's size ladder, not be exact.

    Preference: the **smallest** candidate that is at least `target_width`.
    That is the one that fills a 2x card without wasting bandwidth. If every
    candidate is smaller, take the largest — a slightly soft photo beats none.
    """
    if not srcset:
        return None

    candidates: list[tuple[float, str]] = []
    for entry in srcset.split(","):
        parts = entry.split()
        if not parts:
            continue
        url = _clean(parts[0])
        if not url:
            continue

        width = 0.0
        if len(parts) > 1:
            match = _SRCSET_DESCRIPTOR.match(parts[1])
            if match:
                value = float(match.group(1))
                width = value if match.group(2) == "w" else value * (TARGET_WIDTH / 2)
        candidates.append((width, url))

    if not candidates:
        return None

    big_enough = [c for c in candidates if c[0] >= target_width]
    if big_enough:
        return min(big_enough, key=lambda c: c[0])[1]
    return max(candidates, key=lambda c: c[0])[1]


def in_block(node: Any, testid_suffix: str) -> bool:
    """True if `node` sits inside an element whose data-testid ends with the suffix.

    Used to keep a dealer's logo out of the running for "the car's photo": both
    are plain `<img>` inside the same card, and only their position tells them
    apart. Walking up beats trusting document order, which reshuffles whenever a
    site changes its card layout.
    """
    current = getattr(node, "parent", None)
    while current is not None:
        attrs = getattr(current, "attributes", None) or {}
        if (attrs.get("data-testid") or "").endswith(testid_suffix):
            return True
        current = getattr(current, "parent", None)
    return False
