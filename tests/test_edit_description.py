"""The description on the edit page: shown, never edited.

The form is a name and the links; the description is read off the links when
saving. A detail read wrongly used to be invisible until you opened a
configuration's page — now the edit page shows it, read-only.
"""

import textwrap

import pytest
from fastapi.testclient import TestClient

from carwatch.web.app import create_app

CONFIG = """
settings:
  db_path: "{db}"
searches:
  - name: "Qashqai"
    make: "Nissan"
    trim: "tekna"
    year_min: 2024
    sources:
      - site: "autovit"
        url: "https://www.autovit.ro/autoturisme/nissan/qashqai/de-la-2024/q-tekna"
"""


@pytest.fixture
def client(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(CONFIG).format(db=(tmp_path / "t.db").as_posix()),
        encoding="utf-8",
    )
    return TestClient(create_app(path))


def test_the_edit_page_shows_the_description(client):
    body = client.get("/config/qashqai/edit").text

    assert "make: Nissan" in body
    assert "trim: tekna" in body
    assert "year min: 2024" in body
    assert "Read from the links when you save" in body


def test_the_description_cannot_be_typed_into(client):
    body = client.get("/config/qashqai/edit").text
    for key in ("make", "trim", "year_min"):
        assert f'name="{key}"' not in body


def test_a_blank_new_configuration_shows_no_description(client):
    assert "Read from the links when you save" not in client.get("/config/new").text


def test_a_duplicate_shows_the_description_it_copies(client):
    body = client.get("/config/new?duplicate=qashqai").text
    assert "make: Nissan" in body
