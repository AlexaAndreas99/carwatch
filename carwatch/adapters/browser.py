"""Shared Playwright helper — the escalation path from spec §4.

Used only by sites that return a bot wall to plain HTTP (olx.ro today, likely
mobile.de in phase 8). Everything here is import-guarded: Playwright is an
optional dependency, and a machine without it must still be able to run the
other adapters, the collector and the whole test suite.

On honesty (§14): this drives a real Chromium, so the User-Agent it sends is
genuinely its own — we are not forging a browser identity to slip past a filter.
By default we append an honest CarWatch token to that UA so the traffic is
identifiable as this tool. Set `append_identity=False` if a site rejects the
modified UA; that is a judgement call for the user to make, not a default.
"""

from __future__ import annotations

import logging
import os
import random
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

IDENTITY = "CarWatch/1.0 (personal use)"

# Browsers installed into the project rather than the user profile.
#
# Playwright's default location is %LOCALAPPDATA%\ms-playwright (or
# ~/.cache/ms-playwright). A Windows *scheduled task* cannot reliably read that:
# the task's session sees the directory as empty, so a daily collection fails
# with "Executable doesn't exist" even though it launches fine interactively.
# Installing under the project directory removes the dependency on how a given
# session resolves the user profile.
#
#   Windows:  $env:PLAYWRIGHT_BROWSERS_PATH="$PWD\.playwright"; playwright install chromium
#   macOS:    PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright" playwright install chromium
PROJECT_BROWSERS = Path(__file__).resolve().parents[2] / ".playwright"


def _use_project_browsers_if_present() -> None:
    """Point Playwright at the project-local browsers, unless told otherwise.

    An explicit PLAYWRIGHT_BROWSERS_PATH in the environment always wins, so a
    user who manages browsers elsewhere is never overridden.
    """
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return
    if PROJECT_BROWSERS.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(PROJECT_BROWSERS)
        log.debug("Using project-local Playwright browsers at %s", PROJECT_BROWSERS)

# Pages that render results client-side need a moment after load.
DEFAULT_WAIT_MS = 2500
DEFAULT_TIMEOUT_MS = 45_000

# Lazy-loading pages fill an image's `src` only when its card scrolls into
# view, so reading the DOM straight after load yields photos for the first
# screenful and nothing after it. These bound the walk down the page.
DEFAULT_SCROLL_STEP_PX = 700
DEFAULT_SCROLL_PAUSE_MS = 120
DEFAULT_SCROLL_SETTLE_MS = 900
MAX_SCROLL_STEPS = 60


class BrowserUnavailableError(RuntimeError):
    """Playwright (or its Chromium build) isn't installed."""


class BrowserFetcher:
    """Fetches fully-rendered HTML using a headless Chromium.

    One instance drives one browser for the whole run and closes it on exit, so
    a multi-page search doesn't pay browser start-up per page.
    """

    def __init__(
        self,
        delay_range: tuple[float, float] = (1.5, 4.0),
        wait_ms: int = DEFAULT_WAIT_MS,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        append_identity: bool = True,
        wait_for_selector: Optional[str] = None,
        scroll_to_bottom: bool = False,
    ) -> None:
        self.delay_range = delay_range
        self.wait_ms = wait_ms
        self.timeout_ms = timeout_ms
        self.append_identity = append_identity
        self.wait_for_selector = wait_for_selector
        # Opt-in: it costs a couple of seconds per page and only pays off on a
        # site that lazy-loads its card images.
        self.scroll_to_bottom = scroll_to_bottom

        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._made_a_request = False

    # ------------------------------------------------------------------ setup

    @staticmethod
    def is_available() -> bool:
        try:
            import playwright  # noqa: F401
        except ImportError:
            return False
        return True

    def _ensure_started(self) -> None:
        if self._context is not None:
            return

        # Must happen before Playwright starts: it reads the env var at launch.
        _use_project_browsers_if_present()

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            # Terse: callers that surface this to the user add their own
            # install instructions, and duplicating them reads badly.
            raise BrowserUnavailableError("playwright is not installed") from exc

        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment specific
            self._playwright.stop()
            self._playwright = None
            raise BrowserUnavailableError(
                f"Could not launch Chromium ({exc}). If Playwright is installed "
                f"but its browser is not, run:\n    playwright install chromium"
            ) from exc

        kwargs: dict[str, Any] = {
            "locale": "ro-RO",
            "viewport": {"width": 1366, "height": 900},
        }
        if self.append_identity:
            # Chromium's own UA plus an honest identifying token.
            base = self._browser.new_context().new_page().evaluate("navigator.userAgent")
            kwargs["user_agent"] = f"{base} {IDENTITY}"

        self._context = self._browser.new_context(**kwargs)
        self._context.set_default_timeout(self.timeout_ms)

    # ---------------------------------------------------------------- fetching

    def _sleep_between(self) -> None:
        if not self._made_a_request:
            self._made_a_request = True
            return
        lo, hi = self.delay_range
        if hi > 0:
            time.sleep(random.uniform(lo, hi))

    def get_html(self, url: str) -> str:
        """Load `url` in a real browser and return the rendered HTML."""
        self._ensure_started()
        self._sleep_between()

        page = self._context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            if self.wait_for_selector:
                try:
                    page.wait_for_selector(self.wait_for_selector, timeout=self.wait_ms * 2)
                except Exception:
                    # Absent selector may just mean an empty result set; let the
                    # adapter's parser decide.
                    log.debug("selector %r not found on %s", self.wait_for_selector, url)
            else:
                page.wait_for_timeout(self.wait_ms)
            if self.scroll_to_bottom:
                self._scroll_through(page)
            return page.content()
        finally:
            page.close()

    def _scroll_through(self, page: Any) -> None:
        """Walk the page top to bottom so lazy images resolve, then come back.

        Result cards below the fold ship with an empty `src` and fill it in
        when they scroll into view — without this, olx yielded photos for 10 of
        47 cards. This adds no request to the site's own server: it triggers
        exactly the CDN image loads a person scrolling the page would.

        The step count is capped so a page that grows as you scroll (infinite
        feeds) can't turn this into an unbounded loop.
        """
        try:
            page.evaluate(
                """
                async ({ step, pause, maxSteps }) => {
                  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
                  for (let i = 0; i < maxSteps; i++) {
                    const y = i * step;
                    if (y > document.body.scrollHeight) break;
                    window.scrollTo(0, y);
                    await wait(pause);
                  }
                  window.scrollTo(0, document.body.scrollHeight);
                  await wait(pause);
                  window.scrollTo(0, 0);
                }
                """,
                {
                    "step": DEFAULT_SCROLL_STEP_PX,
                    "pause": DEFAULT_SCROLL_PAUSE_MS,
                    "maxSteps": MAX_SCROLL_STEPS,
                },
            )
            page.wait_for_timeout(DEFAULT_SCROLL_SETTLE_MS)
        except Exception:  # pragma: no cover - a scroll failing is not fatal
            # Worst case we capture what we already had: fewer photos, every
            # other field intact. Never worth losing a page over.
            log.debug("scroll pass failed on %s", page.url, exc_info=True)

    # ---------------------------------------------------------------- teardown

    def close(self) -> None:
        for attr in ("_context", "_browser"):
            obj = getattr(self, attr)
            if obj is not None:
                try:
                    obj.close()
                except Exception:  # pragma: no cover
                    pass
                setattr(self, attr, None)
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:  # pragma: no cover
                pass
            self._playwright = None

    def __enter__(self) -> "BrowserFetcher":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - best effort
        self.close()
