"""Creating, editing, archiving and testing a configuration from the browser
(configurations plan §7).

This is the first part of CarWatch that writes to disk from a request, so these
go end to end: post the form, then read config.yaml back and check both what
changed and what didn't.

`carwatch/config_writer.py` owns the write itself and is tested exhaustively in
`test_config_writer.py`. What is tested here is the wiring — that the form maps
onto a draft, that a refusal comes back as a page rather than a traceback, and
that a saved change actually reaches the running app.
"""

import textwrap

import pytest
from fastapi.testclient import TestClient

from carwatch.adapters.base import AdapterError, BlockedError, RawListing
from carwatch.config import load_config
from carwatch.web import routes
from carwatch.web.app import create_app

LIVE_AUTOVIT = "https://www.autovit.ro/autoturisme/nissan/qashqai/de-la-2024"

CONFIG = """
# A comment at the top of the file, which no save may lose.
settings:
  db_path: "{db}"
  request_delay_seconds: [1.5, 4.0]

searches:
  - name: "Qashqai 2024+"
    make: "Nissan"
    year_min: 2024
    enabled: true
    sources:
      # Why this URL carries no price filter.
      - site: "autovit"
        url: "https://www.autovit.ro/autoturisme/nissan/qashqai/de-la-2024"
"""


@pytest.fixture
def app(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(CONFIG).format(db=(tmp_path / "t.db").as_posix()),
        encoding="utf-8",
    )

    # Saving starts a collection for what you just saved. Real adapters would
    # reach the network from a test, so the runner is silenced; that it was
    # asked to run is asserted separately, below.
    started: list = []

    class Recorder:
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

        def body(self):
            return path.read_text(encoding="utf-8")

        def config(self):
            return load_config(path)

        def stamp(self, url="/config/new"):
            """The hidden stamp field the real form would carry."""
            page = client.get(url).text
            marker = 'name="stamp" value="'
            start = page.index(marker) + len(marker)
            return page[start : page.index('"', start)]

        def save(self, **fields):
            fields.setdefault("stamp", self.stamp())
            fields.setdefault("original_name", "")
            return client.post("/config/save", data=fields, follow_redirects=False)

    return App()


def form(name, **kw):
    """A minimally valid form post."""
    data = {
        "name": name,
        "url_autovit": "https://www.autovit.ro/autoturisme/nissan/juke",
        "enabled": "1",
    }
    data.update(kw)
    return data


# ------------------------------------------------------------------ create


JUKE_2023 = "https://www.autovit.ro/autoturisme/nissan/juke/de-la-2023"
OLX_NISSAN = "https://www.olx.ro/auto-masini-moto-ambarcatiuni/autoturisme/nissan/?"


def test_creating_a_configuration_writes_it_to_the_file(app):
    r = app.save(**form("Nissan Juke 2023+", url_autovit=JUKE_2023))

    assert r.status_code == 303
    assert r.headers["location"] == "/config/nissan-juke-2023"

    created = app.config().find_search("Nissan Juke 2023+")
    assert created is not None
    assert [(s.site, s.url) for s in created.sources] == [("autovit", JUKE_2023)]


def test_creating_keeps_the_comments_in_the_file(app):
    """The guarantee the writer exists for, asserted through the UI too."""
    app.save(**form("Nissan Juke"))

    body = app.body()
    assert "# A comment at the top of the file" in body
    assert "# Why this URL carries no price filter." in body


def test_a_new_configuration_appears_in_the_running_app(app):
    """config.yaml is re-read after a write; without that the sidebar would go
    on showing the list loaded at startup."""
    app.save(**form("Nissan Juke"))
    assert "Nissan Juke" in app.client.get("/").text


def test_saving_starts_a_collection_for_what_was_saved(app):
    """The answer to §11's third question: collect it now, so you find out
    whether the URLs work rather than waiting for the next manual run."""
    app.save(**form("Nissan Juke"))
    assert app.started == [("Nissan Juke", ["Nissan Juke"])]


