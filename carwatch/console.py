"""Console encoding helper.

Windows consoles often default to a legacy code page (cp1252), where printing
any non-Latin-1 character — the arrows, dashes and box characters this tool uses
in its tables — raises UnicodeEncodeError and kills the process. Python only
switches stdout to UTF-8 automatically when it detects a UTF-8 terminal, which
is not the case under Task Scheduler, older PowerShell, or a piped stdout.

Every CLI entry point calls `setup_console()` before printing anything.
"""

from __future__ import annotations

import sys


def setup_console() -> None:
    """Make stdout/stderr tolerate non-ASCII output on any platform."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue  # already wrapped (pytest capture, redirects) — nothing to do
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # Detached or otherwise unreconfigurable stream: not worth failing
            # a collection run over.
            pass
