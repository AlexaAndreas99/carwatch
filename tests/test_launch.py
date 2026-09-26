"""The desktop icon's launcher (carwatch.launch)."""

import carwatch.launch as launch


class Recorder:
    def __init__(self):
        self.started = []
        self.opened = []


def rig(monkeypatch, running):
    """`running` is the answers is_running gives, in order; the last repeats."""
    r = Recorder()
    answers = list(running)
    monkeypatch.setattr(
        launch, "is_running", lambda url: answers.pop(0) if len(answers) > 1 else answers[0]
    )
    monkeypatch.setattr(launch, "start", lambda *a: r.started.append(a))
    monkeypatch.setattr(launch.webbrowser, "open", r.opened.append)
    monkeypatch.setattr(launch.time, "sleep", lambda s: None)
    return r


def test_already_running_only_opens_the_browser(monkeypatch):
    r = rig(monkeypatch, [True])
    assert launch.main([]) == 0
    assert r.started == []
    assert r.opened == ["http://127.0.0.1:8009/"]


def test_starts_it_then_opens_the_browser_once_it_answers(monkeypatch):
    r = rig(monkeypatch, [False, False, False, True])
    assert launch.main(["--port", "8019", "--idle-minutes", "3"]) == 0
    assert len(r.started) == 1 and r.started[0][:2] == (8019, 3)
    assert r.opened == ["http://127.0.0.1:8019/"]


def test_says_so_when_it_never_answers(monkeypatch):
    r = rig(monkeypatch, [False])
    told = []
    monkeypatch.setattr(launch, "tell", told.append)
    clock = iter(range(0, 1000, 5))
    monkeypatch.setattr(launch.time, "monotonic", lambda: next(clock))
    assert launch.main(["--no-browser"]) == 1
    assert r.opened == []
    assert "did not start" in told[0] and "dashboard.log" in told[0]


def test_start_runs_the_dashboard_windowless_with_idle_exit(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(launch.subprocess, "Popen", lambda cmd, **kw: calls.append((cmd, kw)))
    launch.start(8009, 5, tmp_path / "d.log")
    cmd, kw = calls[0]
    assert cmd[1:] == [
        "-m", "carwatch.web", "--port", "8009", "--idle-exit", "5", "--log", str(tmp_path / "d.log")
    ]
    assert kw["stdout"] is launch.subprocess.DEVNULL


def test_start_detaches_into_its_own_session_off_windows(monkeypatch, tmp_path):
    """On a Mac the dashboard must outlive CarWatch.app's hidden shell."""
    calls = []
    monkeypatch.setattr(launch.sys, "platform", "darwin")
    monkeypatch.setattr(launch.subprocess, "Popen", lambda cmd, **kw: calls.append(kw))
    launch.start(8009, 5, tmp_path / "d.log")
    assert calls[0]["start_new_session"] is True
    assert "creationflags" not in calls[0]