def test_the_description_is_read_off_the_links(app):
    """The form has no description boxes; what the link says becomes it."""
    app.save(**form("Nissan Juke 2023+", url_autovit=JUKE_2023))

    assert app.config().find_search("Nissan Juke 2023+").metadata == {
        "make": "Nissan",
        "model": "Juke",
        "year_min": 2023,
    }


def test_a_posted_description_field_is_ignored(app):
    """Nothing but the name and the links is written from the form."""
    app.save(**form("Nissan Juke", make="Renault", currency="USD"))
    assert app.config().find_search("Nissan Juke").metadata == {
        "make": "Nissan",
        "model": "Juke",
    }


def test_a_later_link_adds_only_what_the_first_did_not_say(app):
    """autovit's year wins; olx adds the price cap autovit's URL may not carry."""
    app.save(
        **form(
            "Juke",
            url_autovit=JUKE_2023,
            url_olx=OLX_NISSAN
            + "currency=EUR&search%5Bfilter_enum_model%5D%5B0%5D=juke"
            "&search%5Bfilter_float_year:from%5D=2021"
            "&search%5Bfilter_float_price:to%5D=20000",
        )
    )
    assert app.config().find_search("Juke").metadata == {
        "make": "Nissan",
        "model": "Juke",
        "year_min": 2023,
        "price_max": 20000,
        "currency": "EUR",
    }


def test_a_site_left_blank_is_not_a_source(app):
    app.save(**form("Nissan Juke", url_olx="", url_mobilede=""))
    assert [s.site for s in app.config().find_search("Nissan Juke").sources] == ["autovit"]


def test_all_three_sites_can_be_filled_in(app):
    app.save(
        **form(
            "Everything",
            url_olx="https://www.olx.ro/autoturisme/nissan/",
            url_mobilede="https://www.mobile.de/ro/vehicule/cautare.html?s=Car",
        )
    )
    assert sorted(s.site for s in app.config().find_search("Everything").sources) == [
        "autovit",
        "mobilede",
        "olx",
    ]


# -------------------------------------------------------------- refusals


def test_an_invalid_url_comes_back_as_the_form_not_a_traceback(app):
    r = app.save(**form("Broken", url_autovit="https://www.olx.ro/wrong-site"))

    assert r.status_code == 200
    assert "Not saved" in r.text
    assert "does not point at" in r.text
    assert app.config().find_search("Broken") is None


def test_a_refused_save_keeps_what_was_typed(app):
    """Losing a pasted 300-character URL to a validation error would be a small
    betrayal every time it happened."""
    r = app.save(**form("Broken", url_autovit="https://www.olx.ro/wrong"))

    assert 'value="Broken"' in r.text
    assert "https://www.olx.ro/wrong" in r.text


def test_a_configuration_with_no_sources_is_refused(app):
    r = app.save(**form("Sourceless", url_autovit=""))
    assert r.status_code == 200
    assert "non-empty `sources`" in r.text


def test_a_duplicate_name_is_refused(app):
    r = app.save(**form("Qashqai 2024+"))
    assert r.status_code == 200
    assert "already exists" in r.text


def test_a_stale_stamp_is_refused_and_says_why(app):
    """The guard against clobbering an editor window (§4), reached through the
    form that carries the stamp."""
    stale = app.stamp()
    app.path.write_text(app.body() + "\n# hand-edited meanwhile\n", encoding="utf-8")

    r = app.save(stamp=stale, **form("Nissan Juke"))

    assert r.status_code == 200
    assert "changed on disk" in r.text
    assert "# hand-edited meanwhile" in app.body()
    assert app.config().find_search("Nissan Juke") is None


# --------------------------------------------------------------- editing


