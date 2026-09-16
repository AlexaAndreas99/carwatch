"""Favorites: star a car, and see just those on their own page.

A favourite is keyed on the ad — site and the site's own ad id — rather than a
listing row, so it survives the things rows do not: the same ad under two
configurations, the per-request merge of an autovit ad with olx's copy of it,
and a configuration being deleted.
"""

import textwrap

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from carwatch.adapters.base import RawListing
from carwatch.collector.engine import collect
from carwatch.config import load_config
from carwatch.db import get_engine, purge_configuration, session_scope
from carwatch.models import Favorite, Listing
from carwatch.web.app import create_app

CONFIG = """
settings:
  db_path: "{db}"
  delist_after_missed_runs: 1
searches:
  - name: "Qashqai"
    sources:
      - site: "autovit"
        url: "https://www.autovit.ro/autoturisme/nissan/qashqai"
      - site: "olx"
        url: "https://www.olx.ro/auto-masini-moto-ambarcatiuni/autoturisme/nissan/"
  - name: "Juke"
    sources:
      - site: "autovit"
        url: "https://www.autovit.ro/autoturisme/nissan/juke"
"""


def autovit(ad, title, price=25000):
    return RawListing(
        site_listing_id=ad,
        url=f"https://www.autovit.ro/autoturisme/anunt/nissan-{ad}-ID{ad}.html",
        title=title,
        price=price,
        currency="EUR",
        year=2024,
    )


# olx syndicates autovit ads: its card links to the autovit URL, which is what
# merges the two into one car.
ALPHA_OLX = RawListing(
    site_listing_id="305871621",
    url="https://www.autovit.ro/autoturisme/anunt/nissan-alpha1-IDalpha1.html",
    title="Nissan Alpha (olx copy)",
    price=25000,
    currency="EUR",
    year=2024,
)


class Sites:
    last_relaxation = None

    def __init__(self, by_fragment):
        self.by_fragment = by_fragment

    def __call__(self, site, settings):
        return self

    def fetch_listings(self, url, max_pages=None):
        for fragment, listings in self.by_fragment.items():
            if fragment in url:
                return listings
        return []


@pytest.fixture
def app(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(CONFIG).format(db=(tmp_path / "t.db").as_posix()),
        encoding="utf-8",
    )
    client = TestClient(create_app(path))
    config = load_config(path)

    first = {
        "qashqai": [
            autovit("alpha1", "Nissan Alpha"),
            autovit("bravo2", "Nissan Bravo"),
            autovit("charlie3", "Nissan Charlie"),
        ],
        "olx.ro": [ALPHA_OLX],
        "juke": [autovit("juliet4", "Nissan Juliet")],
    }
    collect(config, adapter_factory=Sites(first))
    # Charlie sells.
    second = dict(first, qashqai=first["qashqai"][:2])
    collect(config, adapter_factory=Sites(second))

    class App:
        def __init__(self):
            self.client = client
            self.db = config.db_file

        def listing_id(self, site, ad):
            with Session(get_engine(self.db)) as session:
                return session.exec(
                    select(Listing.id).where(
                        Listing.site == site, Listing.site_listing_id == ad
                    )
                ).first()

        def star(self, site, ad):
            return client.post(f"/favorite/{self.listing_id(site, ad)}")

        def favorites(self):
            return client.get("/favorites").text

        def rows(self):
            with Session(get_engine(self.db)) as session:
                return session.exec(select(Favorite)).all()

    return App()


# ---------------------------------------------------------------- the page


def test_the_favorites_page_starts_empty(app):
    body = app.favorites()
    assert "No favorites yet" in body


def test_the_nav_links_to_favorites(app):
    assert 'href="/favorites"' in app.client.get("/").text


def test_starring_puts_just_that_car_on_the_page(app):
    r = app.star("autovit", "bravo2")

    assert r.status_code == 200
    assert "fav-toggle on" in r.text

    body = app.favorites()
    assert "Nissan Bravo" in body
    assert "Nissan Alpha" not in body
    assert "Nissan Juliet" not in body


def test_starring_again_unstars(app):
    app.star("autovit", "bravo2")
    r = app.star("autovit", "bravo2")

    assert "fav-toggle on" not in r.text
    assert app.rows() == []
    assert "Nissan Bravo" not in app.favorites()


def test_the_listings_page_shows_which_cars_are_starred(app):
    app.star("autovit", "bravo2")
    body = app.client.get("/").text

    assert body.count("fav-toggle on") == 1
    assert body.count('class="fav-toggle"') >= 2  # the others, unstarred


def test_filters_stay_on_the_favorites_page(app):
    body = app.favorites()
    assert 'hx-get="/favorites"' in body

    r = app.client.get("/favorites?q=bravo", headers={"HX-Request": "true"})
    assert r.headers["HX-Push-Url"].startswith("/favorites")


def test_an_unknown_car_cannot_be_starred(app):
    assert app.client.post("/favorite/999999").status_code == 404


# ------------------------------------------------ what a favourite survives


def test_starring_the_olx_copy_stars_the_merged_car(app):
    """One car, two ads: a star on either is a star on the card."""
    app.star("olx", "305871621")

    body = app.favorites()
    assert "Nissan Alpha" in body
    assert app.client.get("/").text.count("fav-toggle on") == 1


def test_unstarring_from_the_other_copy_clears_it(app):
    app.star("olx", "305871621")
    app.star("autovit", "alpha1")  # the same car, via its other ad

    assert app.rows() == []


def test_a_sold_favorite_stays_on_the_page(app):
    """That a favourite has gone is exactly what you want to know about it."""
    app.star("autovit", "charlie3")
    body = app.favorites()

    assert "Favorites no longer listed" in body
    assert "Nissan Charlie" in body


def test_a_favorite_outlives_its_configurations_rows(app):
    """Deleting a configuration takes its listing rows; the star stays, listed
    by the title it had, with a way to remove it."""
    app.star("autovit", "juliet4")
    with session_scope(app.db) as session:
        purge_configuration(session, "Juke")

    body = app.favorites()
    assert "No longer tracked" in body
    assert "Nissan Juliet" in body

    r = app.client.post(
        "/favorites/forget",
        data={"site": "autovit", "site_listing_id": "juliet4"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Nissan Juliet" not in app.favorites()

