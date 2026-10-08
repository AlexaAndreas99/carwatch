"""Open the CarWatch dashboard: start it if it isn't running, then open the browser.

This is what the Windows desktop icon runs, as

    pythonw -m carwatch.launch

and on a Mac what CarWatch.app runs, through CarWatch.command.

`pythonw` is the point. The icon used to run a PowerShell script, and Windows
gives PowerShell a console window before PowerShell can hide it, so every
double-click flashed a black window. pythonw never gets one, and nor does the
dashboard it starts, so nothing appears but the browser. The Mac's equivalent
is the AppleScript app setup.sh builds, which runs this out of sight.

    python -m carwatch.launch               # the same, from a terminal
    python -m carwatch.launch --no-browser  # start if needed, and nothing else

The dashboard it starts has no window, writes to logs/dashboard.log, and stops
by itself once no CarWatch page has been open for a few minutes (see
carwatch.web.idle). Starting it again while it runs just opens the browser —
it never starts a second copy.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG = PROJECT_ROOT / "logs" / "dashboard.log"

# How long to wait for a fresh dashboard to answer. It normally takes one or
# two seconds; twenty is plenty.
START_TIMEOUT = 20.0


def is_running(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2):
            return True
    except (urllib.error.URLError, OSError):
        return False


def _windowless_python() -> str:
    """pythonw next to this interpreter, so the dashboard gets no console either."""
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return str(pythonw) if pythonw.exists() else str(exe)


def start(port: int, idle_minutes: float, log: Path = LOG) -> None:
    """Start the dashboard in the background, outliving this launcher."""
    spawn_detached([
        _windowless_python(), "-m", "carwatch.web",
        "--port", str(port),
        "--idle-exit", f"{idle_minutes:g}",
        "--log", str(log),
    ])


def spawn_detached(command: list[str]) -> None:
    """Run `command` in the background, with no window, outliving its parent."""
    if sys.platform == "win32":
        detach = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        detach = {"start_new_session": True}
    subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        **detach,
    )


def tell(message: str) -> None:
    """Say something went wrong — in a message box, since there is no console.

    Elsewhere, to stderr: CarWatch.app shows whatever arrives there in a
    dialog of its own, and a terminal shows it anyway.
    """
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "CarWatch", 0x10)  # MB_ICONERROR
    else:
        print(message, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m carwatch.launch", description=__doc__.splitlines()[0])
    p.add_argument("--port", type=int, default=8009)
    p.add_argument("--idle-minutes", type=float, default=5)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--log", type=Path, default=LOG, help=f"default: {LOG}")
    args = p.parse_args(argv)
    url = f"http://127.0.0.1:{args.port}/"

    if not is_running(url):
        start(args.port, args.idle_minutes, args.log)
        deadline = time.monotonic() + START_TIMEOUT
        while not is_running(url):
            if time.monotonic() > deadline:
                tell(f"The CarWatch dashboard did not start. The reason is in {args.log}")
                return 1
            time.sleep(0.5)

    if not args.no_browser:
        webbrowser.open(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
