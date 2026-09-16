"""Scheduled collection: what the Runs page's schedule panel reads and writes.

CarWatch collects when you press Run. It can also collect on a timer — off
unless you turn it on — and this module holds that timer's settings. It never
runs a collection itself: the schedule is registered with the operating
system's own scheduler, so runs happen whether or not the dashboard is open.

* Windows — Task Scheduler, through `schedule.ps1`, which was already the way
  to register a schedule by hand. The panel calls the same script, so the
  command line and the dashboard cannot disagree about what a schedule is.
* macOS — launchd, with a LaunchAgent in ~/Library/LaunchAgents. Not cron, which
  `schedule.sh` uses: cron silently skips a run the Mac slept through, launchd
  runs it on wake.
* anything else — not handled here; `schedule.sh` installs a cron entry.

Whichever it is, the computer has to be on. A run that fell due while it was off
happens when it next can — on Windows as soon as it is back on, on macOS on wake
from sleep but not after a shutdown — and never twice.

The registration *is* the setting. Nothing is written to config.yaml (the
dashboard never writes `settings:`) or to the database: the panel reads the
schedule back from the task or the plist, so a schedule removed by hand shows
as off rather than as a setting that no longer does anything.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]

OFF, DAILY, HOURS = "off", "daily", "hours"
MODES = (OFF, DAILY, HOURS)

# Nothing more often than every six hours. The original plan asks for "at most a
# few times a day", and mobile.de — whose robots.txt disallows the route we use
# — was accepted on the basis of low volume. All three divide the day, so a
# schedule lands on the same clock times every day.
EVERY_HOURS = (6, 8, 12)

# Each scheduled run starts up to this many minutes late, at random (the
# wrappers' `--jitter`). A job firing at exactly the same second every day is
# the most machine-looking traffic pattern there is.
JITTER_MINUTES = 20

TASK_NAME = "CarWatch scheduled collection"
LAUNCHD_LABEL = "ro.carwatch.collect"
# What `schedule.sh` tags its cron line with — looked for so the panel can warn
# about a second, cron-driven schedule on a Mac.
CRON_MARKER = "# carwatch-daily"

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

Runner = Callable[..., subprocess.CompletedProcess]


class ScheduleError(Exception):
    """The system scheduler refused, or could not be asked. Shown to the user."""


# --------------------------------------------------------------- the setting


@dataclass(frozen=True)
class Schedule:
    """Off, once a day at `at`, or every `every_hours` hours starting at `at`."""

    mode: str = OFF
    at: str = "08:00"
    every_hours: Optional[int] = None

    @classmethod
    def from_form(cls, mode: str, at: str, every_hours: Optional[int]) -> "Schedule":
        """The panel's form, checked. Raises ValueError with a readable message."""
        mode = (mode or OFF).strip().lower()
        if mode not in MODES:
            raise ValueError(f"Unknown schedule {mode!r}.")
        if mode == OFF:
            return cls(OFF)

        at = (at or "").strip()
        if not _TIME_RE.match(at):
            raise ValueError("Pick a start time as HH:MM, for example 08:00.")
        if mode == DAILY:
            return cls(DAILY, at)

        if every_hours not in EVERY_HOURS:
            raise ValueError(
                "Every 6, 8 or 12 hours — nothing more often than every 6, because "
                "mobile.de was accepted on the basis of low volume."
            )
        return cls(HOURS, at, every_hours)

    @property
    def is_on(self) -> bool:
        return self.mode != OFF

    def times(self) -> list[tuple[int, int]]:
        """The clock times it fires at, starting from `at` (so not sorted)."""
        if self.mode == OFF:
            return []
        hour, minute = (int(part) for part in self.at.split(":"))
        if self.mode == DAILY:
            return [(hour, minute)]
        step = self.every_hours or 24
        return [((hour + k * step) % 24, minute) for k in range(24 // step)]

    def describe(self) -> str:
        if self.mode == OFF:
            return "Off"
        if self.mode == DAILY:
            return f"Every day at {self.at}"
        clock = ", ".join(f"{h:02d}:{m:02d}" for h, m in sorted(self.times()))
        return f"Every {self.every_hours} hours ({clock})"

    def next_after(self, now: datetime) -> Optional[datetime]:
        """When it next fires after `now`, ignoring the random delay."""
        best: Optional[datetime] = None
        for hour, minute in self.times():
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= now:
                candidate += timedelta(days=1)
            if best is None or candidate < best:
                best = candidate
        return best


# What a scheduled run writes when it starts — the moment it has picked its
# random delay — and removes when it finishes (run.ps1 / run.sh, --scheduled).
CURRENT_RUN_FILE = "scheduled-run.json"

# A record older than the longest a run can take is a run that died without
# cleaning up: the delay, plus the task's own two-hour limit.
CURRENT_RUN_STALE = timedelta(minutes=JITTER_MINUTES, hours=2)


@dataclass(frozen=True)
class CurrentRun:
    """A scheduled run under way: when it started, and when it collects.

    "Next run 18:50" is only the start of a twenty-minute window; the minute
    it actually collects is picked at random when it starts. This is that
    minute, so the dashboard can say it.
    """

    started: datetime
    collect_at: datetime

    def collecting(self, now: datetime) -> bool:
        return now >= self.collect_at


def read_current_run(root: Path, now: datetime) -> Optional[CurrentRun]:
    """The run under way, from the record the wrapper wrote, or None."""
    path = Path(root) / "logs" / CURRENT_RUN_FILE
    try:
        # utf-8-sig: Windows PowerShell 5.1 writes UTF-8 with a BOM.
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    started = _parse_time(data.get("started"))
    if started is None or now - started > CURRENT_RUN_STALE:
        return None
    return CurrentRun(started=started, collect_at=_parse_time(data.get("collect_at")) or started)


@dataclass
class ScheduleStatus:
    """What the panel shows: the schedule as registered, and how it is doing."""

    system: str
    supported: bool = True
    schedule: Schedule = field(default_factory=Schedule)
    next_run: Optional[datetime] = None
    last_run: Optional[datetime] = None
    last_result: Optional[str] = None
    problem: Optional[str] = None
    notes: list[str] = field(default_factory=list)
    # A scheduled run is under way, and — when the wrapper said — its minute.
    running: bool = False
    current: Optional[CurrentRun] = None

    @property
    def window(self) -> Optional[tuple[datetime, datetime]]:
        """When the next run will start collecting: somewhere in here."""
        if self.next_run is None:
            return None
        return self.next_run, self.next_run + timedelta(minutes=JITTER_MINUTES)


def day_label(moment: datetime, now: datetime) -> str:
    if moment.date() == now.date():
        return "today"
    if moment.date() == (now + timedelta(days=1)).date():
        return "tomorrow"
    return moment.strftime("%a %d %b")


def summary(status: Optional[ScheduleStatus], now: datetime) -> Optional[str]:
    """One line for the top bar: the run under way, or the next window."""
    if status is None or not status.supported or not status.schedule.is_on:
        return None
    if status.current is not None:
        if status.current.collecting(now):
            return "Scheduled run collecting now"
        return f"Scheduled run collecting at {status.current.collect_at:%H:%M}"
    if status.running:
        return "Scheduled run under way"
    if status.window:
        start, end = status.window
        return f"Next run {day_label(start, now)} {start:%H:%M}–{end:%H:%M}"
    return None


def _last_result(code) -> Optional[str]:
    """A scheduler's exit code, in words. 0 and 5 are the collector's own."""
    if code is None:
        return None
    try:
        code = int(code)
    except (TypeError, ValueError):
        return str(code)
    return {
        0: "collected cleanly",
        5: "at least one source was blocked or errored — see the run log below",
        267011: "not run yet",  # Task Scheduler's SCHED_S_TASK_HAS_NOT_RUN
        267009: "running now",  # SCHED_S_TASK_RUNNING
    }.get(code, f"failed with exit code {code} — see logs/")


def _message(result: subprocess.CompletedProcess) -> str:
    """The useful line of a failed command's output.

    PowerShell prefixes a script error with the script's path and follows it
    with "+ CategoryInfo" noise; the sentence in between is what to show.
    """
    text = (result.stderr or "").strip() or (result.stdout or "").strip()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("+", "At line", "At ")):
            continue
        return line.split(" : ", 1)[-1]
    return f"exit code {result.returncode}"


def _parse_time(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


# ------------------------------------------------------------------ Windows


class TaskScheduler:
    """Windows Task Scheduler, driven through `schedule.ps1`."""

    system = "Windows Task Scheduler"

    def __init__(
        self,
        root: Path = PROJECT_ROOT,
        run: Runner = subprocess.run,
        task_name: str = TASK_NAME,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.root = Path(root)
        self.run = run
        self.task_name = task_name
        self.now = now

    def _script(self, *args: str) -> subprocess.CompletedProcess:
        command = [
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-File", str(self.root / "schedule.ps1"),
            *args,
            "-TaskName", self.task_name,
        ]
        try:
            return self.run(command, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ScheduleError(f"Could not ask Task Scheduler: {exc}") from exc

    def _check(self, result: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
        if result.returncode != 0:
            raise ScheduleError(_message(result))
        return result

    def status(self) -> ScheduleStatus:
        result = self._check(self._script("-Status", "-Json"))
        try:
            data = json.loads((result.stdout or "").strip() or "{}")
        except ValueError as exc:
            raise ScheduleError(
                f"Task Scheduler answered in a way CarWatch could not read: "
                f"{(result.stdout or '')[:200]!r}"
            ) from exc

        status = ScheduleStatus(system=self.system)
        if not data.get("registered"):
            return status

        at = data.get("at") or "08:00"
        hours = int(data.get("every_hours") or 0)
        status.schedule = Schedule(HOURS, at, hours) if hours else Schedule(DAILY, at)
        status.next_run = _parse_time(data.get("next_run"))
        status.last_run = _parse_time(data.get("last_run"))
        status.last_result = _last_result(data.get("last_result"))
        status.running = data.get("state") == "Running"
        if status.running:
            status.current = read_current_run(self.root, self.now())
        if data.get("state") == "Disabled":
            status.problem = (
                "The task is registered but disabled in Task Scheduler, so it "
                "will not run. Save the schedule again to re-enable it."
            )
        # Missing from an older schedule.ps1's answer: say nothing then.
        if data.get("headless") is False:
            status.notes.append(
                "This schedule was saved by an older CarWatch, so every run opens "
                "a window. Press Save schedule once, with the same settings, to "
                "update it."
            )
        return status

    def apply(self, schedule: Schedule) -> None:
        if not schedule.is_on:
            self.remove()
            return
        args = ["-At", schedule.at]
        if schedule.mode == HOURS:
            args += ["-EveryHours", str(schedule.every_hours)]
        self._check(self._script(*args))

    def remove(self) -> None:
        self._check(self._script("-Remove"))


# -------------------------------------------------------------------- macOS


def _schedule_from_intervals(intervals) -> Schedule:
    """A LaunchAgent's StartCalendarInterval back into a Schedule."""
    if isinstance(intervals, dict):
        intervals = [intervals]
    times = [
        (int(i.get("Hour", 0)), int(i.get("Minute", 0)))
        for i in (intervals or [])
        if isinstance(i, dict)
    ]
    if not times:
        return Schedule()
    at = f"{times[0][0]:02d}:{times[0][1]:02d}"
    if len(times) == 1:
        return Schedule(DAILY, at)
    hours = 24 // len(times)
    candidate = Schedule(HOURS, at, hours)
    if hours in EVERY_HOURS and candidate.times() == times:
        return candidate
    # Hand-edited into something the panel does not offer. Report its first
    # time rather than guess; the problem line says why it looks odd.
    return Schedule(DAILY, at)


class Launchd:
    """macOS launchd, through a LaunchAgent plist and `launchctl`.

    Written and unit-tested against the documented plist format and
    `launchctl bootstrap`/`bootout`/`print`; exercised on a real Mac only when
    someone runs it on one.
    """

    system = "launchd"

    def __init__(
        self,
        root: Path = PROJECT_ROOT,
        run: Runner = subprocess.run,
        home: Optional[Path] = None,
        uid: Optional[int] = None,
        label: str = LAUNCHD_LABEL,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.root = Path(root)
        self.run = run
        self.home = Path(home) if home is not None else Path.home()
        self.uid = uid if uid is not None else getattr(os, "getuid", lambda: 0)()
        self.label = label
        self.now = now
        self.plist_path = self.home / "Library" / "LaunchAgents" / f"{label}.plist"

    @property
    def domain(self) -> str:
        return f"gui/{self.uid}"

    @property
    def target(self) -> str:
        return f"{self.domain}/{self.label}"

    def _launchctl(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        try:
            result = self.run(["launchctl", *args], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ScheduleError(f"Could not ask launchd: {exc}") from exc
        if check and result.returncode != 0:
            raise ScheduleError(f"launchctl {args[0]} failed: {_message(result)}")
        return result

    def _cron_notes(self) -> list[str]:
        try:
            result = self.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return []
        if CRON_MARKER in (result.stdout or ""):
            return [
                "schedule.sh has also installed a cron entry, so CarWatch may "
                "collect twice. Remove that one with ./schedule.sh --remove."
            ]
        return []

    def status(self) -> ScheduleStatus:
        status = ScheduleStatus(system=self.system, notes=self._cron_notes())
        if not self.plist_path.exists():
            return status

        try:
            data = plistlib.loads(self.plist_path.read_bytes())
        except Exception as exc:  # noqa: BLE001 - a broken file is shown, not raised
            status.problem = f"{self.plist_path} could not be read: {exc}"
            return status

        status.schedule = _schedule_from_intervals(data.get("StartCalendarInterval"))
        status.next_run = status.schedule.next_after(self.now())
        # launchctl has no "running" state worth parsing; the wrapper's own
        # record says it, and is stale-proofed.
        status.current = read_current_run(self.root, self.now())
        status.running = status.current is not None

        loaded = self._launchctl("print", self.target)
        if loaded.returncode != 0:
            status.problem = (
                "The schedule file is there, but launchd has not loaded it. Save "
                "the schedule again, or log out and back in."
            )
        else:
            match = re.search(r"last exit (?:code|status) = (-?\d+)", loaded.stdout or "")
            status.last_result = _last_result(int(match.group(1))) if match else "not run yet"
        return status

    def apply(self, schedule: Schedule) -> None:
        if not schedule.is_on:
            self.remove()
            return

        logs = self.root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        plist = {
            "Label": self.label,
            # bash explicitly, so run.sh needs no execute bit.
            "ProgramArguments": [
                "/bin/bash", str(self.root / "run.sh"),
                "--all", "--jitter", str(JITTER_MINUTES), "--scheduled",
            ],
            "WorkingDirectory": str(self.root),
            "StartCalendarInterval": [
                {"Hour": hour, "Minute": minute} for hour, minute in schedule.times()
            ],
            # run.sh keeps its own log; this catches anything before it starts.
            "StandardOutPath": str(logs / "launchd.log"),
            "StandardErrorPath": str(logs / "launchd.log"),
        }
        self.plist_path.parent.mkdir(parents=True, exist_ok=True)
        self.plist_path.write_bytes(plistlib.dumps(plist))

        # Replacing a loaded agent means unloading it first; "not loaded" is fine.
        self._launchctl("bootout", self.target)
        self._launchctl("bootstrap", self.domain, str(self.plist_path), check=True)

    def remove(self) -> None:
        self._launchctl("bootout", self.target)
        self.plist_path.unlink(missing_ok=True)


# -------------------------------------------------------------- elsewhere


class Unsupported:
    system = ""

    def status(self) -> ScheduleStatus:
        return ScheduleStatus(system="", supported=False)

    def apply(self, schedule: Schedule) -> None:
        raise ScheduleError(
            "Scheduling from the dashboard works on Windows and macOS. On this "
            "system, ./schedule.sh installs a cron entry instead."
        )

    def remove(self) -> None:
        self.apply(Schedule())


def backend_for(platform: Optional[str] = None, **kwargs):
    """The scheduler for this operating system."""
    platform = platform or sys.platform
    if platform.startswith("win"):
        return TaskScheduler(**kwargs)
    if platform == "darwin":
        return Launchd(**kwargs)
    return Unsupported()
