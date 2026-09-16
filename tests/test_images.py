"""Tests for the shared card-photo picker.

`image_url_from` is duck-typed on `.attributes`, so most of this runs against
plain stand-in objects — the point is the attribute precedence and the
placeholder rejection, neither of which needs a real parser.
"""

import pytest
from selectolax.parser import HTMLParser

from carwatch.adapters.images import image_url_from, in_block

CDN = "https://frankfurt.apollo.olxcdn.com/v1/files/abc/image;s=320x240"
CDN2 = "https://frankfurt.apollo.olxcdn.com/v1/files/abc/image;s=640x480"


class Img:
    """Minimal stand-in for a parser node."""

    def __init__(self, **attributes):
        self.attributes = attributes


# ------------------------------------------------------------------ src first


def test_reads_src():
    assert image_url_from(Img(src=CDN)) == CDN


def test_src_wins_over_lazy_attributes():
    assert image_url_from(Img(src=CDN, **{"data-src": CDN2})) == CDN


def test_a_sized_srcset_beats_an_oversized_src():
    """mobile.de puts its 1600px variant in `src` and the whole ladder in
    `srcset`. Reading `src` pulled a 1600px photo into a 268px card, 173 times."""
    node = Img(
        src="https://img.classistatic.de/a?rule=mo-1600",
        srcset=(
            "https://img.classistatic.de/a?rule=mo-360 360w, "
            "https://img.classistatic.de/a?rule=mo-640 640w, "
            "https://img.classistatic.de/a?rule=mo-1600 1600w"
        ),
    )
    assert image_url_from(node) == "https://img.classistatic.de/a?rule=mo-640"


def test_falls_back_to_data_src_when_src_is_a_placeholder():
    node = Img(src="data:image/gif;base64,R0lGODlhAQABAAAAACw=", **{"data-src": CDN})
    assert image_url_from(node) == CDN


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "data:image/gif;base64,R0lGODlhAQABAAAAACw=",
        "blob:https://www.olx.ro/9f2c",
        "about:blank",
        # Site-relative: no host to hot-link from, and guessing one per adapter
        # would be wrong as often as right.
        "/img/placeholder.png",
        "img/placeholder.png",
    ],
)
def test_rejects_unusable_values(value):
    assert image_url_from(Img(src=value)) is None


def test_missing_attributes_entirely():
    assert image_url_from(Img()) is None
    assert image_url_from(object()) is None


def test_protocol_relative_url_gets_a_scheme():
    node = Img(src="//frankfurt.apollo.olxcdn.com/v1/files/abc/image")
    assert image_url_from(node) == "https://frankfurt.apollo.olxcdn.com/v1/files/abc/image"


# --------------------------------------------------------------------- srcset


def test_srcset_picks_the_smallest_size_that_fills_the_card():
    """Not the widest: a 268px card at 2x needs ~536px, so 640 is the right
    rung and 1600 is three times the bytes for no visible gain."""
    node = Img(srcset=f"{CDN} 320w, {CDN2} 640w, https://cdn/huge 1600w")
    assert image_url_from(node) == CDN2


def test_srcset_falls_back_to_the_largest_when_nothing_is_big_enough():
    """A slightly soft photo beats no photo."""
    node = Img(srcset=f"{CDN} 200w, {CDN2} 320w")
    assert image_url_from(node) == CDN2


def test_srcset_picks_the_higher_density():
    node = Img(srcset=f"{CDN} 1x, {CDN2} 2x")
    assert image_url_from(node) == CDN2


def test_srcset_entry_without_a_descriptor_loses_to_one_with():
    node = Img(srcset=f"{CDN}, {CDN2} 640w")
    assert image_url_from(node) == CDN2


def test_srcset_alone_still_works():
    assert image_url_from(Img(srcset=f"{CDN} 320w")) == CDN


def test_target_width_is_configurable():
    node = Img(srcset=f"{CDN} 320w, {CDN2} 640w")
    assert image_url_from(node, target_width=300) == CDN


def test_srcset_skips_placeholder_entries():
    node = Img(srcset=f"data:image/gif;base64,AAAA 1x, {CDN} 2x")
    assert image_url_from(node) == CDN


def test_an_unusable_srcset_falls_through_to_src():
    node = Img(srcset="data:image/gif;base64,AAAA 1x", src=CDN)
    assert image_url_from(node) == CDN


def test_empty_srcset_is_none():
    assert image_url_from(Img(srcset="")) is None
    assert image_url_from(Img(srcset="   ,  ")) is None


# ------------------------------------------------------------------- in_block


def _first_img(html, selector="img"):
    return HTMLParser(html).css_first(selector)


def test_in_block_finds_an_ancestor_by_testid_suffix():
    html = (
        '<a><div data-testid="base-result-listing-3-seller-info">'
        '<div><img id="logo" src="x"></div></div></a>'
    )
    assert in_block(_first_img(html), "seller-info") is True


def test_in_block_is_false_outside_that_ancestor():
    html = (
        '<a><div class="gallery"><img id="car" src="x"></div>'
        '<div data-testid="listing-3-seller-info"><img id="logo" src="y"></div></a>'
    )
    assert in_block(_first_img(html, "#car"), "seller-info") is False
    assert in_block(_first_img(html, "#logo"), "seller-info") is True


def test_in_block_walks_past_elements_without_attributes():
    html = '<a data-testid="card-seller-info"><span><em><img src="x"></em></span></a>'
    assert in_block(_first_img(html), "seller-info") is True
