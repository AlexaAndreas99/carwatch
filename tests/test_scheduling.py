"""The schedule: settings, the two system schedulers, and the Runs page panel.

The Windows and macOS backends are tested against a fake command runner —
nothing here registers a real task or LaunchAgent. The one test that touches
the real Task Scheduler asks it, read-only, about a task that does not exist.
"""

import json
import plistlib
import subprocess
import sys
import textwrap
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from carwatch import scheduling
from carwatch.scheduling import (
    DAILY,
    HOURS,
    OFF,
    CurrentRun,
    Launchd,
    Schedule,
    ScheduleError,
    ScheduleStatus,
    TaskScheduler,
    Unsupported,
    backend_for,
    read_current_run,
    summary,
)
from carwatch.web.app import create_app

# The dashboard's clock in these tests: 15:00 on the day before the fake
# schedule's next run, so "tomorrow" is always right.
NOW = datetime(2026, 9, 11, 15, 0)

# ---------------------------------------------------------------- settings


def test_off_needs_nothing_else():
    assert Schedule.from_form("off", "", None) == Schedule(OFF)


def test_daily_needs_a_time():
    assert Schedule.from_form("daily", "07:30", None) == Schedule(DAILY, "07:30")
    with pytest.raises(ValueError, match="HH:MM"):
        Schedule.from_form("daily", "7.30", None)


@pytest.mark.parametrize("hours", [6, 8, 12])
def test_every_six_eight_or_twelve_hours(hours):
    assert Schedule.from_form("hours", "08:00", hours) == Schedule(HOURS, "08:00", hours)


@pytest.mark.parametrize("hours", [1, 2, 4, 5, None])
def test_nothing_more_often_than_every_six_hours(hours):
    with pytest.raises(ValueError, match="nothing more often"):
        Schedule.from_form("hours", "08:00", hours)


def test_an_unknown_mode_is_refused():
    with pytest.raises(ValueError):
        Schedule.from_form("hourly", "08:00", 6)


def test_every_six_hours_lands_on_four_times_a_day():
    assert Schedule(HOURS, "08:00", 6).times() == [(8, 0), (14, 0), (20, 0), (2, 0)]


def test_times_wrap_past_midnight():
    assert Schedule(HOURS, "22:30", 12).times() == [(22, 30), (10, 30)]


def test_describe():
    assert Schedule().describe() == "Off"
    assert Schedule(DAILY, "08:00").describe() == "Every day at 08:00"
    assert (
        Schedule(HOURS, "08:00", 8).describe() == "Every 8 hours (00:00, 08:00, 16:00)"
    )


def test_next_run_is_the_next_time_after_now():
    s = Schedule(HOURS, "08:00", 6)
    assert s.next_after(datetime(2026, 9, 11, 15, 0)) == datetime(2026, 9, 11, 20, 0)
    assert s.next_after(datetime(2026, 9, 11, 21, 0)) == datetime(2026, 9, 12, 2, 0)


# ------------------------------------------------------------------ runner


