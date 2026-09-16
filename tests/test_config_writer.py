"""The config writer (configurations plan §4).

This is the code that can lose the user's work, so the tests lean hard on the
two guarantees that matter: the file's comments survive an edit, and nothing
that fails validation ever reaches config.yaml.

Every test works on a copy of the *real* config.yaml — frozen, as it stood on
2026-09-10 with both Qashqai configurations, in tests/fixtures/real_config.yaml.
A hand-written fixture would be a config with no comments to lose and no
300-character URLs to mangle, which is precisely the file this module is easy
on. Frozen rather than read live, because the dashboard now edits the live file:
deleting a configuration from it made seventeen of these fail overnight.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from carwatch.collector.lock import collection_lock
from carwatch.config import Source, load_config
from carwatch.config_writer import (
    BACKUP_PREFIX,
    KEEP_BACKUPS,
    CollectionRunningError,
    DuplicateNameError,
    FileStamp,
    ImpossibleRangeError,
    InvalidConfigError,
    LastConfigurationError,
    NoSuchSearchError,
    NotArchivedError,
    SearchDraft,
    StaleConfigError,
    archive_search,
    delete_search,
    export_search,
    save_search,
    set_enabled,
)

REAL_CONFIG = Path(__file__).resolve().parent / "fixtures" / "real_config.yaml"

STRICT = "Nissan Qashqai 2025 4x4 Tekna"
WIDER = "Nissan Qashqai 2024+ (any trim)"


@pytest.fixture
def config(tmp_path: Path) -> Path:
    """A copy of the project's own config.yaml, pointed at a scratch database."""
    path = tmp_path / "config.yaml"
    text = REAL_CONFIG.read_text(encoding="utf-8")
    path.write_text(text, encoding="utf-8")
    return path


def draft(name: str = "Test Configuration", **kwargs) -> SearchDraft:
    kwargs.setdefault(
        "sources", [Source("autovit", "https://www.autovit.ro/autoturisme/nissan/juke")]
    )
    return SearchDraft(name=name, **kwargs)


