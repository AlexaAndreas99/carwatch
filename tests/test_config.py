"""Config loader tests — the validation rules that catch a bad config.yaml."""

import textwrap

import pytest

from carwatch.config import ConfigError, load_config

GOOD = """
settings:
  db_path: "./carwatch.db"
  request_delay_seconds: [1.5, 4.0]
  delist_after_missed_runs: 1
  user_agent: "CarWatch/1.0 (personal use)"

searches:
  - name: "Qashqai"
    make: "Nissan"
    year_min: 2025
    enabled: true
    sources:
      - site: "autovit"
        url: "https://www.autovit.ro/autoturisme/nissan/qashqai"
      - site: "olx"
        url: "https://www.olx.ro/auto-masini-moto-ambarcatiuni/autoturisme/nissan/"
"""


def write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(text), encoding="utf-8")
    return p


def test_loads_valid_config(tmp_path):
    cfg = load_config(write(tmp_path, GOOD))

    assert cfg.settings.request_delay_seconds == (1.5, 4.0)
    assert cfg.settings.delist_after_missed_runs == 1
    assert len(cfg.searches) == 1

    search = cfg.searches[0]
    assert search.name == "Qashqai"
    assert [s.site for s in search.sources] == ["autovit", "olx"]
    assert search.metadata == {"make": "Nissan", "year_min": 2025}
    assert cfg.db_file == (tmp_path / "carwatch.db").resolve()


def test_defaults_when_settings_absent(tmp_path):
    cfg = load_config(
        write(
            tmp_path,
            """
            searches:
              - name: "X"
                sources:
                  - site: "autovit"
                    url: "https://www.autovit.ro/autoturisme"
            """,
        )
    )
    assert cfg.settings.db_path == "./carwatch.db"
    assert cfg.settings.delist_after_missed_runs == 1


def test_placeholder_url_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="PASTE_URL_HERE"):
        load_config(
            write(
                tmp_path,
                """
                searches:
                  - name: "X"
                    sources:
                      - site: "autovit"
                        url: "PASTE_URL_HERE"
                """,
            )
        )


def test_url_must_match_declared_site(tmp_path):
    with pytest.raises(ConfigError, match="does not point at"):
        load_config(
            write(
                tmp_path,
                """
                searches:
                  - name: "X"
                    sources:
                      - site: "autovit"
                        url: "https://www.olx.ro/whatever"
                """,
            )
        )


def test_unknown_site_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="unknown site"):
        load_config(
            write(
                tmp_path,
                """
                searches:
                  - name: "X"
                    sources:
                      - site: "craigslist"
                        url: "https://example.com/x"
                """,
            )
        )


def test_duplicate_search_names_rejected(tmp_path):
    with pytest.raises(ConfigError, match="Duplicate search name"):
        load_config(
            write(
                tmp_path,
                """
                searches:
                  - name: "X"
                    sources:
                      - site: "autovit"
                        url: "https://www.autovit.ro/a"
                  - name: "x"
                    sources:
                      - site: "olx"
                        url: "https://www.olx.ro/b"
                """,
            )
        )


def test_duplicate_site_within_search_rejected(tmp_path):
    with pytest.raises(ConfigError, match="more than once"):
        load_config(
            write(
                tmp_path,
                """
                searches:
                  - name: "X"
                    sources:
                      - site: "autovit"
                        url: "https://www.autovit.ro/a"
                      - site: "autovit"
                        url: "https://www.autovit.ro/b"
                """,
            )
        )


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_find_search_is_case_insensitive(tmp_path):
    cfg = load_config(write(tmp_path, GOOD))
    assert cfg.find_search("qashqai") is not None
    assert cfg.find_search("nope") is None


# ------------------------------------------------- a fresh install: no searches


@pytest.mark.parametrize("searches", ["searches: []\n", "searches:\n", ""])
def test_a_config_with_no_searches_loads(tmp_path, searches):
    """A fresh install starts like this; the first search comes from the dashboard."""
    path = write(tmp_path, 'settings:\n  db_path: "./t.db"\n' + searches)

    config = load_config(path)

    assert config.searches == [] and config.enabled_searches() == []


def test_searches_must_still_be_a_list(tmp_path):
    path = write(tmp_path, 'settings:\n  db_path: "./t.db"\nsearches: "Qashqai"\n')

    with pytest.raises(ConfigError, match="must be a list"):
        load_config(path)


def test_the_shipped_example_needs_no_editing():
    """setup copies it to config.yaml, and CarWatch must start on that copy."""
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "config.example.yaml"
    config = load_config(example)

    assert config.searches == []
    assert "PASTE_URL_HERE" not in example.read_text(encoding="utf-8")