class Runner:
    """Records commands and answers them from a script of (match, result)."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or []

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        for match, (code, out, err) in self.answers:
            if match in " ".join(command):
                return subprocess.CompletedProcess(command, code, out, err)
        return subprocess.CompletedProcess(command, 0, "", "")


# ----------------------------------------------------------------- Windows


def test_windows_status_when_nothing_is_registered():
    run = Runner([("-Status", (0, '{"registered": false}', ""))])
    status = TaskScheduler(run=run).status()

    assert status.schedule == Schedule(OFF)
    assert "-Json" in run.calls[0]


def test_windows_status_reads_the_schedule_back():
    answer = json.dumps(
        {
            "registered": True,
            "state": "Ready",
            "at": "08:00",
            "every_hours": 6,
            "next_run": "2026-09-11T14:00:00",
            "last_run": None,
            "last_result": 267011,
        }
    )
    status = TaskScheduler(run=Runner([("-Status", (0, answer, ""))])).status()

    assert status.schedule == Schedule(HOURS, "08:00", 6)
    assert status.next_run == datetime(2026, 9, 11, 14, 0)
    assert status.last_result == "not run yet"
    assert status.problem is None


def test_windows_says_when_the_task_is_disabled():
    answer = json.dumps({"registered": True, "state": "Disabled", "at": "08:00", "every_hours": 0})
    status = TaskScheduler(run=Runner([("-Status", (0, answer, ""))])).status()

    assert status.schedule == Schedule(DAILY, "08:00")
    assert "disabled" in status.problem


def test_windows_daily_registration():
    run = Runner()
    TaskScheduler(run=run).apply(Schedule(DAILY, "07:30"))

    command = run.calls[0]
    assert command[command.index("-At") + 1] == "07:30"
    assert "-EveryHours" not in command
    assert command[command.index("-TaskName") + 1] == scheduling.TASK_NAME


def test_windows_every_n_hours_registration():
    run = Runner()
    TaskScheduler(run=run).apply(Schedule(HOURS, "08:00", 8))

    command = run.calls[0]
    assert command[command.index("-EveryHours") + 1] == "8"


def test_windows_off_unregisters():
    run = Runner()
    TaskScheduler(run=run).apply(Schedule(OFF))
    assert "-Remove" in run.calls[0]


def test_windows_refusal_shows_the_scripts_own_sentence():
    err = (
        "C:\\carwatch\\schedule.ps1 : Access is denied.\n"
        "    + CategoryInfo          : NotSpecified: (:) [Write-Error]\n"
    )
    run = Runner([("-At", (1, "", err))])
    with pytest.raises(ScheduleError, match="^Access is denied.$"):
        TaskScheduler(run=run).apply(Schedule(DAILY, "08:00"))


def test_windows_unreadable_status_is_an_error_not_a_crash():
    run = Runner([("-Status", (0, "not json", ""))])
    with pytest.raises(ScheduleError, match="could not read"):
        TaskScheduler(run=run).status()


@pytest.mark.skipif(sys.platform != "win32", reason="needs Windows Task Scheduler")
def test_the_real_script_reports_a_missing_task_as_json():
    """Read-only: asks the real Task Scheduler about a task that cannot exist."""
    backend = TaskScheduler(task_name=f"CarWatch test {uuid.uuid4()}")
    assert backend.status().schedule == Schedule(OFF)


# ------------------------------------------------------------------- macOS


@pytest.fixture
def mac(tmp_path):
    run = Runner()
    backend = Launchd(
        root=tmp_path / "carwatch",
        run=run,
        home=tmp_path / "home",
        uid=501,
        now=lambda: datetime(2026, 9, 11, 15, 0),
    )
    return backend, run


def test_mac_registration_writes_a_launch_agent(mac):
    backend, run = mac
    backend.apply(Schedule(HOURS, "08:00", 12))

    plist = plistlib.loads(backend.plist_path.read_bytes())
    assert plist["Label"] == scheduling.LAUNCHD_LABEL
    assert plist["StartCalendarInterval"] == [
        {"Hour": 8, "Minute": 0},
        {"Hour": 20, "Minute": 0},
    ]
    assert plist["ProgramArguments"][1].endswith("run.sh")
    assert plist["ProgramArguments"][-3:] == ["--jitter", "20", "--scheduled"]

    commands = [" ".join(c) for c in run.calls]
    assert any(c.startswith("launchctl bootout gui/501/") for c in commands)
    assert any(c.startswith("launchctl bootstrap gui/501 ") for c in commands)


def test_mac_status_reads_the_schedule_back(mac):
    backend, run = mac
    backend.apply(Schedule(HOURS, "08:00", 6))
    run.answers = [("launchctl print", (0, "state = waiting\n\tlast exit code = 0\n", ""))]

    status = backend.status()
    assert status.schedule == Schedule(HOURS, "08:00", 6)
    assert status.next_run == datetime(2026, 9, 11, 20, 0)
    assert status.last_result == "collected cleanly"


def test_mac_status_when_nothing_is_registered(mac):
    backend, _ = mac
    assert backend.status().schedule == Schedule(OFF)


def test_mac_says_when_launchd_has_not_loaded_it(mac):
    backend, run = mac
    backend.apply(Schedule(DAILY, "08:00"))
    run.answers = [("launchctl print", (113, "", "Could not find service"))]

    assert "not loaded" in backend.status().problem


def test_mac_off_unloads_and_removes_the_file(mac):
    backend, run = mac
    backend.apply(Schedule(DAILY, "08:00"))
    backend.apply(Schedule(OFF))

    assert not backend.plist_path.exists()
    assert "launchctl bootout" in " ".join(run.calls[-1])


def test_mac_refusal_is_an_error(mac):
    backend, run = mac
    run.answers = [("launchctl bootstrap", (5, "", "Bootstrap failed: 5: Input/output error"))]
    with pytest.raises(ScheduleError, match="bootstrap failed"):
        backend.apply(Schedule(DAILY, "08:00"))


def test_mac_warns_about_a_cron_entry_from_schedule_sh(mac):
    backend, run = mac
    run.answers = [("crontab -l", (0, "0 8 * * * /x/run.sh --all  # carwatch-daily\n", ""))]
    assert any("cron" in note for note in backend.status().notes)


# ---------------------------------------------------------------- elsewhere


def test_other_systems_are_not_offered():
    assert backend_for("linux").status().supported is False
    with pytest.raises(ScheduleError, match="schedule.sh"):
        Unsupported().apply(Schedule(DAILY, "08:00"))


def test_the_right_backend_per_system():
    assert isinstance(backend_for("win32"), TaskScheduler)
    assert isinstance(backend_for("darwin"), Launchd)


# ------------------------------------------------------------------- panel

CONFIG = """
settings:
  db_path: "{db}"