def test_editing_changes_the_configuration_in_place(app):
    r = app.save(
        original_name="Qashqai 2024+",
        **form(
            "Qashqai 2024+",
            url_autovit="https://www.autovit.ro/autoturisme/nissan/qashqai/de-la-2023",
        ),
    )

    assert r.status_code == 303
    edited = app.config().find_search("Qashqai 2024+")
    # The link changed, so the description was re-read from it.
    assert edited.metadata == {"make": "Nissan", "model": "Qashqai", "year_min": 2023}
    assert "de-la-2023" in edited.sources[0].url
    # The comment above the source it edited is still there.
    assert "# Why this URL carries no price filter." in app.body()


def test_editing_can_rename_and_redirects_to_the_new_slug(app):
    r = app.save(original_name="Qashqai 2024+", **form("Qashqai wide"))

    assert r.headers["location"] == "/config/qashqai-wide"
    assert app.config().find_search("Qashqai 2024+") is None
    assert app.config().find_search("Qashqai wide") is not None


def test_the_edit_form_is_prefilled(app):
    body = app.client.get("/config/qashqai-2024/edit").text
    assert 'value="Qashqai 2024+"' in body
    assert "de-la-2024" in body


def test_renaming_leaves_the_description_exactly_as_it_was(app):
    """Links unchanged, so nothing is re-read — the fixture's description has
    no `model`, and a re-read would have added one."""
    app.save(original_name="Qashqai 2024+", **form("Qashqai wide", url_autovit=LIVE_AUTOVIT))

    assert app.config().find_search("Qashqai wide").metadata == {
        "make": "Nissan",
        "year_min": 2024,
    }


def _hand_edited(app):
    """The fixture's config with a hand-written trim and currency, as seen by a
    fresh app — it reads config.yaml at startup, not on every request."""
    app.path.write_text(
        app.body().replace(
            "year_min: 2024", 'year_min: 2024\n    trim: "Tekna"\n    currency: "USD"'
        ),
        encoding="utf-8",
    )
    client = TestClient(create_app(app.path))
    client.app.state.jobs = app.client.app.state.jobs

    def save(**fields):
        page = client.get("/config/qashqai-2024/edit").text
        marker = 'name="stamp" value="'
        start = page.index(marker) + len(marker)
        fields.setdefault("stamp", page[start : page.index('"', start)])
        fields.setdefault("original_name", "Qashqai 2024+")
        return client.post("/config/save", data=fields, follow_redirects=False)

    return save


def test_saving_unchanged_links_keeps_a_hand_written_description(app):
    save = _hand_edited(app)
    save(**form("Qashqai 2024+", url_autovit=LIVE_AUTOVIT))

    assert app.config().find_search("Qashqai 2024+").metadata == {
        "make": "Nissan",
        "trim": "Tekna",
        "currency": "USD",
        "year_min": 2024,
    }


def test_a_changed_link_keeps_what_no_link_says(app):
    """Re-read from the links, but a trim and currency no link states stay."""
    save = _hand_edited(app)
    save(
        **form(
            "Qashqai 2024+",
            url_autovit="https://www.autovit.ro/autoturisme/nissan/qashqai/de-la-2023",
        )
    )

    meta = app.config().find_search("Qashqai 2024+").metadata
    assert meta["year_min"] == 2023
    assert meta["model"] == "Qashqai"
    assert meta["trim"] == "Tekna"
    assert meta["currency"] == "USD"


def test_editing_an_unknown_configuration_is_a_404(app):
    assert app.client.get("/config/nope/edit").status_code == 404


# ------------------------------------------------------------- duplicate


def test_duplicate_prefills_a_new_configuration(app):
    """How the "same but 2024+" variant came about in the first place (§7)."""
    body = app.client.get("/config/new?duplicate=qashqai-2024").text

    assert 'value="Qashqai 2024+ (copy)"' in body
    assert "de-la-2024" in body
    # A duplicate creates; it must not overwrite what it was copied from.
    assert 'name="original_name" value=""' in body


