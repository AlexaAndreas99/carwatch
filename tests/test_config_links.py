"""Pasting a link into the configuration form.

Creating a configuration is links only (2026-09-10). CarWatch builds no URLs,
because a generated one that is wrong does not fail — it widens quietly and
nothing the site returns says so. The form is a name and the links: pasting
one shows what it reads as and suggests a name, and the description itself is
read off the links when saving (tested in test_config_forms.py).
"""

import textwrap

import pytest
from fastapi.testclient import TestClient

from carwatch.web import routes
from carwatch.web.app import create_app

CONFIG = """
settings:
  db_path: "{db}"
searches:
  - name: "Existing"
    sources:
      - site: "autovit"
        url: "https://www.autovit.ro/autoturisme/nissan/qashqai/de-la-2024"
"""

AUTOVIT = "https://www.autovit.ro/autoturisme/nissan/qashqai/de-la-2024"
OLX = (
    "https://www.olx.ro/auto-masini-moto-ambarcatiuni/autoturisme/nissan/"
    "?currency=EUR&search%5Bfilter_float_price:to%5D=30000"
    "&search%5Bfilter_enum_model%5D%5B0%5D=qashqai"
    "&search%5Bfilter_float_year:from%5D=2024"
)
MOBILEDE = (
    "https://www.mobile.de/ro/vehicule/c%C4%83utare.html?isSearchRequest=true"
    "&s=Car&vc=Car&fr=2025&ms=18700%3B47&dt=ALL_WHEEL"
)


@pytest.fixture
def app(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(CONFIG).format(db=(tmp_path / "t.db").as_posix()),
        encoding="utf-8",
    )

    class App:
        client = TestClient(create_app(path))
        config_path = path

        def read(self, site, url, **fields):
            data = {"site": site, f"url_{site}": url}
            data.update(fields)
            return self.client.post("/config/read-link", data=data).text

    return App()


# ------------------------------------------------------------ reading


def test_pasting_a_link_says_what_it_reads_as(app):
    body = app.read("autovit", AUTOVIT)

    assert "Reads as" in body
    assert "make: Nissan" in body
    assert "model: Qashqai" in body
    assert "year min: 2024" in body


def test_pasting_suggests_a_name_when_the_box_is_empty(app):
    body = app.read("autovit", AUTOVIT)

    assert 'id="field_name" value="Nissan Qashqai 2024+"' in body
    assert "name filled in" in body


def test_a_name_you_typed_is_not_overwritten(app):
    body = app.read("autovit", AUTOVIT, name="My own name")

    assert 'id="field_name"' not in body
    assert "name filled in" not in body


def test_no_description_boxes_are_sent_back(app):
    """The form has none — the description is read off the links on save."""
    body = app.read("olx", OLX)
    assert 'id="field_make"' not in body
    assert 'name="currency"' not in body


def test_a_mobilede_link_reads_what_it_states_and_no_names(app):
    """Its make and model are numeric ids, so they are not guessed at."""
    body = app.read("mobilede", MOBILEDE)

    assert "year min: 2025" in body
    assert "drivetrain: 4x4" in body
    assert "make:" not in body


# ------------------------------------------------------------ wrong links


def test_a_link_in_the_wrong_box_says_where_it_belongs(app):
    body = app.read("autovit", OLX)

    assert "paste it in the OLX box" in body
    assert "Reads as" not in body


def test_a_link_from_elsewhere_is_flagged(app):
    body = app.read("autovit", "https://www.ebay.de/sch/cars")
    assert "not from AUTOVIT" in body


def test_an_empty_box_says_nothing(app):
    assert app.read("autovit", "").strip() == ""


def test_an_unknown_site_says_nothing(app):
    assert app.read("ebay", AUTOVIT).strip() == ""


# ---------------------------------------------------------- side effects


def test_reading_a_link_fetches_nothing(app, monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail("reading a link must not ask the site anything")

    monkeypatch.setattr(routes, "get_adapter", refuse)
    assert "Reads as" in app.read("autovit", AUTOVIT)


def test_reading_a_link_writes_nothing(app):
    before = app.config_path.read_text(encoding="utf-8")
    app.read("autovit", AUTOVIT)
    assert app.config_path.read_text(encoding="utf-8") == before


# -------------------------------------------------------------- the form


def test_every_url_box_reads_its_link(app):
    page = app.client.get("/config/new").text
    assert page.count('hx-post="/config/read-link"') == 3


def test_the_form_offers_no_url_building(app):
    """Links only: nothing on the page generates a URL."""
    page = app.client.get("/config/new").text

    assert "/config/build" not in page
    assert "Build the search URLs" not in page
    assert "Start from a link" not in page


def test_the_build_route_is_gone(app):
    assert app.client.post("/config/build", data={}).status_code in (404, 405)