searches:
  - name: "Qashqai"
    sources:
      - site: "autovit"
        url: "https://www.autovit.ro/autoturisme/nissan/qashqai"
"""


class FakeBackend:
    system = "Test scheduler"

    def __init__(self):
        self.current = Schedule()
        self.applied = []
        self.fail = None
        self.asked = 0
        self.under_way = None  # a CurrentRun, when a run is going

    def status(self):
        self.asked += 1
        return ScheduleStatus(
            system=self.system,
            schedule=self.current,
            next_run=datetime(2026, 9, 12, 8, 0) if self.current.is_on else None,
            running=self.under_way is not None,
            current=self.under_way,
        )

    def apply(self, schedule):
        if self.fail:
            raise ScheduleError(self.fail)
        self.applied.append(schedule)
        self.current = schedule


@pytest.fixture
def panel(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(CONFIG).format(db=(tmp_path / "t.db").as_posix()),
        encoding="utf-8",
    )
    client = TestClient(create_app(path))
    backend = FakeBackend()
    client.app.state.schedule_backend = backend
    client.app.state.clock = lambda: NOW
    return client, backend


def test_the_runs_page_loads_the_panel_after_itself(panel):
    client, _ = panel
    body = client.get("/runs").text
    assert 'hx-get="/schedule"' in body


def test_the_panel_says_it_is_off(panel):
    client, _ = panel
    body = client.get("/schedule").text

    assert "Off." in body
    assert 'value="off" checked' in body


def test_saving_registers_the_schedule(panel):
    client, backend = panel
    body = client.post("/schedule", data={"mode": "hours", "at": "08:00", "every_hours": "6"}).text

    assert backend.applied == [Schedule(HOURS, "08:00", 6)]
    assert "Saved" in body
    assert "Every 6 hours (02:00, 08:00, 14:00, 20:00)" in body
    assert "Next run tomorrow between 08:00 and 08:20" in " ".join(body.split())


def test_turning_it_off(panel):
    client, backend = panel
    backend.current = Schedule(DAILY, "08:00")
    body = client.post("/schedule", data={"mode": "off"}).text

    assert backend.applied == [Schedule(OFF)]
    assert "turned off" in body


def test_too_often_is_refused_before_the_scheduler_is_asked(panel):
    client, backend = panel
    body = client.post("/schedule", data={"mode": "hours", "at": "08:00", "every_hours": "2"}).text

    assert backend.applied == []
    assert "Not saved" in body


def test_a_scheduler_refusal_is_shown_not_raised(panel):
    client, backend = panel
    backend.fail = "Access is denied."
    r = client.post("/schedule", data={"mode": "daily", "at": "08:00"})

    assert r.status_code == 200
    assert "Access is denied." in r.text


def test_a_dashboard_on_another_config_is_warned(panel):
    """The scheduled command collects the project's own config.yaml."""
    client, _ = panel
    text = " ".join(client.get("/schedule").text.split())
    assert "not the config file this dashboard was started with" in text