def body(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------- comments survive


def test_adding_a_configuration_keeps_every_comment(config: Path):
    """The guarantee the whole module exists for."""
    before = [line for line in body(config).splitlines() if line.strip().startswith("#")]

    save_search(config, draft())

    after = [line for line in body(config).splitlines() if line.strip().startswith("#")]
    assert after == before


def test_the_mobilede_robots_note_survives_a_write(config: Path):
    """Named explicitly because it is the comment that carries a decision.

    It records that collecting mobile.de goes against its robots.txt and that
    the user accepted that for personal use. Losing it would erase the only
    written trace of that choice.
    """
    save_search(config, draft())
    text = body(config)
    assert "mobile.de's robots.txt disallows this path" in text
    assert "You accepted that risk" in text


def test_editing_a_url_keeps_the_comment_above_it(config: Path):
    """An edited source keeps its explanation, because the node is mutated in place."""
    existing = load_config(config).find_search(WIDER)
    new_url = "https://www.autovit.ro/autoturisme/nissan/qashqai/de-la-2023"

    save_search(
        config,
        SearchDraft(
            name=WIDER,
            enabled=existing.enabled,
            metadata=existing.metadata,
            sources=[
                Source("autovit", new_url),
                *[s for s in existing.sources if s.site != "autovit"],
            ],
        ),
        original_name=WIDER,
    )

    text = body(config)
    assert new_url in text
    assert "autovit's robots.txt disallows" in text
    assert "Filter by price in the dashboard instead." in text


def test_the_worked_example_at_the_bottom_survives(config: Path):
    """The trailing comment block is the one a naive dump loses first."""
    save_search(config, draft())
    assert "PASTE_URL_HERE" in body(config)
    assert "Want to also track the front-wheel-drive Tekna" in body(config)


def test_settings_are_never_touched(config: Path):
    """The UI writes collection targets; pacing stays hand-edited."""
    before = load_config(config).settings
    save_search(config, draft())
    after = load_config(config).settings
    assert after == before
    assert "request_delay_seconds: [5.0, 12.0]" in body(config)


def test_long_urls_are_never_wrapped(config: Path):
    """A folded URL is valid YAML and a misery to copy back out."""
    long_url = (
        "https://www.mobile.de/ro/vehicule/c%C4%83utare.html?isSearchRequest=true"
        "&s=Car&vc=Car&fr=2023&ms=18700%3B47&pw=110%3A147&tr=AUTOMATIC_GEAR"
        "&dt=ALL_WHEEL&ref=dsp&extra=" + "x" * 120
    )
    save_search(config, draft(sources=[Source("mobilede", long_url)]))
    assert long_url in body(config)


# ------------------------------------------------------------------ create


def test_create_adds_a_loadable_configuration(config: Path):
    save_search(
        config,
        draft(
            "Nissan Juke 2023+",
            metadata={"make": "Nissan", "model": "Juke", "year_min": 2023},
        ),
    )

    cfg = load_config(config)
    assert [s.name for s in cfg.searches] == [STRICT, WIDER, "Nissan Juke 2023+"]

    created = cfg.find_search("Nissan Juke 2023+")
    assert created.metadata == {"make": "Nissan", "model": "Juke", "year_min": 2023}
    assert created.enabled is True
    assert [(s.site, s.url) for s in created.sources] == [
        ("autovit", "https://www.autovit.ro/autoturisme/nissan/juke")
    ]


def test_metadata_is_written_above_sources(config: Path):
    """Key order, so a hand-read of the file matches every other block."""
    save_search(config, draft("Ordered", metadata={"make": "Nissan", "year_min": 2024}))

    lines = [line.strip() for line in body(config).splitlines()]
    block = lines[lines.index('- name: "Ordered"') :]
    keys = [line.split(":")[0] for line in block if line and not line.startswith("#")]
    assert keys[:5] == ["- name", "make", "year_min", "enabled", "sources"]


def test_unknown_metadata_keys_are_dropped(config: Path):
    """A stray form field cannot invent a key in the file."""
    save_search(config, draft("Filtered", metadata={"make": "Nissan", "colour": "red"}))
    assert load_config(config).find_search("Filtered").metadata == {"make": "Nissan"}
    assert "colour" not in body(config)


def test_blank_metadata_values_are_dropped(config: Path):
    """An empty form box means "not set", not `make: ""`."""
    save_search(config, draft("Blanks", metadata={"make": "Nissan", "trim": ""}))
    assert load_config(config).find_search("Blanks").metadata == {"make": "Nissan"}


def test_a_duplicate_name_is_refused(config: Path):
    with pytest.raises(DuplicateNameError):
        save_search(config, draft(WIDER))


def test_a_duplicate_name_is_refused_case_insensitively(config: Path):
    """`load_config` compares names casefolded, so this check must too."""
    with pytest.raises(DuplicateNameError):
        save_search(config, draft(WIDER.upper()))


# -------------------------------------------------------------------- edit


def test_edit_replaces_metadata_and_urls(config: Path):
    save_search(
        config,
        SearchDraft(
            name=WIDER,
            metadata={"make": "Nissan", "model": "Qashqai", "year_min": 2023},
            sources=[Source("olx", "https://www.olx.ro/autoturisme/nissan/?edited=1")],
        ),
        original_name=WIDER,
    )

    edited = load_config(config).find_search(WIDER)
    assert edited.metadata["year_min"] == 2023
    assert [(s.site, s.url) for s in edited.sources] == [
        ("olx", "https://www.olx.ro/autoturisme/nissan/?edited=1")
    ]


def test_edit_removes_a_metadata_key_that_is_no_longer_set(config: Path):
    """Clearing a form box must clear the key, not leave the old value behind."""
    assert load_config(config).find_search(STRICT).metadata["trim"] == "Tekna"

    existing = load_config(config).find_search(STRICT)
    metadata = {k: v for k, v in existing.metadata.items() if k != "trim"}
    save_search(
        config,
        SearchDraft(name=STRICT, metadata=metadata, sources=existing.sources),
        original_name=STRICT,
    )

    assert "trim" not in load_config(config).find_search(STRICT).metadata


def test_edit_can_rename(config: Path):
    existing = load_config(config).find_search(WIDER)
    save_search(
        config,
        SearchDraft(name="Renamed", metadata=existing.metadata, sources=existing.sources),
        original_name=WIDER,
    )

    cfg = load_config(config)
    assert cfg.find_search(WIDER) is None
    assert cfg.find_search("Renamed") is not None


def test_editing_an_unknown_configuration_raises(config: Path):
    with pytest.raises(NoSuchSearchError):
        save_search(config, draft("Ghost"), original_name="Ghost")


def test_existing_sources_keep_their_order(config: Path):
    """Comments sit above source blocks; reordering would strand them."""
    existing = load_config(config).find_search(STRICT)
    reversed_sources = list(reversed(existing.sources))

    save_search(
        config,
        SearchDraft(
            name=STRICT, metadata=existing.metadata, sources=reversed_sources
        ),
        original_name=STRICT,
    )

    after = load_config(config).find_search(STRICT)
    assert [s.site for s in after.sources] == [s.site for s in existing.sources]


def test_a_removed_source_is_dropped_from_the_file(config: Path):
    existing = load_config(config).find_search(STRICT)
    keep = [s for s in existing.sources if s.site != "mobilede"]

    save_search(
        config,
        SearchDraft(name=STRICT, metadata=existing.metadata, sources=keep),
        original_name=STRICT,
    )

    after = load_config(config).find_search(STRICT)
    assert [s.site for s in after.sources] == ["autovit", "olx"]


# ------------------------------------------------------------------ enable


def test_archive_disables_rather_than_deletes(config: Path):
    """§9.5's safety rule: a configuration's price history must survive removal."""
    archive_search(config, WIDER)

    cfg = load_config(config)
    archived = cfg.find_search(WIDER)
    assert archived is not None
    assert archived.enabled is False
    assert len(archived.sources) == 2
    assert [s.name for s in cfg.enabled_searches()] == [STRICT]


def test_an_archived_configuration_can_be_switched_back_on(config: Path):
    archive_search(config, WIDER)
    set_enabled(config, WIDER, True)
    assert load_config(config).find_search(WIDER).enabled is True


# ------------------------------------------------------------------- delete
#
# Only the file half. Which database rows go is test_config_delete.py's.


def test_only_an_archived_configuration_can_be_deleted(config: Path):
    original = body(config)

    with pytest.raises(NotArchivedError, match="Archive it first"):
        delete_search(config, WIDER)
    assert body(config) == original


def test_deleting_removes_the_block(config: Path):
    archive_search(config, WIDER)
    delete_search(config, WIDER)

    assert [s.name for s in load_config(config).searches] == [STRICT]


def test_deleting_the_last_block_keeps_the_worked_example_below_it(config: Path):
    """ruamel stores the footer on the last block's last line — a naive delete
    takes the worked example with it."""
    archive_search(config, WIDER)
    delete_search(config, WIDER)

    text = body(config)
    assert "Want to also track the front-wheel-drive Tekna" in text
    assert '#         url: "PASTE_URL_HERE"' in text


def test_deleting_a_block_takes_its_own_comments_with_it(config: Path):
    archive_search(config, WIDER)
    delete_search(config, WIDER)

    text = body(config)
    # Its heading, stored on the block above, and the note inside it.
    assert "Wider comparison search" not in text
    assert "No price filter in this URL on purpose" not in text


def test_deleting_the_first_block_keeps_the_next_ones_heading(config: Path):
    """The heading of the second block is stored on the first — so deleting the
    first must hand it on, not drop it."""
    archive_search(config, STRICT)
    delete_search(config, STRICT)

    text = body(config)
    assert "Wider comparison search" in text
    assert text.index("Wider comparison search") < text.index(WIDER)
    assert "No price filter in this URL on purpose" in text
    assert "AUTOVIT — build/test this adapter first" not in text
    assert [s.name for s in load_config(config).searches] == [WIDER]


def test_an_end_of_line_comment_above_a_deleted_block_survives(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """settings:
  db_path: "./t.db"
searches:
  - name: "A"
    enabled: true
    sources:
      - site: "autovit"
        url: "https://www.autovit.ro/autoturisme/nissan/juke"   # keep me

  # Heading of B.
  - name: "B"
    enabled: false
    sources:
      - site: "autovit"
        url: "https://www.autovit.ro/autoturisme/dacia"

# Footer.
""",
        encoding="utf-8",
    )

    delete_search(path, "B")

    text = body(path)
    assert "# keep me" in text
    assert "# Footer." in text
    assert "Heading of B" not in text
    assert [s.name for s in load_config(path).searches] == ["A"]


def test_the_last_configuration_cannot_be_deleted(tmp_path: Path):
    """`load_config` refuses an empty `searches:` list."""
    path = tmp_path / "config.yaml"
    path.write_text(
        """settings:
  db_path: "./t.db"
searches:
  - name: "Only"
    enabled: false
    sources:
      - site: "autovit"
        url: "https://www.autovit.ro/autoturisme/dacia"
""",
        encoding="utf-8",
    )
    original = body(path)

    with pytest.raises(LastConfigurationError, match="only configuration"):
        delete_search(path, "Only")
    assert body(path) == original


def test_deleting_an_unknown_configuration_raises(config: Path):
    with pytest.raises(NoSuchSearchError):
        delete_search(config, "Nope")


def test_a_delete_is_refused_while_a_collection_is_running(config: Path, tmp_path: Path):
    db_path = tmp_path / "carwatch.db"
    archive_search(config, WIDER, db_path=db_path)
    original = body(config)

    with collection_lock(db_path, source="test"):
        with pytest.raises(CollectionRunningError, match="via test"):
            delete_search(config, WIDER, db_path=db_path)

    assert body(config) == original


def test_a_delete_leaves_a_backup(config: Path):
    archive_search(config, WIDER)
    before = body(config)

    delete_search(config, WIDER)

    newest = sorted(config.parent.glob(config.name + BACKUP_PREFIX + "*"))[-1]
    assert newest.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------- validation


def test_a_placeholder_url_is_refused(config: Path):
    original = body(config)
    with pytest.raises(InvalidConfigError, match="PASTE_URL_HERE"):
        save_search(config, draft(sources=[Source("autovit", "PASTE_URL_HERE")]))
    assert body(config) == original


def test_a_url_for_the_wrong_site_is_refused(config: Path):
    with pytest.raises(InvalidConfigError, match="does not point at"):
        save_search(config, draft(sources=[Source("autovit", "https://www.olx.ro/x")]))


def test_an_unknown_site_is_refused(config: Path):
    with pytest.raises(InvalidConfigError, match="unknown site"):
        save_search(config, draft(sources=[Source("ebay", "https://ebay.de/x")]))


def test_a_configuration_with_no_sources_is_refused(config: Path):
    with pytest.raises(InvalidConfigError, match="non-empty `sources`"):
        save_search(config, draft(sources=[]))


def test_a_nameless_configuration_is_refused(config: Path):
    with pytest.raises(InvalidConfigError, match="missing `name`"):
        save_search(config, draft("   "))


def test_the_same_site_twice_is_refused(config: Path):
    with pytest.raises(InvalidConfigError, match="more than once"):
        save_search(
            config,
            draft(
                sources=[
                    Source("autovit", "https://www.autovit.ro/a"),
                    Source("autovit", "https://www.autovit.ro/b"),
                ]
            ),
        )


def test_a_refused_write_leaves_no_temporary_file(config: Path):
    with pytest.raises(InvalidConfigError):
        save_search(config, draft(sources=[Source("autovit", "not-a-url")]))
    assert list(config.parent.glob("*.tmp-write")) == []


def test_a_refused_write_makes_no_backup(config: Path):
    """Backups mark real writes; a rejected edit is not one."""
    with pytest.raises(InvalidConfigError):
        save_search(config, draft(sources=[Source("autovit", "not-a-url")]))
    assert list(config.parent.glob("config.yaml" + BACKUP_PREFIX + "*")) == []


# ------------------------------------------------------------ concurrency


def test_a_stale_stamp_refuses_the_write(config: Path):
    """The guard against clobbering an editor window."""
    stamp = FileStamp.of(config)

    time.sleep(0.01)
    config.write_text(body(config) + "\n# edited by hand\n", encoding="utf-8")

    with pytest.raises(StaleConfigError):
        save_search(config, draft(), expected=stamp)

    assert "# edited by hand" in body(config)
    assert load_config(config).find_search("Test Configuration") is None


def test_a_current_stamp_allows_the_write(config: Path):
    save_search(config, draft(), expected=FileStamp.of(config))
    assert load_config(config).find_search("Test Configuration") is not None


def test_a_stamp_round_trips_through_its_token(config: Path):
    stamp = FileStamp.of(config)
    assert FileStamp.parse(stamp.token) == stamp


@pytest.mark.parametrize("token", ["", None, "garbage", "12-", "a-b"])
def test_an_unusable_token_means_do_not_check(token):
    """A missing stamp is a caller without one, not a caller to refuse."""
    assert FileStamp.parse(token) is None


def test_a_write_is_refused_while_a_collection_is_running(config: Path, tmp_path: Path):
    """The collector loads config at the start of a run; rewriting it underneath
    a run in flight is how you get a half-applied change."""
    db_path = tmp_path / "carwatch.db"
    original = body(config)

    with collection_lock(db_path, source="test"):
        with pytest.raises(CollectionRunningError, match="via test"):
            save_search(config, draft(), db_path=db_path)

    assert body(config) == original
    save_search(config, draft(), db_path=db_path)  # and again once it has finished


def test_a_stale_lock_does_not_block_a_write(config: Path, tmp_path: Path):
    """A collection killed mid-run must not leave the config unwritable forever."""
    db_path = tmp_path / "carwatch.db"
    lock = db_path.with_suffix(".lock")
    lock.write_text(
        json.dumps({"pid": 1, "source": "cli", "epoch": time.time() - 10 * 3600}),
        encoding="utf-8",
    )

    save_search(config, draft(), db_path=db_path)
    assert load_config(config).find_search("Test Configuration") is not None


# --------------------------------------------------------------- backups


def test_every_write_leaves_a_backup_of_what_was_there_before(config: Path):
    original = body(config)
    save_search(config, draft())

    backups = list(config.parent.glob("config.yaml" + BACKUP_PREFIX + "*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original


def test_backups_are_pruned_to_the_most_recent_few(config: Path):
    for index in range(KEEP_BACKUPS + 4):
        save_search(config, draft(f"Config {index}"))

    backups = list(config.parent.glob("config.yaml" + BACKUP_PREFIX + "*"))
    assert len(backups) == KEEP_BACKUPS


def test_writes_inside_one_second_do_not_share_a_backup(config: Path):
    save_search(config, draft("One"))
    save_search(config, draft("Two"))

    backups = list(config.parent.glob("config.yaml" + BACKUP_PREFIX + "*"))
    assert len(backups) == 2


# ---------------------------------------------------------------- export


def test_export_returns_a_loadable_snippet_with_its_comments(config: Path, tmp_path: Path):
    snippet = export_search(config, STRICT)

    assert "mobile.de's robots.txt disallows this path" in snippet
    assert STRICT in snippet

    # The snippet's real test: it must be a config file in its own right.
    elsewhere = tmp_path / "exported.yaml"
    elsewhere.write_text(snippet, encoding="utf-8")
    assert [s.name for s in load_config(elsewhere).searches] == [STRICT]


def test_exporting_an_unknown_configuration_raises(config: Path):
    with pytest.raises(NoSuchSearchError):
        export_search(config, "Ghost")


# ------------------------------------------------------------ round-tripping


def test_a_no_op_edit_leaves_the_file_byte_identical(config: Path):
    """Round-trip fidelity, stated as strongly as it can be.

    Saving a configuration back exactly as it was read must produce the same
    bytes. Anything less means every save quietly reformats the file, and the
    reformatting is where comments go missing.
    """
    before = body(config)
    existing = load_config(config).find_search(STRICT)

    save_search(config, SearchDraft.from_config(existing), original_name=STRICT)

    assert body(config) == before


def test_repeated_writes_do_not_drift(config: Path):
    """Ten writes must cost the file nothing but the blocks they add.

    Round-trip serialisers can lose a blank line or re-indent a nested list on
    each pass; individually invisible, and after a fortnight of edits the file
    no longer looks like the one the user wrote.
    """
    comments_before = [
        line for line in body(config).splitlines() if line.strip().startswith("#")
    ]

    for index in range(10):
        save_search(config, draft(f"Drift {index}"))
        set_enabled(config, f"Drift {index}", False)

    comments_after = [
        line for line in body(config).splitlines() if line.strip().startswith("#")
    ]
    assert comments_after == comments_before

    cfg = load_config(config)
    assert [s.name for s in cfg.searches] == [
        STRICT,
        WIDER,
        *[f"Drift {i}" for i in range(10)],
    ]


# ------------------------------------------------------- impossible ranges


def test_a_backwards_year_range_is_refused(config: Path):
    """Display-only, so it collects fine — it just renders a pair of chips that
    cannot describe anything, which is worse than an error."""
    with pytest.raises(ImpossibleRangeError, match="wrong way round"):
        save_search(config, draft("Backwards", metadata={"year_min": 2025, "year_max": 2024}))


def test_a_backwards_price_range_is_refused(config: Path):
    with pytest.raises(ImpossibleRangeError, match="wrong way round"):
        save_search(
            config, draft("Backwards", metadata={"price_min": 30000, "price_max": 20000})
        )


def test_an_equal_range_is_fine(config: Path):
    """`year_min: 2025, year_max: 2025` is the shipped strict search."""
    save_search(config, draft("Exact", metadata={"year_min": 2025, "year_max": 2025}))
    assert load_config(config).find_search("Exact").metadata["year_min"] == 2025


def test_half_a_range_is_fine(config: Path):
    """A floor with no ceiling is the commonest shape here — "2024 or newer"."""
    save_search(config, draft("Open ended", metadata={"year_min": 2024}))
    assert load_config(config).find_search("Open ended").metadata == {"year_min": 2024}


def test_a_negative_price_is_refused(config: Path):
    with pytest.raises(ImpossibleRangeError, match="cannot be negative"):
        save_search(config, draft("Negative", metadata={"price_max": -100}))


@pytest.mark.parametrize("year", [202, 20255, 0])
def test_a_slipped_digit_in_a_year_is_refused(config: Path, year):
    with pytest.raises(ImpossibleRangeError, match="not a year"):
        save_search(config, draft("Typo", metadata={"year_min": year}))


def test_an_impossible_range_writes_nothing(config: Path):
    original = body(config)
    with pytest.raises(ImpossibleRangeError):
        save_search(config, draft("Backwards", metadata={"year_min": 2025, "year_max": 2024}))
    assert body(config) == original


def test_ranges_are_not_checked_when_loading_a_config(config: Path, tmp_path: Path):
    """The same rule the site checks follow: this must never stop an existing
    config.yaml loading. These fields are display-only, so a file with them
    backwards still collects — it is only worth catching where we write it."""
    hand_edited = tmp_path / "hand.yaml"
    hand_edited.write_text(
        body(config).replace('year_min: 2024', 'year_min: 2024\n    year_max: 2020'),
        encoding="utf-8",
    )
    assert load_config(hand_edited).find_search(WIDER).metadata["year_max"] == 2020
