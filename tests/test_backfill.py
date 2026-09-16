"""Backfilling `image_url` from data the collector already stored.

autovit's `raw_json` is the whole GraphQL node, thumbnail included, so its
existing rows get photos with no re-fetch. olx and mobile.de store only text,
so theirs genuinely need a collection run — the point of these tests is that
the difference is reported honestly rather than silently.
"""

import json
import textwrap

import pytest
from sqlmodel import select

from carwatch.collector.backfill import backfill_image_urls, image_from_raw
from carwatch.config import load_config
from carwatch.db import init_db, session_scope
from carwatch.models import Listing, Search

CONFIG = """
settings:
  db_path: "{db}"
searches:
  - name: "Qashqai"
    sources:
      - site: "autovit"
        url: "https://www.autovit.ro/autoturisme/nissan/qashqai"
"""

X1 = "https://ireland.apollo.olxcdn.com/v1/files/abc-AUTOVITRO/image;s=320x240"
X2 = "https://ireland.apollo.olxcdn.com/v1/files/abc-AUTOVITRO/image;s=640x480"


@pytest.fixture
def config(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        textwrap.dedent(CONFIG).format(db=(tmp_path / "t.db").as_posix()),
        encoding="utf-8",
    )
    cfg = load_config(p)
    init_db(cfg.db_file)
    with session_scope(cfg.db_file) as s:
        s.add(Search(id=1, name="Qashqai", site="autovit", url="https://x"))
    return cfg


def add_listing(config, site, ad_id, raw, image_url=None):
    with session_scope(config.db_file) as s:
        s.add(
            Listing(
                search_id=1,
                site=site,
                site_listing_id=ad_id,
                url=f"https://x/{ad_id}",
                title="Nissan Qashqai",
                image_url=image_url,
                raw_json=json.dumps(raw) if raw is not None else None,
            )
        )


def listings(config):
    with session_scope(config.db_file) as s:
        return {x.site_listing_id: x for x in s.exec(select(Listing)).all()}


# ------------------------------------------------------------ reading a blob


def test_prefers_the_larger_thumbnail():
    assert image_from_raw(json.dumps({"thumbnail": {"x1": X1, "x2": X2}})) == X2


def test_falls_back_to_the_smaller_thumbnail():
    assert image_from_raw(json.dumps({"thumbnail": {"x1": X1}})) == X1


@pytest.mark.parametrize(
    "blob",
    [
        None,
        "",
        "not json at all",
        json.dumps([1, 2, 3]),
        json.dumps({"card_text": "Nissan Qashqai 2025 21 700 km"}),  # olx's blob
        json.dumps({"thumbnail": None}),
        json.dumps({"thumbnail": "https://cdn/x.jpg"}),  # not the expected shape
        json.dumps({"thumbnail": {"x1": "", "x2": ""}}),
    ],
)
def test_blobs_without_a_usable_thumbnail(blob):
    assert image_from_raw(blob) is None


# ---------------------------------------------------------------- the pass


def test_fills_autovit_listings_from_stored_json(config):
    add_listing(config, "autovit", "700", {"thumbnail": {"x1": X1, "x2": X2}})

    with session_scope(config.db_file) as s:
        result = backfill_image_urls(s)

    assert result.filled == {"autovit": 1}
    assert listings(config)["700"].image_url == X2


def test_reports_rows_that_need_a_recollection(config):
    """olx and mobile.de store only text in raw_json, so their photos can only
    come from a fresh run. Say so rather than reporting a silent zero."""
    add_listing(config, "olx", "800", {"card_text": "Nissan Qashqai"})
    add_listing(config, "mobilede", "900", {"attributes": "10 km"})

    with session_scope(config.db_file) as s:
        result = backfill_image_urls(s)

    assert result.filled == {}
    assert result.unavailable == {"olx": 1, "mobilede": 1}
    assert result.total_unavailable == 2


def test_mixed_sites_are_counted_separately(config):
    add_listing(config, "autovit", "700", {"thumbnail": {"x2": X2}})
    add_listing(config, "autovit", "701", {"thumbnail": {"x2": X2}})
    add_listing(config, "olx", "800", {"card_text": "x"})

    with session_scope(config.db_file) as s:
        result = backfill_image_urls(s)

    assert result.filled == {"autovit": 2}
    assert result.unavailable == {"olx": 1}
    assert result.total_filled == 2


def test_a_photo_already_on_the_row_is_left_alone(config):
    """Only NULLs are candidates — the backfill must never overwrite something
    a collection run put there."""
    add_listing(
        config, "autovit", "700", {"thumbnail": {"x2": X2}}, image_url="https://kept/x.jpg"
    )

    with session_scope(config.db_file) as s:
        result = backfill_image_urls(s)

    assert result.filled == {}
    assert result.unavailable == {}
    assert listings(config)["700"].image_url == "https://kept/x.jpg"


def test_running_it_twice_changes_nothing_the_second_time(config):
    add_listing(config, "autovit", "700", {"thumbnail": {"x2": X2}})

    with session_scope(config.db_file) as s:
        backfill_image_urls(s)
    with session_scope(config.db_file) as s:
        second = backfill_image_urls(s)

    assert second.total_filled == 0
    assert listings(config)["700"].image_url == X2


def test_it_touches_nothing_but_the_image_column(config):
    add_listing(config, "autovit", "700", {"thumbnail": {"x2": X2}})
    before = listings(config)["700"]
    snapshot = (before.title, before.price, before.first_seen, before.last_seen,
                before.is_active, before.raw_json)

    with session_scope(config.db_file) as s:
        backfill_image_urls(s)

    after = listings(config)["700"]
    assert (after.title, after.price, after.first_seen, after.last_seen,
            after.is_active, after.raw_json) == snapshot


def test_empty_database_is_a_no_op(config):
    with session_scope(config.db_file) as s:
        result = backfill_image_urls(s)
    assert result.total_filled == 0
    assert result.total_unavailable == 0