def test_an_unsupported_system_says_so(panel):
    client, _ = panel
    client.app.state.schedule_backend = Unsupported()
    assert "schedule.sh" in client.get("/schedule").text


# ------------------------------------------------ when the next run really is
#
# "Next run 18:50" is only the start of a twenty-minute window: each run picks
# a random minute in it when it starts. The dashboard says the window, and
# once a run has picked its minute, that minute.


def test_the_window_is_the_start_plus_the_random_delay():
    status = ScheduleStatus(
        system="x", schedule=Schedule(DAILY, "18:50"), next_run=datetime(2026, 9, 11, 18, 50)
    )
    assert status.window == (datetime(2026, 9, 11, 18, 50), datetime(2026, 9, 11, 19, 10))


def test_the_top_bar_line_says_the_window():
    status = ScheduleStatus(
        system="x", schedule=Schedule(DAILY, "08:00"), next_run=datetime(2026, 9, 12, 8, 0)
    )
    assert summary(status, NOW) == "Next run tomorrow 08:00–08:20"


def test_the_top_bar_says_nothing_when_there_is_no_schedule():
    assert summary(ScheduleStatus(system="x"), NOW) is None
    assert summary(None, NOW) is None


def test_a_run_under_way_says_its_minute():
    run = CurrentRun(started=datetime(2026, 9, 11, 18, 50), collect_at=datetime(2026, 9, 11, 19, 7))
    status = ScheduleStatus(
        system="x", schedule=Schedule(DAILY, "18:50"), running=True, current=run
    )

    assert summary(status, datetime(2026, 9, 11, 18, 55)) == "Scheduled run collecting at 19:07"
    assert summary(status, datetime(2026, 9, 11, 19, 8)) == "Scheduled run collecting now"


def write_record(root, started, collect_at):
    logs = Path(root) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    # With a BOM, the way Windows PowerShell 5.1's Set-Content -Encoding utf8 writes it.
    (logs / scheduling.CURRENT_RUN_FILE).write_text(
        "﻿" + json.dumps({"started": started, "collect_at": collect_at}),
        encoding="utf-8",
    )


def test_the_wrappers_record_is_read(tmp_path):
    write_record(tmp_path, "2026-09-11T18:50:00", "2026-09-11T19:06:46")
    run = read_current_run(tmp_path, datetime(2026, 9, 11, 18, 55))

    assert run == CurrentRun(datetime(2026, 9, 11, 18, 50), datetime(2026, 9, 11, 19, 6, 46))


def test_a_record_left_by_a_dead_run_is_ignored(tmp_path):
    write_record(tmp_path, "2026-09-11T08:00:00", "2026-09-11T08:10:00")
    assert read_current_run(tmp_path, datetime(2026, 9, 11, 18, 0)) is None


def test_no_record_is_no_run(tmp_path):
    assert read_current_run(tmp_path, NOW) is None


def windows_status(state):
    return json.dumps(
        {"registered": True, "state": state, "at": "18:50", "every_hours": 6,
         "next_run": "2026-09-12T00:50:00", "last_run": "2026-09-11T18:50:00",
         "last_result": 267009}
    )


def test_windows_running_reads_the_wrappers_record(tmp_path):
    write_record(tmp_path, "2026-09-11T18:50:00", "2026-09-11T19:07:00")
    backend = TaskScheduler(
        root=tmp_path,
        run=Runner([("-Status", (0, windows_status("Running"), ""))]),
        now=lambda: datetime(2026, 9, 11, 18, 55),
    )
    status = backend.status()

    assert status.running
    assert status.current.collect_at == datetime(2026, 9, 11, 19, 7)