def test_a_duplicate_keeps_the_description_it_copied(app):
    app.save(duplicate_of="Qashqai 2024+", **form("Qashqai copy", url_autovit=LIVE_AUTOVIT))

    assert app.config().find_search("Qashqai copy").metadata == {
        "make": "Nissan",
        "year_min": 2024,
    }


def test_duplicating_leaves_the_original_alone(app):
    app.save(**form("Qashqai 2024+ (copy)"))

    cfg = app.config()
    assert cfg.find_search("Qashqai 2024+") is not None
    assert cfg.find_search("Qashqai 2024+ (copy)") is not None


# ---------------------------------------------------- archive and restore


def test_archiving_disables_rather_than_deletes(app):
    """§9.5's safety rule, through the button that does it."""
    r = app.client.post("/config/qashqai-2024/archive", follow_redirects=False)

    assert r.status_code == 303
    archived = app.config().find_search("Qashqai 2024+")
    assert archived is not None
    assert archived.enabled is False
    assert len(archived.sources) == 1


# ------------------------------------------------------------------ delete
#
# The database half — which rows go and which stay — is tested against a real
# collection in test_config_delete.py. These are the file half and the wiring.


def _archived_second(app, name="Juke"):
    """A second configuration, archived and ready to delete.

    The fixture's file holds only one, and the last configuration can never
    be deleted.
    """
    app.save(**form(name))
    slug = name.lower()
    app.client.post(f"/config/{slug}/archive", follow_redirects=False)
    return slug


def delete(app, slug, stamp=None):
    if stamp is None:
        stamp = app.stamp(f"/config/{slug}/delete")
    return app.client.post(
        f"/config/{slug}/delete", data={"stamp": stamp}, follow_redirects=False
    )


def test_an_active_configuration_cannot_be_deleted(app):
    """Delete sits behind archive: two decisions, never one click."""
    r = app.client.post("/config/qashqai-2024/delete", follow_redirects=False)

    assert r.status_code == 409
    assert "Archive it first" in r.text
    assert app.config().find_search("Qashqai 2024+") is not None


def test_delete_is_offered_only_once_archived(app):
    assert "/config/qashqai-2024/delete" not in app.client.get("/config/qashqai-2024").text

    app.client.post("/config/qashqai-2024/archive", follow_redirects=False)
    page = app.client.get("/config/qashqai-2024?disabled=1").text
    assert 'href="/config/qashqai-2024/delete"' in page


def test_deleting_an_archived_configuration_removes_it_from_the_file(app):
    slug = _archived_second(app)

    r = delete(app, slug)

    assert r.status_code == 303
    assert app.config().find_search("Juke") is None
    assert app.config().find_search("Qashqai 2024+") is not None
    assert "# A comment at the top of the file" in app.body()
    assert "# Why this URL carries no price filter." in app.body()


def test_deleting_leaves_a_backup_of_the_file(app):
    slug = _archived_second(app)
    before = app.body()

    delete(app, slug)

    backups = sorted(app.path.parent.glob("config.yaml.bak-*"))
    assert backups and backups[-1].read_text(encoding="utf-8") == before


def test_the_last_configuration_cannot_be_deleted(app):
    """`load_config` refuses an empty `searches:` — deleting it would stop
    CarWatch loading at all."""
    app.client.post("/config/qashqai-2024/archive", follow_redirects=False)
    before = app.body()

    page = app.client.get("/config/qashqai-2024/delete").text
    assert "only configuration" in page
    assert 'class="btn-danger"' not in page  # no button that can only fail

    # Posted anyway, the writer refuses it too.
    r = delete(app, "qashqai-2024", stamp=app.stamp())

    assert r.status_code == 409
    assert "only configuration" in r.text
    assert app.body() == before


