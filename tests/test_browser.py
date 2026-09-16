"""Playwright helper tests — no browser is launched here."""

import os

import pytest

from carwatch.adapters import browser as browser_mod
from carwatch.adapters.browser import BrowserFetcher, _use_project_browsers_if_present


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)


def test_project_browsers_used_when_present(monkeypatch, tmp_path):
    """A Windows scheduled task can't read the profile-local install, so a
    project-local .playwright directory must win by default."""
    local = tmp_path / ".playwright"
    local.mkdir()
    monkeypatch.setattr(browser_mod, "PROJECT_BROWSERS", local)

    _use_project_browsers_if_present()
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(local)


def test_explicit_env_var_is_never_overridden(monkeypatch, tmp_path):
    """Someone managing browsers elsewhere must not be hijacked."""
    local = tmp_path / ".playwright"
    local.mkdir()
    monkeypatch.setattr(browser_mod, "PROJECT_BROWSERS", local)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/somewhere/else")

    _use_project_browsers_if_present()
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "/somewhere/else"


def test_absent_project_dir_leaves_env_alone(monkeypatch, tmp_path):
    monkeypatch.setattr(browser_mod, "PROJECT_BROWSERS", tmp_path / "nope")

    _use_project_browsers_if_present()
    assert "PLAYWRIGHT_BROWSERS_PATH" not in os.environ


def test_is_available_reflects_the_import(monkeypatch):
    assert BrowserFetcher.is_available() is True


def test_delay_is_skipped_before_the_first_request(monkeypatch):
    """No point pausing politely before we've made any request at all."""
    slept = []
    monkeypatch.setattr("carwatch.adapters.browser.time.sleep", lambda s: slept.append(s))

    fetcher = BrowserFetcher(delay_range=(1.0, 1.0))
    fetcher._sleep_between()
    assert slept == []
    fetcher._sleep_between()
    assert slept == [1.0]


def test_close_is_safe_when_never_started():
    BrowserFetcher().close()  # must not raise


# ------------------------------------------------------- lazy-image scrolling


class FakePage:
    """Stands in for a Playwright page; records what it was asked to do."""

    url = "https://www.olx.ro/x"

    def __init__(self, evaluate_raises=None):
        self.evaluated = []
        self.waits = []
        self.evaluate_raises = evaluate_raises

    def evaluate(self, script, arg=None):
        self.evaluated.append((script, arg))
        if self.evaluate_raises:
            raise self.evaluate_raises

    def wait_for_timeout(self, ms):
        self.waits.append(ms)


def test_scrolling_is_off_by_default():
    assert BrowserFetcher().scroll_to_bottom is False


def test_scroll_pass_walks_the_page_and_settles():
    page = FakePage()
    BrowserFetcher(scroll_to_bottom=True)._scroll_through(page)

    (script, arg), = page.evaluated
    assert "scrollTo" in script
    assert arg["step"] == browser_mod.DEFAULT_SCROLL_STEP_PX
    assert arg["maxSteps"] == browser_mod.MAX_SCROLL_STEPS
    # A settle wait after the walk, so the last images have a chance to arrive.
    assert page.waits == [browser_mod.DEFAULT_SCROLL_SETTLE_MS]


def test_scroll_is_bounded_so_a_growing_page_cannot_loop_forever():
    """An infinite feed extends as you scroll; the cap is what stops that."""
    assert browser_mod.MAX_SCROLL_STEPS > 0
    page = FakePage()
    BrowserFetcher(scroll_to_bottom=True)._scroll_through(page)
    assert page.evaluated[0][1]["maxSteps"] == browser_mod.MAX_SCROLL_STEPS


def test_a_failed_scroll_never_loses_the_page():
    """Worst case we capture fewer photos — every other field is still there,
    so a scroll blowing up must not cost us the whole fetch."""
    page = FakePage(evaluate_raises=RuntimeError("execution context destroyed"))
    BrowserFetcher(scroll_to_bottom=True)._scroll_through(page)  # must not raise