def test_windows_not_running_ignores_a_leftover_record(tmp_path):
    write_record(tmp_path, "2026-09-11T18:50:00", "2026-09-11T19:07:00")
    backend = TaskScheduler(
        root=tmp_path,
        run=Runner([("-Status", (0, windows_status("Ready"), ""))]),
        now=lambda: datetime(2026, 9, 11, 18, 55),
    )
    status = backend.status()

    assert not status.running
    assert status.current is None


def test_mac_reads_the_wrappers_record(mac):
    backend, _ = mac
    backend.apply(Schedule(DAILY, "15:00"))
    write_record(backend.root, "2026-09-11T15:00:00", "2026-09-11T15:12:00")

    status = backend.status()
    assert status.running
    assert status.current.collect_at == datetime(2026, 9, 11, 15, 12)


def test_every_page_asks_for_the_top_bar_line(panel):
    client, _ = panel
    assert 'hx-get="/schedule/next"' in client.get("/").text


def test_the_top_bar_line(panel):
    client, backend = panel
    assert "Next run" not in client.get("/schedule/next").text  # off: nothing

    client.app.state.schedule_cache = None
    backend.current = Schedule(DAILY, "08:00")
    body = client.get("/schedule/next").text
    assert "Next run tomorrow 08:00–08:20" in body
    assert 'href="/runs"' in body


def test_the_top_bar_does_not_ask_the_scheduler_on_every_page(panel):
    """Asking Task Scheduler starts PowerShell — a second, on every page."""
    client, backend = panel
    client.get("/schedule/next")
    client.get("/schedule/next")
    assert backend.asked == 1


def test_saving_refreshes_the_top_bar(panel):
    client, backend = panel
    client.get("/schedule/next")
    client.post("/schedule", data={"mode": "daily", "at": "08:00"})

    assert "Next run tomorrow 08:00–08:20" in client.get("/schedule/next").text


def test_a_run_under_way_is_shown_and_followed(panel):
    client, backend = panel
    backend.current = Schedule(DAILY, "15:00")
    backend.under_way = CurrentRun(datetime(2026, 9, 11, 15, 0), datetime(2026, 9, 11, 15, 12))

    bar = client.get("/schedule/next").text
    assert "Scheduled run collecting at 15:12" in bar
    assert "every 30s" in bar  # keeps asking while it runs
    assert 'data-countdown="720"' in bar  # 15:00 now, collects 15:12

    panel = client.get("/schedule").text
    panel_text = " ".join(panel.split())
    assert "A scheduled run is under way" in panel_text
    assert "collecting at 15:12" in panel_text
    assert 'data-countdown="720"' in panel


def test_the_countdown_counts_up_once_collecting(panel):
    client, backend = panel
    backend.current = Schedule(DAILY, "14:50")
    backend.under_way = CurrentRun(datetime(2026, 9, 11, 14, 50), datetime(2026, 9, 11, 14, 58))

    assert 'data-countdown="-120"' in client.get("/schedule/next").text


def test_no_countdown_without_a_run_under_way(panel):
    client, backend = panel
    backend.current = Schedule(DAILY, "08:00")
    assert "data-countdown" not in client.get("/schedule/next").text


def test_the_countdown_script_is_on_every_page(panel):
    client, _ = panel
    assert "querySelectorAll('[data-countdown]')" in client.get("/").text


def test_an_old_windowed_task_is_pointed_out():
    answer = json.loads(windows_status("Ready"))
    answer["headless"] = False
    backend = TaskScheduler(
        run=Runner([("-Status", (0, json.dumps(answer), ""))]), now=lambda: NOW
    )
    notes = " ".join(backend.status().notes)
    assert "opens a window" in notes and "Save schedule" in notes


def test_a_headless_task_needs_no_note():
    answer = json.loads(windows_status("Ready"))
    answer["headless"] = True
    backend = TaskScheduler(
        run=Runner([("-Status", (0, json.dumps(answer), ""))]), now=lambda: NOW
    )
    assert backend.status().notes == []