def test_a_stale_stamp_refuses_the_delete(app):
    slug = _archived_second(app)
    stamp = app.stamp(f"/config/{slug}/delete")
    app.path.write_text(app.body() + "\n# edited in another window\n", encoding="utf-8")

    r = delete(app, slug, stamp=stamp)

    assert r.status_code == 409
    assert "changed on disk" in r.text
    assert app.config().find_search("Juke") is not None


def test_deleting_an_unknown_configuration_is_a_404(app):
    assert app.client.get("/config/nope/delete").status_code == 404
    assert app.client.post("/config/nope/delete", follow_redirects=False).status_code == 404


def test_restoring_switches_it_back_on(app):
    app.client.post("/config/qashqai-2024/archive", follow_redirects=False)
    app.client.post("/config/qashqai-2024/restore", follow_redirects=False)
    assert app.config().find_search("Qashqai 2024+").enabled is True


def test_saving_never_changes_whether_it_collects(app):
    """Archive and Restore say that; the form has no say, even if posted one."""
    app.client.post("/config/qashqai-2024/archive", follow_redirects=False)
    app.save(original_name="Qashqai 2024+", **form("Qashqai 2024+", url_autovit=LIVE_AUTOVIT))

    assert app.config().find_search("Qashqai 2024+").enabled is False


def test_saving_an_archived_configuration_does_not_collect_it(app):
    app.client.post("/config/qashqai-2024/archive", follow_redirects=False)
    app.save(original_name="Qashqai 2024+", **form("Qashqai 2024+", url_autovit=LIVE_AUTOVIT))

    assert app.started == []


def test_a_new_configuration_always_collects(app):
    data = form("Nissan Juke")
    data.pop("enabled")
    app.save(**data)

    assert app.config().find_search("Nissan Juke").enabled is True


# ---------------------------------------------------------------- export


def test_export_returns_a_pasteable_yaml_snippet(app):
    r = app.client.get("/config/qashqai-2024/export")

    assert r.status_code == 200
    assert "Qashqai 2024+" in r.text
    assert "# Why this URL carries no price filter." in r.text
    assert r.headers["content-type"].startswith("text/plain")


def test_exporting_an_unknown_configuration_is_a_404(app):
    assert app.client.get("/config/nope/export").status_code == 404


# ---------------------------------------------------------- test this URL


class Probe:
    """Stands in for a real adapter, so no test reaches the network."""

    def __init__(self, result, relaxation=None):
        self.result = result
        self.last_relaxation = relaxation

    def fetch_listings(self, url, max_pages=None):
        # Recorded so a test can pin that testing a URL asks for one page, not
        # the whole paginated search.
        self.max_pages = max_pages
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def listing(title, price):
    return RawListing(
        site_listing_id=title,
        url="https://www.autovit.ro/anunt/x.html",
        title=title,
        price=price,
        currency="EUR",
        year=2024,
    )


def probe(monkeypatch, result, relaxation=None):
    monkeypatch.setattr(
        routes, "get_adapter", lambda site, settings: Probe(result, relaxation)
    )


def try_url(app, **fields):
    """POST the "Test this URL" fragment. Deliberately not named `test_*`:
    pytest would collect it, and it would run without the stubbed adapter and
    reach the real site."""
    data = {"site": "autovit", "url": "https://www.autovit.ro/autoturisme/nissan"}
    data.update(fields)
    return app.client.post("/config/test-url", data=data).text


def test_testing_a_url_reports_what_it_would_find(app, monkeypatch):
    probe(monkeypatch, [listing("Qashqai one", 25000), listing("Qashqai two", 26000)])

    body = try_url(app)
    assert "Would collect" in body
    assert "Qashqai one" in body
    assert "nothing was saved" in body


def test_testing_a_url_writes_nothing(app, monkeypatch):
    before = app.body()
    probe(monkeypatch, [listing("Qashqai one", 25000)])

    try_url(app)
    assert app.body() == before


