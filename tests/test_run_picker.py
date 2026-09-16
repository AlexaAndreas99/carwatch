"""Choosing what to collect (the caret beside "Run all now").

"Run all now" stays one click because it is what you want almost every time;
the picker is for the times it isn't. Every box in it starts ticked, so opening
it and pressing Run is still a run of everything — it narrows a run, it does not
make you assemble one.
"""

import textwrap

import pytest
from fastapi.testclient import TestClient

from carwatch.config import load_config
from carwatch.web.app import create_app

CONFIG = """
settings:
  db_path: "{db}"
searches:
  - name: "Qashqai 2024+"
    sources:
      - site: "autovit"
        url: "https://www.autovit.ro/autoturisme/nissan/qashqai/de-la-2024"

  - name: "Juke"
    sources:
      - site: "autovit"
        url: "https://www.autovit.ro/autoturisme/nissan/juke"

  - name: "Leaf"
    sources:
      - site: "olx"
        url: "https://www.olx.ro/autoturisme/nissan/"
"""


@pytest.fixture
def app(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(CONFIG).format(db=(tmp_path / "t.db").as_posix()),
        encoding="utf-8",
    )

    started: list = []

    class Recorder:
        """Stands in for the job runner, so no test reaches the network."""

        config = None

        def start(self, label, search_names=None, sites=None):
            started.append((label, search_names))

        def latest(self):
            return None

        def is_busy(self):
            return False

    client = TestClient(create_app(path))
    client.app.state.jobs = Recorder()

    class App:
        def __init__(self):
            self.client = client
            self.path = path
            self.started = started

        def run(self, **data):
            return client.post("/run", data=data)

        def config(self):
            return load_config(path)

        def archive(self, slug):
            """Archive the way the UI does.

            Not through `config_writer` directly: that writes the file but
            leaves this long-lived app serving the configuration list it read
            at startup. The route re-reads and re-syncs, which is the whole
            reason it exists.
            """
            return client.post(f"/config/{slug}/archive", follow_redirects=False)

    return App()


# --------------------------------------------------------------- the picker


def test_the_picker_lists_every_configuration(app):
    body = app.client.get("/run/picker").text

    assert "Qashqai 2024+" in body
    assert "Juke" in body
    assert "Leaf" in body


def test_every_box_starts_ticked(app):
    """The default is all: the picker narrows a run rather than assembling one."""
    body = app.client.get("/run/picker").text
    assert body.count('name="config"') == 3
    assert body.count("checked") == 3


def test_archived_configurations_are_not_offered(app):
    """`collect()` skips them whatever it is asked for, so offering one would be
    offering something that silently does nothing."""
    app.archive("juke")

    body = app.client.get("/run/picker").text
    assert "Qashqai 2024+" in body
    assert ">Juke<" not in body


def test_the_picker_says_so_when_nothing_is_collecting(app):
    for slug in ("qashqai-2024", "juke", "leaf"):
        app.archive(slug)

    body = app.client.get("/run/picker").text
    assert "No configurations are collecting" in body


# ------------------------------------------------------------- what it runs


def test_running_with_no_choice_runs_everything(app):
    """The "Run all now" button posts nothing at all."""
    app.run()
    assert app.started == [("All configurations", None)]


def test_running_a_subset_runs_only_those(app):
    app.run(config=["juke", "leaf"])
    assert app.started == [("Juke + Leaf", ["Juke", "Leaf"])]


def test_running_one_names_it(app):
    app.run(config=["juke"])
    assert app.started == [("Juke", ["Juke"])]


def test_ticking_everything_is_the_same_as_ticking_nothing(app):
    """Passing all three would work, but the status strip should say what it is
    doing in the words the button uses."""
    app.run(config=["qashqai-2024", "juke", "leaf"])
    assert app.started == [("All configurations", None)]


def test_many_configurations_are_counted_rather_than_listed(app):
    """Two names read fine in the status strip; a list of five does not."""
    from carwatch.web.routes import _run_label

    assert _run_label(["A"]) == "A"
    assert _run_label(["A", "B"]) == "A + B"
    assert _run_label(["A", "B", "C"]) == "3 configurations"


def test_a_configuration_can_be_chosen_by_name_as_well_as_slug(app):
    app.run(config=["Juke"])
    assert app.started == [("Juke", ["Juke"])]


def test_an_unknown_configuration_is_a_404(app):
    assert app.client.post("/run", data={"config": ["nope"]}).status_code == 404
    assert app.started == []


def test_a_blank_choice_is_ignored_rather_than_refused(app):
    """An unchecked box sends nothing, but a stray empty value must not 404."""
    app.run(config=[""])
    assert app.started == [("All configurations", None)]


def test_the_configuration_pages_own_button_still_works(app):
    """It posts a single `config`, which arrives as a one-item list."""
    app.run(config="juke")
    assert app.started == [("Juke", ["Juke"])]


def test_duplicate_choices_are_collapsed(app):
    app.run(config=["juke", "Juke"])
    assert app.started == [("Juke", ["Juke"])]
