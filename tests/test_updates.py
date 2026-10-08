"""Updating from GitHub inside the dashboard, against real temporary git repos."""

import subprocess
import textwrap
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from carwatch import updates
from carwatch.models import utcnow
from carwatch.web.app import create_app


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def commit(repo: Path, name: str, text: str, message: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repos(tmp_path, monkeypatch):
    """`github` stands in for GitHub; `local` is someone's clone of it."""
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git is not installed")
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    github = tmp_path / "github"
    git(tmp_path, "clone", "-q", str(origin), str(github))
    git(github, "checkout", "-q", "-b", "main")
    commit(github, "requirements.txt", "PyYAML\n", "First")
    git(github, "push", "-q", "-u", "origin", "main")
    local = tmp_path / "local"
    git(tmp_path, "clone", "-q", str(origin), str(local))
    monkeypatch.setattr(updates, "PROJECT_ROOT", local)

    class Repos:
        pass

    r = Repos()
    r.github, r.local = github, local

    def publish(name="app.py", text="print(1)\n", message="Add a feature"):
        commit(github, name, text, message)
        git(github, "push", "-q")

    r.publish = publish
    return r


def test_up_to_date(repos):
    status = updates.check()

    assert not status.available and not status.blocked and not status.error


def test_a_new_commit_on_github_is_offered_with_its_title(repos):
    repos.publish(message="Show the market behind each configuration")

    status = updates.check()

    assert status.behind == 1 and status.available
    assert status.commits[0][1] == "Show the market behind each configuration"
    assert not status.blocked


def test_applying_moves_this_copy_forward(repos, monkeypatch):
    repos.publish()
    pip = []
    real_run = updates._run
    monkeypatch.setattr(updates, "_run", lambda args, **kw: pip.append(args) if "pip" in args else real_run(args, **kw))

    applied = updates.apply()

    assert (repos.local / "app.py").exists()
    assert applied.old != applied.new and not applied.installed_packages
    assert pip == []
    assert updates.check().behind == 0


def test_new_requirements_are_installed(repos, monkeypatch):
    repos.publish("requirements.txt", "PyYAML\nkeyring\n", "Need keyring")
    calls = []
    real_run = updates._run

    def run(args, **kw):
        if "pip" in args:
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        return real_run(args, **kw)

    monkeypatch.setattr(updates, "_run", run)

    applied = updates.apply()

    assert applied.installed_packages
    assert calls and calls[0][-2:] == ["-r", "requirements.txt"]


def test_edited_files_block_the_update_and_are_left_alone(repos):
    """The Mac case: config.py edited by hand. Never overwrite it."""
    repos.publish()
    (repos.local / "requirements.txt").write_text("edited by hand\n", encoding="utf-8")

    status = updates.check()

    assert status.available and "requirements.txt" in status.blocked
    with pytest.raises(updates.UpdateError, match="edited in this copy"):
        updates.apply()
    assert (repos.local / "requirements.txt").read_text(encoding="utf-8") == "edited by hand\n"
    assert not (repos.local / "app.py").exists()


def test_untracked_files_such_as_config_yaml_do_not_block(repos):
    repos.publish()
    (repos.local / "config.yaml").write_text("searches: []\n", encoding="utf-8")

    assert updates.check().blocked == ""
    updates.apply()
    assert (repos.local / "config.yaml").read_text(encoding="utf-8") == "searches: []\n"


def test_a_copy_with_its_own_commits_is_not_forced(repos):
    repos.publish()
    commit(repos.local, "mine.py", "x = 1\n", "My own change")

    status = updates.check()

    assert "commit of its own" in status.blocked
    with pytest.raises(updates.UpdateError):
        updates.apply()


def test_not_a_git_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "PROJECT_ROOT", tmp_path)

    assert "git clone" in updates.unsupported_reason()
    assert "git clone" in updates.check().blocked


def test_a_failed_check_is_retried_within_the_hour():
    now = utcnow()
    ok = updates.Status(checked_at=now - timedelta(hours=2))
    failed = updates.Status(checked_at=now - timedelta(hours=2), error="offline")

    assert updates.due(None, now)
    assert not updates.due(ok, now)
    assert updates.due(failed, now)
    assert updates.due(updates.Status(checked_at=now - timedelta(days=1, minutes=1)), now)


# ------------------------------------------------------------- dashboard


@pytest.fixture
def app(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(f"""
        settings:
          db_path: "{(tmp_path / 't.db').as_posix()}"
        searches: []
        """),
        encoding="utf-8",
    )
    application = create_app(path)
    return application, TestClient(application)


AVAILABLE = updates.Status(
    checked_at=utcnow(), behind=2,
    commits=[("abc1234", "Fit the list view"), ("def5678", "Show the market")],
)


def test_the_runs_page_loads_the_panel_and_badges_the_tab(app, monkeypatch):
    application, client = app
    monkeypatch.setattr(updates, "unsupported_reason", lambda: "")

    assert 'hx-get="/updates"' in client.get("/runs").text
    assert "update-badge" not in client.get("/").text

    application.state.set_update_status(AVAILABLE)

    assert "update-badge" in client.get("/").text
    panel = client.get("/updates").text
    assert "2 updates available." in panel and "Fit the list view" in panel
    assert 'hx-post="/updates/apply"' in panel


def test_a_blocked_update_has_no_button_and_no_badge(app, monkeypatch):
    application, client = app
    monkeypatch.setattr(updates, "unsupported_reason", lambda: "")
    application.state.set_update_status(
        updates.Status(checked_at=utcnow(), behind=1, commits=[("abc1234", "X")],
                       blocked="CarWatch's own files were edited in this copy")
    )

    panel = client.get("/updates").text

    assert "edited in this copy" in panel
    assert 'hx-post="/updates/apply"' not in panel
    assert "update-badge" not in client.get("/").text


def test_updating_is_refused_while_a_collection_runs(app, monkeypatch):
    application, client = app
    monkeypatch.setattr(application.state.jobs, "is_busy", lambda: True)
    monkeypatch.setattr(updates, "apply", lambda: pytest.fail("must not update mid-collection"))

    assert "A collection is running" in client.post("/updates/apply").text


def test_updating_restarts_after_answering(app, monkeypatch):
    application, client = app
    restarted = []
    application.state.restart = lambda: restarted.append(True)
    monkeypatch.setattr(updates, "apply", lambda: updates.Applied(old="aaaaaaa", new="bbbbbbb"))
    monkeypatch.setattr(updates, "check", lambda fetch=True: updates.Status(checked_at=utcnow()))

    page = client.post("/updates/apply").text

    assert "Updated from aaaaaaa to bbbbbbb" in page and "Restarting" in page
    assert restarted == [True]


def test_without_a_restart_it_says_to_reopen(app, monkeypatch):
    _, client = app
    monkeypatch.setattr(updates, "apply", lambda: updates.Applied(old="aaaaaaa", new="bbbbbbb"))
    monkeypatch.setattr(updates, "check", lambda fetch=True: updates.Status(checked_at=utcnow()))

    assert "Close CarWatch and open it again" in client.post("/updates/apply").text


def test_the_running_version_is_served(app):
    application, client = app
    application.state.version = "4e8e864"

    assert client.get("/updates/version").text == "4e8e864"
