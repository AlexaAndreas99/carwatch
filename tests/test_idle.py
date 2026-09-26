"""The dashboard stopping itself once no page is open (carwatch.web.idle)."""

import threading

from fastapi.testclient import TestClient

from carwatch.web.idle import TICK_SECONDS, IdleWatch

from tests.test_web import app_and_config, create_app  # noqa: F401  (fixture)

IDLE = 300  # five minutes, as the desktop icon uses


class Clock:
    """A clock the test moves by hand, driving the watch it belongs to."""

    def __init__(self):
        self.now = 1000.0
        self.watch = None

    def __call__(self):
        return self.now

    def advance(self, seconds, tick=TICK_SECONDS):
        """Pass time the way the watch sees it: one look per tick."""
        end = self.now + seconds
        stopped = False
        while self.now < end:
            self.now = min(self.now + tick, end)
            stopped = stopped or self.watch.should_stop()
        return stopped


def make(busy=lambda: False):
    clock = Clock()
    watch = IdleWatch(IDLE, is_busy=busy, clock=clock)
    clock.watch = watch
    return clock, watch


def test_stops_once_quiet_for_the_idle_period():
    clock, _ = make()
    assert not clock.advance(IDLE - TICK_SECONDS)
    assert clock.advance(2 * TICK_SECONDS)


def test_a_heartbeat_keeps_it_running():
    clock, watch = make()
    for _ in range(40):  # twenty minutes, checked in every half minute
        assert not clock.advance(30)
        watch.seen()


def test_a_running_collection_holds_it_open_and_restarts_the_quiet_period():
    busy = threading.Event()
    busy.set()
    clock, _ = make(busy=busy.is_set)
    assert not clock.advance(IDLE * 3)

    busy.clear()
    # Counted from the end of the run, not from the last page.
    assert not clock.advance(IDLE - TICK_SECONDS)
    assert clock.advance(2 * TICK_SECONDS)


def test_waking_from_sleep_starts_the_quiet_period_over():
    clock, watch = make()
    assert not clock.advance(60)
    watch.seen()

    # Asleep overnight: the watch's thread did not run, so its next look comes
    # hours after the last one, and the last heartbeat looks hours old.
    clock.now += 8 * 3600
    assert not watch.should_stop()

    # The page wakes up and checks in well inside the fresh quiet period.
    assert not clock.advance(30)
    watch.seen()
    assert not clock.advance(IDLE - TICK_SECONDS)


def test_nothing_after_waking_still_stops_it():
    clock, watch = make()
    clock.now += 8 * 3600
    assert not watch.should_stop()
    # The browser was closed before the sleep, so nothing checks in.
    assert clock.advance(IDLE + TICK_SECONDS)


def test_heartbeat_route_answers_and_counts(app_and_config):  # noqa: F811
    path, _ = app_and_config
    app = create_app(path)
    client = TestClient(app)

    # Without --idle-exit there is no watch, but the page still gets an answer.
    assert client.post("/heartbeat").status_code == 204

    clock, watch = make()
    app.state.idle = watch
    clock.advance(IDLE - TICK_SECONDS)
    assert client.post("/heartbeat").status_code == 204
    assert not clock.advance(2 * TICK_SECONDS)


def test_every_page_carries_the_heartbeat(app_and_config):  # noqa: F811
    path, _ = app_and_config
    r = TestClient(create_app(path)).get("/changes")
    assert "fetch('/heartbeat'" in r.text
    assert 'id="stopped-notice"' in r.text