def test_a_url_that_matches_nothing_is_reported_loudly(app, monkeypatch):
    """A silent zero is the single commonest way a configuration fails, so it
    is stated as plainly as an error would be."""
    probe(monkeypatch, [])

    body = try_url(app)
    assert "matches <strong>nothing</strong>" in body
    assert "too-narrow" in body


def test_a_relaxed_search_is_reported_as_the_cause_not_the_symptom(app, monkeypatch):
    """The live problem this feature was written for: autovit widens a search
    it would answer with zero, and the URL looks perfectly good."""
    probe(monkeypatch, [], relaxation={"rule": "widened year", "applied": True})

    body = try_url(app)
    assert "relaxed this search" in body
    assert "widened year" in body


def test_a_blocked_site_is_reported_without_failing_the_page(app, monkeypatch):
    probe(monkeypatch, BlockedError("bot wall"))

    body = try_url(app)
    assert "blocked the request" in body
    assert "bot wall" in body


def test_an_adapter_error_is_reported_without_failing_the_page(app, monkeypatch):
    probe(monkeypatch, AdapterError("could not parse"))
    assert "could not parse" in try_url(app)


def test_an_unexpected_error_never_500s_the_page(app, monkeypatch):
    probe(monkeypatch, RuntimeError("something odd"))

    body = try_url(app)
    assert "RuntimeError" in body


def test_testing_the_placeholder_says_so(app):
    assert "placeholder" in try_url(app, url="PASTE_URL_HERE")


def test_testing_an_empty_url_asks_for_one(app):
    assert "Paste a search URL first" in try_url(app, url="")


def test_testing_an_unknown_site_is_refused(app):
    assert "Unknown site" in try_url(app, site="ebay")


def test_the_test_button_finds_the_url_under_its_own_field_name(app, monkeypatch):
    """The form's boxes are named per site (`url_autovit`), because all three
    live in one form — so that is the name `hx-include` posts. Reading a plain
    `url` alone silently answered "paste a search URL first" with the URL
    right there on screen."""
    probe(monkeypatch, [listing("Qashqai one", 25000)])

    body = app.client.post(
        "/config/test-url",
        data={"site": "autovit", "url_autovit": "https://www.autovit.ro/x"},
    ).text
    assert "Would collect" in body


# ------------------------------------------------- robots-disallowed URLs


PRICE_FILTERED = (
    "https://www.autovit.ro/autoturisme/nissan/qashqai"
    "?search%5Bfilter_float_price%3Ato%5D=30000"
)


def test_saving_a_robots_disallowed_url_is_refused(app):
    """autovit's robots.txt disallows `*_price*`. That rule used to live only in
    a comment inside config.yaml — which the form never shows you."""
    r = app.save(**form("Price capped", url_autovit=PRICE_FILTERED))

    assert r.status_code == 200
    assert "Not saved" in r.text
    assert "_price" in r.text
    assert app.config().find_search("Price capped") is None


def test_the_refusal_explains_the_alternative(app):
    r = app.save(**form("Price capped", url_autovit=PRICE_FILTERED))
    assert "filter by price in the dashboard" in r.text


def test_a_refused_url_is_kept_in_the_box(app):
    """You should be able to see and edit what you pasted, not retype it."""
    r = app.save(**form("Price capped", url_autovit=PRICE_FILTERED))
    assert "filter_float_price" in r.text


def test_the_mobilede_search_route_still_saves(app):
    """Its robots.txt disallows it and that was accepted deliberately. A check
    that refused it would break the configuration collecting today."""
    r = app.save(
        **form(
            "German",
            url_autovit="",
            url_mobilede="https://www.mobile.de/ro/vehicule/c%C4%83utare.html?s=Car",
        )
    )
    assert r.status_code == 303
    assert app.config().find_search("German") is not None


