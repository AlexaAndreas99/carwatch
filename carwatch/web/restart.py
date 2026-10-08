"""Start the dashboard again once the old one has let go of its port.

    python -m carwatch.web.restart PORT -- <arguments for carwatch.web>

Run, detached, by a dashboard that has just updated itself: the running
process still holds the old code, so it starts this and stops. This waits
until nothing answers on PORT any more, then starts a fresh dashboard with the
same arguments the old one had - same port, same idle timeout, same log.
"""

from __future__ import annotations

import sys
import time

from carwatch.launch import _windowless_python, is_running, spawn_detached

# The old dashboard finishes the request that asked for the restart first,
# then shuts down; a few seconds normally. Past this, start anyway and let the
# new one report the port as taken in its log.
WAIT_SECONDS = 60


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2 or argv[1] != "--":
        print(__doc__, file=sys.stderr)
        return 1
    port, web_args = int(argv[0]), argv[2:]

    url = f"http://127.0.0.1:{port}/"
    deadline = time.monotonic() + WAIT_SECONDS
    while is_running(url) and time.monotonic() < deadline:
        time.sleep(0.5)

    spawn_detached([_windowless_python(), "-m", "carwatch.web", *web_args])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
