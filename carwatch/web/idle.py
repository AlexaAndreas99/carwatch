"""Stop the dashboard once nobody has it open.

The desktop icon starts the dashboard with no window, so closing the browser
is the only "I'm done" there is. Browsers do not tell a server a tab closed,
so every open page says it is still there instead — a heartbeat every half
minute (see base.html) — and the server stops when those have been quiet for
a while.

Only for the icon's copy: `python -m carwatch.web --idle-exit MINUTES`. Run
any other way, the dashboard stays up until you stop it.

Two things must not stop it:

* **A collection in progress.** Press Run and close the browser, and the run
  finishes first; the quiet period is counted from then on.
* **Waking from sleep.** Everything is suspended while the computer sleeps,
  the browser's timer included, so on waking the last heartbeat looks hours
  old — and whether this clock counted the sleep depends on the platform. The
  watch notices its own checks stopped for far longer than it waits between
  them, takes that as a sleep, and starts the quiet period over, which gives
  the page time to wake up and check in.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

# How often an open page checks in. base.html reads it from here, so the two
# cannot drift apart.
HEARTBEAT_SECONDS = 30

# How often the watch looks.
TICK_SECONDS = 15.0

# A gap between two looks this much longer than a tick was not the watch
# being slow; the computer was asleep.
WAKE_GAP_SECONDS = TICK_SECONDS * 4


class IdleWatch:
    """Knows when a page last checked in, and whether it is time to stop."""

    def __init__(
        self,
        idle_seconds: float,
        is_busy: Callable[[], bool] = lambda: False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.idle_seconds = idle_seconds
        self.is_busy = is_busy
        self.clock = clock
        # Started counts as seen: the icon opens the browser straight after,
        # and a start with no browser at all still stops on its own.
        now = clock()
        self._last_seen = now
        self._last_check = now
        self._lock = threading.Lock()

    def seen(self) -> None:
        """A page is open. Called by the heartbeat."""
        with self._lock:
            self._last_seen = self.clock()

    def should_stop(self) -> bool:
        """Look once. True when it has been quiet long enough and nothing runs."""
        with self._lock:
            now = self.clock()
            if now - self._last_check > WAKE_GAP_SECONDS:
                self._last_seen = now
            self._last_check = now

            if self.is_busy():
                # The quiet period starts when the run ends, not before it.
                self._last_seen = now
                return False
            return now - self._last_seen > self.idle_seconds

    def watch(self, stop: Callable[[], None], tick: float = TICK_SECONDS) -> threading.Thread:
        """Look every `tick` seconds on a background thread; call `stop` once."""

        def loop() -> None:
            while True:
                time.sleep(tick)
                if self.should_stop():
                    stop()
                    return

        thread = threading.Thread(target=loop, name="carwatch-idle", daemon=True)
        thread.start()
        return thread