def test_the_form_states_each_sites_rules_before_you_paste(app):
    body = app.client.get("/config/new").text

    assert "robots.txt disallows this path" in body   # mobile.de, accepted
    assert "cannot be read" in body                   # olx, 403 to plain HTTP


def test_testing_a_disallowed_url_does_not_fetch_it(app, monkeypatch):
    """The point of a rule about what we may fetch is that we do not fetch it
    to find out. The adapter must never be reached."""
    def explode(site, settings):
        raise AssertionError("the adapter was called for a disallowed URL")

    monkeypatch.setattr(routes, "get_adapter", explode)

    body = try_url(app, url_autovit=PRICE_FILTERED)
    assert "_price" in body
    assert "Nothing was requested" in body


def test_testing_an_allowed_url_still_fetches(app, monkeypatch):
    probe(monkeypatch, [listing("Qashqai one", 25000)])
    assert "Would collect" in try_url(app, url_autovit=LIVE_AUTOVIT)


def test_the_accepted_mobilede_note_does_not_stop_a_fetch(app, monkeypatch):
    probe(monkeypatch, [listing("Qashqai one", 25000)])

    body = try_url(
        app,
        site="mobilede",
        url_mobilede="https://www.mobile.de/ro/vehicule/c%C4%83utare.html?s=Car",
    )
    assert "accepted that" in body
    assert "Would collect" in body


# ------------------------------------------------- the form, and ranges


def test_the_form_is_a_name_and_the_links(app):
    """No description boxes and no collect checkbox (2026-09-10)."""
    body = app.client.get("/config/new").text

    for key in (
        "make", "model", "trim", "drivetrain", "year_min", "year_max",
        "price_min", "price_max", "currency", "enabled",
    ):
        assert f'name="{key}"' not in body
    assert body.count('type="url"') == 3


def test_a_backwards_year_range_in_a_link_comes_back_as_the_form(app):
    r = app.save(
        **form(
            "Backwards",
            url_autovit="",
            url_olx=OLX_NISSAN
            + "search%5Bfilter_float_year:from%5D=2025&search%5Bfilter_float_year:to%5D=2024",
        )
    )

    assert r.status_code == 200
    assert "wrong way round" in r.text
    assert app.config().find_search("Backwards") is None


def test_a_backwards_price_range_in_a_link_comes_back_as_the_form(app):
    r = app.save(
        **form(
            "Backwards",
            url_autovit="",
            url_olx=OLX_NISSAN
            + "search%5Bfilter_float_price:from%5D=30000&search%5Bfilter_float_price:to%5D=20000",
        )
    )
    assert "wrong way round" in r.text


def test_a_slipped_digit_in_a_links_year_is_caught(app):
    r = app.save(
        **form("Typo", url_autovit="https://www.autovit.ro/autoturisme/nissan/juke/de-la-20255")
    )
    assert "not a year" in r.text
    assert "slipped digit" in r.text


def test_a_sane_range_still_saves(app):
    app.save(
        **form(
            "Sane",
            url_autovit="",
            url_olx=OLX_NISSAN
            + "search%5Bfilter_float_year:from%5D=2023&search%5Bfilter_float_year:to%5D=2025"
            "&search%5Bfilter_float_price:to%5D=30000",
        )
    )
    assert app.config().find_search("Sane").metadata == {
        "make": "Nissan",
        "year_min": 2023,
        "year_max": 2025,
        "price_max": 30000,
    }


def test_testing_a_url_asks_for_one_page_not_the_whole_search(app, monkeypatch):
    """`fetch_listings` paginates to its cap — fifty pages on autovit. This
    button answers "does this URL parse and match anything", which one page
    settles; walking the whole search to say so is rude to the site for no
    extra information.
    """
    p = Probe([listing("Qashqai one", 25000)])
    monkeypatch.setattr(routes, "get_adapter", lambda site, settings: p)

    try_url(app, url_autovit=LIVE_AUTOVIT)
    assert p.max_pages == 1
