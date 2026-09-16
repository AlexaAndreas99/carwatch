"""Reading a pasted search URL back into a description.

The URLs asserted against are the ones in the project's own config.yaml — the
searches that have actually been collecting.

Creating a configuration is links only, so this module reads and never
builds: a generated URL that is wrong does not fail, it widens quietly (see the
module docstring). What these tests hold it to is the other half of that
honesty — a value that was not in the URL must never appear as if it were.
"""

import pytest

from carwatch.search_urls import parse_url, site_of

AUTOVIT_STRICT = (
    "https://www.autovit.ro/autoturisme/nissan/qashqai/de-la-2025/q-tekna"
    "?search%5Bfilter_enum_fuel_type%5D=petrol"
    "&search%5Bfilter_enum_gearbox%5D=automatic"
    "&search%5Bfilter_enum_transmission%5D=all-wheel-auto"
)
AUTOVIT_WIDE = "https://www.autovit.ro/autoturisme/nissan/qashqai/de-la-2024"
OLX_STRICT = (
    "https://www.olx.ro/auto-masini-moto-ambarcatiuni/autoturisme/nissan/"
    "?currency=EUR&search%5Bfilter_float_price:to%5D=30000"
    "&search%5Bfilter_enum_model%5D%5B0%5D=qashqai"
    "&search%5Bfilter_float_year:from%5D=2025"
)
MOBILEDE = (
    "https://www.mobile.de/ro/vehicule/c%C4%83utare.html?isSearchRequest=true"
    "&s=Car&vc=Car&fr=2025&ms=18700%3B47&pw=110%3A147&tr=AUTOMATIC_GEAR"
    "&dt=ALL_WHEEL&ref=dsp"
)


@pytest.mark.parametrize(
    "url, site",
    [
        (AUTOVIT_WIDE, "autovit"),
        (OLX_STRICT, "olx"),
        (MOBILEDE, "mobilede"),
        ("https://www.ebay.de/x", None),
        ("", None),
    ],
)
def test_site_of(url, site):
    assert site_of(url) == site


def test_an_autovit_url_reads_back_into_criteria():
    c = parse_url("autovit", AUTOVIT_STRICT)

    assert c.make == "Nissan"
    assert c.model == "Qashqai"
    assert c.year_min == 2025
    assert c.trim == "tekna"
    assert c.drivetrain == "4x4"


def test_the_simplest_autovit_url_reads_back():
    c = parse_url("autovit", AUTOVIT_WIDE)

    assert (c.make, c.model, c.year_min) == ("Nissan", "Qashqai", 2024)
    assert c.trim is None
    assert c.drivetrain is None


def test_an_unknown_autovit_drivetrain_is_left_blank():
    c = parse_url(
        "autovit",
        AUTOVIT_WIDE + "?search%5Bfilter_enum_transmission%5D=something-new",
    )
    assert c.drivetrain is None


def test_an_olx_url_reads_back_into_criteria():
    c = parse_url("olx", OLX_STRICT)

    assert c.make == "Nissan"
    assert c.model == "Qashqai"
    assert c.year_min == 2025
    assert c.price_max == 30000
    assert c.currency == "EUR"


def test_a_mobilede_url_gives_what_it_states_and_no_names():
    """`ms=18700;47` is Nissan Qashqai in ids. Reading a name off a number
    would be a guess, so make and model stay blank."""
    c = parse_url("mobilede", MOBILEDE)

    assert c.year_min == 2025
    assert c.drivetrain == "4x4"
    assert c.make is None
    assert c.model is None


def test_parsing_invents_nothing():
    """Everything here is display metadata; a value that was not in the URL
    must not appear as though it was."""
    c = parse_url("autovit", "https://www.autovit.ro/autoturisme/dacia")

    assert c.make == "Dacia"
    assert c.model is None
    assert c.year_min is None
    assert c.price_max is None


@pytest.mark.parametrize("site", ["", "ebay", None])
def test_parsing_an_unknown_site_gives_nothing(site):
    assert parse_url(site, AUTOVIT_WIDE).as_metadata() == {}


def test_criteria_become_the_forms_own_fields():
    """Reading a link back must fill the form with nothing left over."""
    from carwatch.config import METADATA_KEYS

    c = parse_url("olx", OLX_STRICT)
    assert set(c.as_metadata()) <= set(METADATA_KEYS)


def test_a_suggested_name_reads_like_one_a_person_would_type():
    # "2025+", not "2025": `de-la-2025` is a floor with no ceiling, and the
    # name should not imply a bound the URL does not have.
    assert (
        parse_url("autovit", AUTOVIT_STRICT).suggested_name()
        == "Nissan Qashqai 2025+ 4x4 tekna"
    )
    assert parse_url("autovit", AUTOVIT_WIDE).suggested_name() == "Nissan Qashqai 2024+"


def test_nothing_here_builds_a_url():
    """Links only. If a builder comes back, it should come back on purpose,
    with the reason it was removed in view."""
    from carwatch import search_urls

    assert not hasattr(search_urls, "build_url")
    assert not hasattr(search_urls, "build_all")
