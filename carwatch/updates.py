"""Updating CarWatch from GitHub, from inside the dashboard.

For a copy installed with `git clone`: once a day the dashboard asks GitHub
whether there is anything new (`git fetch`), and the Runs page offers to apply
it (`git pull --ff-only`), install any new packages, and restart.

It never overwrites anything. It refuses, saying why, when CarWatch's own
files have been edited locally, when this copy has commits GitHub does not,
when it is not a git checkout at all, and while a collection is running.
`config.yaml`, the database and the logs are not tracked by git, so an update
cannot touch them.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from sqlmodel import Session

from carwatch.models import AppState, utcnow

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_KEY = "update_check"
CHECK_EVERY = timedelta(days=1)
RETRY_AFTER = timedelta(hours=1)
# How often the background thread looks whether a check is due.
WAKE_EVERY = timedelta(hours=1)
GIT_TIMEOUT = 60
PIP_TIMEOUT = 15 * 60
# The commit titles shown before updating; past this many, "and N more".
SHOW_COMMITS = 10


class UpdateError(RuntimeError):
    """Something the person can act on, worded for the Runs page."""


def _run(args: list[str], timeout: float = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    # No console window: the dashboard runs windowless, and on Windows every
    # child process would otherwise flash one.
    hidden = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        args, cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, creationflags=hidden,
    )


def _git(*args: str, timeout: float = GIT_TIMEOUT) -> str:
    result = _run(["git", *args], timeout=timeout)
    if result.returncode != 0:
        raise UpdateError((result.stderr or result.stdout).strip() or f"git {args[0]} failed")
    return result.stdout.strip()


# ---------------------------------------------------------------- status


@dataclass
class Status:
    checked_at: Optional[datetime] = None
    # Commits GitHub has that this copy does not: (short hash, title).
    commits: list[tuple[str, str]] = field(default_factory=list)
    behind: int = 0
    # Why updating is not possible right now, if it is not.
    blocked: str = ""
    error: str = ""

    @property
    def available(self) -> bool:
        return self.behind > 0


def unsupported_reason() -> str:
    """Why this copy cannot update itself at all, or "" when it can."""
    if shutil.which("git") is None:
        return "git is not installed, so CarWatch cannot fetch updates."
    if not (PROJECT_ROOT / ".git").exists():
        return (
            "This copy was not installed with `git clone`, so it cannot update "
            "itself. Install it again with `git clone` to get updates here."
        )
    return ""


def local_changes() -> list[str]:
    """CarWatch's own files edited in this copy — the ones an update would clobber.

    Tracked files only: config.yaml, the database and logs are not tracked, so
    they are never in the way.
    """
    # Not through _git: its .strip() would eat the leading space of the first
    # " M file" line, and the two status columns are positional.
    result = _run(["git", "status", "--porcelain", "--untracked-files=no"])
    if result.returncode != 0:
        raise UpdateError(result.stderr.strip() or "git status failed")
    return [line[3:] for line in result.stdout.splitlines() if line.strip()]


def current_version() -> str:
    try:
        return _git("rev-parse", "--short", "HEAD")
    except (UpdateError, OSError, subprocess.SubprocessError):
        return ""


def check(fetch: bool = True) -> Status:
    """Ask GitHub what is new. Never raises; problems go into the status."""
    status = Status(checked_at=utcnow())
    reason = unsupported_reason()
    if reason:
        status.blocked = reason
        return status
    try:
        if fetch:
            _git("fetch", "--quiet")
        try:
            _git("rev-parse", "--abbrev-ref", "@{u}")
        except UpdateError:
            status.blocked = "This copy does not track a GitHub branch, so there is nothing to update from."
            return status
        status.behind = int(_git("rev-list", "--count", "HEAD..@{u}") or 0)
        ahead = int(_git("rev-list", "--count", "@{u}..HEAD") or 0)
        lines = _git("log", "--format=%h%x09%s", f"-{SHOW_COMMITS}", "HEAD..@{u}")
        status.commits = [tuple(line.split("\t", 1)) for line in lines.splitlines() if "\t" in line]
        if status.behind:
            changed = local_changes()
            if changed:
                status.blocked = (
                    "CarWatch's own files were edited in this copy, and updating would "
                    "overwrite them: " + ", ".join(changed[:5])
                    + (f" and {len(changed) - 5} more" if len(changed) > 5 else "")
                    + ". Put them back with `git checkout -- <file>`, then update."
                )
            elif ahead:
                status.blocked = (
                    f"This copy has {ahead} commit{'' if ahead == 1 else 's'} of its own that "
                    "GitHub does not, so it cannot simply move forward. Update it with git."
                )
    except (UpdateError, OSError, subprocess.SubprocessError) as exc:
        status.error = _describe(exc)
    return status


def _describe(exc: Exception) -> str:
    text = str(exc)
    if isinstance(exc, subprocess.TimeoutExpired):
        return "GitHub did not answer in time."
    if "Could not resolve host" in text or "unable to access" in text:
        return "Could not reach GitHub — check the internet connection."
    return text.splitlines()[-1][:300] if text else exc.__class__.__name__


# ------------------------------------------------------- remembering a check


def load(session: Session) -> Optional[Status]:
    row = session.get(AppState, STATE_KEY)
    if row is None or not row.value:
        return None
    try:
        data = json.loads(row.value)
        data["checked_at"] = datetime.fromisoformat(data["checked_at"]) if data.get("checked_at") else None
        data["commits"] = [tuple(c) for c in data.get("commits", [])]
        return Status(**data)
    except (ValueError, TypeError, KeyError):
        return None


def save(session: Session, status: Status) -> None:
    data = asdict(status)
    data["checked_at"] = status.checked_at.isoformat() if status.checked_at else None
    row = session.get(AppState, STATE_KEY) or AppState(name=STATE_KEY)
    row.value = json.dumps(data)
    session.add(row)
    session.commit()


def due(previous: Optional[Status], now: datetime) -> bool:
    if previous is None or previous.checked_at is None:
        return True
    # A check that failed (offline, GitHub down) is tried again within the
    # hour rather than a day later.
    wait = RETRY_AFTER if previous.error else CHECK_EVERY
    return now - previous.checked_at >= wait


def start_daily_check(db_file, on_status: Callable[[Status], None]) -> threading.Thread:
    """Check GitHub once a day for as long as the dashboard runs.

    Started by the real dashboard only (carwatch.web.__main__), never by a test
    client. The last check is kept in the database, so restarting the dashboard
    does not ask again.
    """
    from carwatch.db import session_scope

    def loop() -> None:
        while True:
            try:
                with session_scope(db_file) as session:
                    previous = load(session)
                if previous is not None:
                    on_status(previous)
                if due(previous, utcnow()) and not unsupported_reason():
                    status = check()
                    with session_scope(db_file) as session:
                        save(session, status)
                    on_status(status)
            except Exception:  # never let the check take the dashboard down
                log.exception("update check failed")
            time.sleep(WAKE_EVERY.total_seconds())

    thread = threading.Thread(target=loop, name="carwatch-updates", daemon=True)
    thread.start()
    return thread


# ---------------------------------------------------------------- applying


@dataclass
class Applied:
    old: str
    new: str
    installed_packages: bool = False


def apply() -> Applied:
    """Move this copy forward to GitHub's version. Raises UpdateError."""
    reason = unsupported_reason()
    if reason:
        raise UpdateError(reason)
    status = check(fetch=True)
    if status.error:
        raise UpdateError(status.error)
    if status.blocked:
        raise UpdateError(status.blocked)
    if not status.behind:
        raise UpdateError("Already up to date.")

    old = _git("rev-parse", "HEAD")
    _git("merge", "--ff-only", "@{u}")
    new = _git("rev-parse", "HEAD")

    changed = _git("diff", "--name-only", old, new).splitlines()
    installed = False
    if "requirements.txt" in changed:
        # The new code may import a package the old one did not. sys.executable
        # is this copy's own .venv interpreter.
        result = _run(
            [sys.executable, "-m", "pip", "install", "--quiet", "-r", "requirements.txt"],
            timeout=PIP_TIMEOUT,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()[-1:] or ["pip failed"]
            raise UpdateError(
                "The update was applied, but installing its new packages failed: "
                f"{detail[0][:300]} Run setup again (setup.ps1 or setup.sh) to finish."
            )
        installed = True
    return Applied(old=old[:7], new=new[:7], installed_packages=installed)
