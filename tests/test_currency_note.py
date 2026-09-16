"""Cars priced in another currency are left out of medians — and now say so.

Every median and range in the dashboard is EUR-only; folding lei into euros
would produce a number that is neither. That was right but silent: a RON-priced
olx ad simply vanished from the figures. These check the pages now name it.
"""

import textwrap

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from carwatch.adapters.base import RawListing
from carwatch.collector.engine import collect
from carwatch.config import load_config
from carwatch.db import get_engine
from carwatch.models import Search
from carwatch.web.app import create_app

CONFIG = """
settings:
  db_path: "{db}"
searches:
  - name: "Mixed"
    sources:
      - site: "autovit"
        url: "https://www.autovit.ro/autoturisme/nissan/qashqai"
"""


def car(ad_id, price, currency):
    return RawListing(
        site_listing_id=ad_id,
        url=f"https://www.autovit.ro/anunt/ID{ad_id}.html",
        title=f"Nissan Qashqai {ad_id}",
        price=price,
        currency=currency,
        year=2024,
    )


class Returns:
    last_relaxation = None

    def __init__(self, listings):
        self.listings = listings

    def __call__(self, site, settings):
        return self

    def fetch_listings(self, url, max_pages=None):
        return self.listings


def make_project(tmp_path, listings):
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(CONFIG).format(db=(tmp_path / "t.db").as_posix()),
        encoding="utf-8",
    )
    client = TestClient(create_app(path))
    config = load_config(path)
    collect(config, adapter_factory=Returns(listings))
    with Session(get_engine(config.db_file)) as session:
        search_id = session.exec(select(Search.id)).first()
    return client, search_id


@pytest.fixture
def mixed(tmp_path):
    return make_project(
        tmp_path,
        [
            car("e1", 20000, "EUR"),
            car("e2", 22000, "EUR"),
            car("e3", 24000, "EUR"),
            car("r1", 110000, "RON"),
        ],
    )


def test_the_listings_page_names_what_it_left_out(mixed):
    client, _ = mixed
    assert "1 priced in RON left out" in client.get("/").text


def test_the_configuration_page_names_what_it_left_out(mixed):
    client, _ = mixed
    assert "1 priced in RON left out" in client.get("/config/mixed").text


def test_the_source_page_names_what_it_left_out(mixed):
    client, search_id = mixed
    assert "1 priced in RON left out" in client.get(f"/search/{search_id}").text


def test_the_source_page_median_no_longer_mixes_currencies(mixed):
    """It used to take the middle of EUR and RON prices together."""
    client, search_id = mixed
    body = client.get(f"/search/{search_id}").text

    assert "110 000" not in body.split("median", 1)[1][:80]


def test_nothing_is_said_when_everything_is_in_euros(tmp_path):
    client, search_id = make_project(
        tmp_path, [car("e1", 20000, "EUR"), car("e2", 22000, "EUR")]
    )
    for url in ("/", "/config/mixed", f"/search/{search_id}"):
        assert "left out" not in client.get(url).text
