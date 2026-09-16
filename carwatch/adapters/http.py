"""Shared httpx client, headers and politeness for the plain-HTTP adapters.

Spec §14: randomized small delays between page requests, an honest User-Agent,
no aggressive parallelism. The delay is applied *between* requests, never before
the first one, so a single-page search stays fast.
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from carwatch.adapters.base import AdapterError, BlockedError

log = logging.getLogger(__name__)

# Markers that mean "a bot wall", not "no results".
#
# These must be *highly specific*. A bare "captcha" substring is not: autovit's
# ordinary result pages embed an anti-bot client config containing
# `disableAutoRefreshOnCaptchaPassed: true`, so matching on "captcha" flags every
# healthy page as blocked. Each entry below is a vendor-specific challenge
# fingerprint that does not appear on a normal page.
_CHALLENGE_MARKERS = (
    "cf-browser-verification",
    "cf_chl_opt",
    "/cdn-cgi/challenge-platform/",
    "px-captcha",
    "_pxhc",
    "geo.captcha-delivery.com",
    "data-dd-captcha",
    "g-recaptcha-response",
    "are you a robot",
    "verifying you are human",
    "enable javascript and cookies to continue",
)

# Challenge pages served in place of content are small. A full result page is
# ~1MB, so anything above this cannot be a challenge interstitial — used as a
# guard so a stray marker in a big page can never trip a false block.
_MAX_CHALLENGE_PAGE_BYTES = 200_000

# Titles that are themselves the block.
_CHALLENGE_TITLES = (
    "just a moment",
    "attention required",
    "access denied",
    "access to this page has been denied",
    "403 forbidden",
)

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)

_BLOCKED_STATUS = {401, 403, 407, 429, 451}

# Cap on how long we'll honour a Retry-After. Beyond this the site clearly wants
# us gone for a while, and blocking a collection run for an hour helps nobody -
# better to record `blocked` and let the next scheduled run try.
MAX_RETRY_AFTER_SECONDS = 120.0


def _retry_after_seconds(resp: "httpx.Response") -> Optional[float]:
    """Parse Retry-After, which is either a delay in seconds or an HTTP date."""
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None

    raw = raw.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass

    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None

    delta = when.timestamp() - time.time()
    return max(0.0, delta)


def looks_like_challenge(body: str) -> bool:
    """True if `body` is an anti-bot interstitial rather than real content."""
    title_match = _TITLE_RE.search(body[:_MAX_CHALLENGE_PAGE_BYTES])
    if title_match:
        title = title_match.group(1).strip().lower()
        if any(t in title for t in _CHALLENGE_TITLES):
            return True

    if len(body) > _MAX_CHALLENGE_PAGE_BYTES:
        return False

    lowered = body.lower()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)


class PoliteClient:
    """Thin httpx wrapper: honest UA, randomized inter-request delay, retries.

    One instance per adapter run, so the delay is tracked across all the pages
    of a single search.
    """

    def __init__(
        self,
        user_agent: str,
        delay_range: tuple[float, float] = (1.5, 4.0),
        timeout: float = 45.0,
        max_retries: int = 2,
    ) -> None:
        self.delay_range = delay_range
        self.max_retries = max_retries
        self._made_a_request = False
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ro-RO,ro;q=0.9,en;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            },
        )

    def __enter__(self) -> "PoliteClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _sleep_between(self) -> None:
        if not self._made_a_request:
            self._made_a_request = True
            return
        lo, hi = self.delay_range
        if hi > 0:
            time.sleep(random.uniform(lo, hi))

    def get_html(self, url: str) -> str:
        """GET a page and return its body.

        Raises BlockedError on a bot wall / challenge / 403 / 429, and
        AdapterError on anything else that stops us getting the page.
        """
        self._sleep_between()

        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.get(url)
            except httpx.TimeoutException as exc:
                last_exc = exc
            except httpx.HTTPError as exc:
                last_exc = exc
            else:
                # 429 means "slow down", not "go away" - honour Retry-After and
                # try again rather than treating a rate limit as a hard block.
                if resp.status_code == 429 and attempt < self.max_retries:
                    wait = _retry_after_seconds(resp) or (30.0 * (attempt + 1))
                    wait = min(wait, MAX_RETRY_AFTER_SECONDS)
                    log.info(
                        "%s asked us to slow down (HTTP 429); waiting %.0fs",
                        urlparse(url).netloc,
                        wait,
                    )
                    time.sleep(wait)
                    continue

                if resp.status_code in _BLOCKED_STATUS:
                    raise BlockedError(
                        f"{urlparse(url).netloc} returned HTTP {resp.status_code} "
                        f"(bot wall or rate limit) for {url}"
                    )
                if resp.status_code >= 500:
                    last_exc = AdapterError(
                        f"HTTP {resp.status_code} from {url}"
                    )
                else:
                    if resp.status_code != 200:
                        raise AdapterError(f"HTTP {resp.status_code} from {url}")
                    body = resp.text
                    if looks_like_challenge(body):
                        raise BlockedError(
                            f"{urlparse(url).netloc} served an anti-bot challenge page "
                            f"for {url}"
                        )
                    return body

            # Back off before retrying a timeout / 5xx.
            if attempt < self.max_retries:
                time.sleep(2.0 * (attempt + 1) + random.uniform(0, 1.0))

        raise AdapterError(f"Failed to fetch {url}: {last_exc}") from last_exc


def with_page_param(url: str, page: int, param: str = "page") -> str:
    """Return `url` with `param` set to `page`, replacing any existing value.

    The search URL is pasted from the user's address bar, so it may already
    carry a page number or arbitrary filter params — those must be preserved
    exactly or we'd be paginating a different search.
    """
    parts = urlparse(url)
    # keep_blank_values so filters like `?foo=` survive the round-trip
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != param]
    if page > 1:
        query.append((param, str(page)))
    # safe=":" keeps params like `search[filter_float_price:to]` byte-identical to
    # what the user pasted, rather than re-encoding the colon to %3A.
    return urlunparse(parts._replace(query=urlencode(query, safe=":")))
