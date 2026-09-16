"""Per-site robots.txt and routing rules (`carwatch/site_rules.py`).

These facts were discovered while building the adapters and lived in prose —
a docstring in `adapters/mobilede.py`, a comment in `config.yaml`. That was
enough while adding a source meant hand-editing the file the comment was in.
The dashboard's form made it possible to paste a URL and save it in one click
having never read the paragraph, so the prose became code.

The distinction the tests care about most: mobile.de's search route *is*
robots-disallowed and was accepted anyway, deliberately, for personal
low-volume use. A checker that treated "robots-disallowed" as "refuse" would
contradict a decision already taken and break a configuration that has been
collecting for a week.
"""

import pytest

from carwatch import site_rules
from carwatch.config import load_config

# The URLs actually in the project's own config.yaml. If a rule ever refuses
# one of these, the rule is wrong — these are what the tool collects today.
LIVE_AUTOVIT = "https://www.autovit.ro/autoturisme/nissan/qashqai/de-la-2024"
LIVE_OLX = (
    "https://www.olx.ro/auto-masini-moto-ambarcatiuni/autoturisme/nissan/"
    "?currency=EUR&search%5Bfilter_float_price:to%5D=30000"
)
LIVE_MOBILEDE = (
    "https://www.mobile.de/ro/vehicule/c%C4%83utare.html?isSearchRequest=true"
    "&s=Car&vc=Car&fr=2025&ms=18700%3B47&tr=AUTOMATIC_GEAR&dt=ALL_WHEEL"
)


def levels(site, url):
    return [n.level for n in site_rules.check_url(site, url)]


# ------------------------------------------------------------------ autovit


def test_a_price_filtered_autovit_url_is_refused():
    """autovit's robots.txt disallows URLs matching `*_price*`, which is why the
    tracked autovit search carries no price cap and config.yaml says so."""
    url = (
        "https://www.autovit.ro/autoturisme/nissan/qashqai"
        "?search%5Bfilter_float_price%3Ato%5D=30000"
    )
    assert site_rules.BLOCKED in levels("autovit", url)
    assert "_price" in site_rules.refusal("autovit", url)


def test_the_refusal_says_what_to_do_instead():
    """Filtering by price in the dashboard costs nothing — it filters cars after
    collection — so the message should say so rather than just refuse."""
    url = "https://www.autovit.ro/autoturisme/nissan?search%5Bfilter_float_price%3Ato%5D=1"
    assert "filter by price in the dashboard" in site_rules.refusal("autovit", url)


def test_an_undecoded_price_filter_is_caught_too():
    """An address bar may hand over either form of the same parameter."""
    url = "https://www.autovit.ro/autoturisme/nissan?search[filter_float_price:to]=30000"
    assert site_rules.blocking("autovit", url)


def test_the_autovit_api_is_refused():
    """`Disallow: /api/`. The adapter reads the search page and parses the JSON
    it embeds precisely so it never goes here."""
    url = "https://www.autovit.ro/api/graphql?q=1"
    assert site_rules.blocking("autovit", url)


def test_an_ordinary_autovit_search_is_allowed():
    assert site_rules.check_url("autovit", LIVE_AUTOVIT) == []


# ----------------------------------------------------------------- mobile.de


def test_the_mobilede_search_route_is_allowed_with_a_note():
    """The route CarWatch uses. Its robots.txt disallows it and the user was
    shown that and accepted it — so this must be a note, never a refusal."""
    notes = site_rules.check_url("mobilede", LIVE_MOBILEDE)

    assert [n.level for n in notes] == [site_rules.NOTE]
    assert "accepted that" in notes[0].summary
    assert site_rules.blocking("mobilede", LIVE_MOBILEDE) == []


@pytest.mark.parametrize(
    "path", ["cautare.html", "c%C4%83utare.html", "căutare.html"]
)
def test_every_spelling_of_the_search_route_is_recognised(path):
    """`căutare.html` arrives percent-encoded from an address bar and plain from
    a hand-typed config. All three are the same route."""
    url = f"https://www.mobile.de/ro/vehicule/{path}?isSearchRequest=true&s=Car"
    assert levels("mobilede", url) == [site_rules.NOTE]


def test_the_german_search_app_is_refused():
    """403 behind Akamai Bot Manager on first contact; §14 forbids working
    around that, so the adapter does not try and neither does the form."""
    url = "https://suchen.mobile.de/fahrzeuge/search.html?isSearchRequest=true"
    assert site_rules.blocking("mobilede", url)
    assert "403" in site_rules.refusal("mobilede", url)


def test_the_seo_category_page_is_refused():
    """It renders listings but exposes no advert id, so every run would report
    the whole page as new as the cards reorder."""
    url = "https://www.mobile.de/ro/automobil/nissan-qashqai/vhc:car,ms1:18700"
    assert site_rules.blocking("mobilede", url)
    assert "no advert ids" in site_rules.refusal("mobilede", url)


def test_an_unrecognised_mobilede_route_warns_without_refusing():
    """We know it is probably wrong; we do not know it is forbidden."""
    url = "https://www.mobile.de/ro/something-new.html?s=Car"
    assert levels("mobilede", url) == [site_rules.WARN]
    assert site_rules.blocking("mobilede", url) == []


# ----------------------------------------------------------------------- olx


def test_olx_says_its_rules_cannot_be_read():
    """olx.ro returns 403 to plain HTTP, `/robots.txt` included. Saying so is
    more honest than silence, which would imply we checked and found nothing.

    It is a standing fact about the site rather than a verdict on a URL, so
    there is nothing to check per-URL.
    """
    assert site_rules.check_url("olx", LIVE_OLX) == []

    notes = site_rules.standing("olx")
    assert [n.level for n in notes] == [site_rules.NOTE]
    assert "cannot be read" in notes[0].summary


def test_every_site_has_standing_rules_to_show():
    """The form renders these against an empty box; a site with none would
    quietly offer no guidance at all."""
    for site in ("autovit", "olx", "mobilede"):
        assert site_rules.standing(site), site


def test_no_standing_rule_blocks_anything():
    """Standing rules are guidance, not verdicts — they are shown beside a box
    that is still empty, so none of them may claim something is refused."""
    for site in ("autovit", "olx", "mobilede"):
        assert not any(n.blocking for n in site_rules.standing(site)), site


def test_an_olx_price_filter_is_not_autovits_problem():
    """olx URLs legitimately carry `filter_float_price`; the rule is autovit's."""
    assert site_rules.blocking("olx", LIVE_OLX) == []


# --------------------------------------------------------------------- edges


@pytest.mark.parametrize("site", ["", "ebay", None])
def test_an_unknown_site_has_no_rules(site):
    assert site_rules.check_url(site, "https://example.com") == []


def test_an_empty_url_has_no_rules():
    assert site_rules.check_url("autovit", "") == []


def test_nothing_here_refuses_the_live_configuration():
    """The strongest guard available: whatever these rules say, they must not
    say it about the URLs CarWatch is collecting from today."""
    config = load_config("config.yaml")

    offenders = [
        (search.name, source.site, site_rules.refusal(source.site, source.url))
        for search in config.searches
        for source in search.sources
        if site_rules.blocking(source.site, source.url)
    ]
    assert offenders == []
