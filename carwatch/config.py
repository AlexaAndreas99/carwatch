"""Load and validate config.yaml (spec §11).

The `sources[].url` values are pasted by the user straight from their browser's
address bar, so validation is deliberately forgiving about their shape — we only
check they're plausible http(s) URLs for the site they claim to be, and that the
PASTE_URL_HERE placeholder has actually been replaced.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

import yaml

PLACEHOLDER = "PASTE_URL_HERE"

# Site key -> hostname fragments we expect in a pasted URL for that site.
KNOWN_SITES: dict[str, tuple[str, ...]] = {
    "autovit": ("autovit.ro",),
    "olx": ("olx.ro",),
    "mobilede": ("mobile.de",),
}

# Currencies the dashboard offers for the display-only `currency` field.
#
# Two, because these are Romanian-market searches: EUR is what car ads quote,
# RON is the alternative if you meant lei. Deliberately *not* enforced by the
# parser — a config naming something else still loads, and the form keeps a
# value it does not recognise rather than quietly rewriting it.
CURRENCIES = ("EUR", "RON")

# Keys treated as display-only metadata and carried into Search.filters_json.
#
# Display-only means exactly that: none of these reaches a `Listing`. A car's
# currency, year and price are whatever the adapter scraped, and the real
# filtering lives in the pasted URLs.
METADATA_KEYS = (
    "make",
    "model",
    "trim",
    "drivetrain",
    "year_min",
    "year_max",
    "price_max",
    "price_min",
    "currency",
)


class ConfigError(Exception):
    """Raised when config.yaml is missing, malformed, or semantically invalid."""


@dataclass
class Settings:
    db_path: str = "./carwatch.db"
    request_delay_seconds: tuple[float, float] = (1.5, 4.0)
    delist_after_missed_runs: int = 1
    user_agent: str = "CarWatch/1.0 (personal use)"

    # Hours to leave a site alone after it blocks us. A site that just said no
    # is the last one that should be asked again on the next run; backing off
    # is both politer and less likely to harden a temporary block into a
    # permanent one. 0 disables the cooldown.
    blocked_cooldown_hours: float = 0.0

    # Per-site overrides, e.g. a slower cadence for a site with many pages:
    #   per_site:
    #     mobilede:
    #       request_delay_seconds: [4.0, 9.0]
    per_site: dict[str, dict] = field(default_factory=dict)

    def for_site(self, site: str) -> "Settings":
        """This Settings with any per-site overrides applied.

        Adapters receive the result, so they never need to know that per-site
        configuration exists.
        """
        overrides = self.per_site.get(site.strip().lower())
        if not overrides:
            return self

        merged = replace(self)
        if "request_delay_seconds" in overrides:
            merged.request_delay_seconds = overrides["request_delay_seconds"]
        if "user_agent" in overrides:
            merged.user_agent = overrides["user_agent"]
        return merged


@dataclass
class Source:
    site: str
    url: str


@dataclass
class SearchConfig:
    name: str
    sources: list[Source]
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def filters_json(self) -> str:
        return json.dumps(self.metadata, ensure_ascii=False, sort_keys=True)


@dataclass
class Config:
    settings: Settings
    searches: list[SearchConfig]
    path: Path

    def enabled_searches(self) -> list[SearchConfig]:
        return [s for s in self.searches if s.enabled]

    def find_search(self, name: str) -> Optional[SearchConfig]:
        target = name.strip().casefold()
        for s in self.searches:
            if s.name.casefold() == target:
                return s
        return None

    @property
    def db_file(self) -> Path:
        """db_path resolved relative to the config file's directory."""
        p = Path(self.settings.db_path).expanduser()
        return p if p.is_absolute() else (self.path.parent / p).resolve()


def _parse_settings(raw: Any) -> Settings:
    if raw is None:
        return Settings()
    if not isinstance(raw, dict):
        raise ConfigError("`settings` must be a mapping.")

    s = Settings()

    if "db_path" in raw:
        s.db_path = str(raw["db_path"])

    if "request_delay_seconds" in raw:
        s.request_delay_seconds = _parse_delay(
            raw["request_delay_seconds"], "settings.request_delay_seconds"
        )

    if "delist_after_missed_runs" in raw:
        try:
            n = int(raw["delist_after_missed_runs"])
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                "`settings.delist_after_missed_runs` must be an integer."
            ) from exc
        if n < 1:
            raise ConfigError("`settings.delist_after_missed_runs` must be >= 1.")
        s.delist_after_missed_runs = n

    if "user_agent" in raw:
        ua = str(raw["user_agent"]).strip()
        if not ua:
            raise ConfigError("`settings.user_agent` must not be empty.")
        s.user_agent = ua

    if "blocked_cooldown_hours" in raw:
        try:
            hours = float(raw["blocked_cooldown_hours"])
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                "`settings.blocked_cooldown_hours` must be a number."
            ) from exc
        if hours < 0:
            raise ConfigError("`settings.blocked_cooldown_hours` must not be negative.")
        s.blocked_cooldown_hours = hours

    if "per_site" in raw:
        s.per_site = _parse_per_site(raw["per_site"])

    return s


def _parse_delay(value: Any, where: str) -> tuple[float, float]:
    """A single number or a [min, max] pair, normalised to an ordered pair."""
    if isinstance(value, (int, float)):
        value = [value, value]
    if not (isinstance(value, (list, tuple)) and len(value) == 2):
        raise ConfigError(f"`{where}` must be a number or a [min, max] pair.")
    try:
        lo, hi = float(value[0]), float(value[1])
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"`{where}` values must be numbers.") from exc
    if lo < 0 or hi < 0:
        raise ConfigError(f"`{where}` must not be negative.")
    return (min(lo, hi), max(lo, hi))


def _parse_per_site(raw: Any) -> dict[str, dict]:
    """Per-site overrides, e.g. a gentler cadence for one site."""
    if not isinstance(raw, dict):
        raise ConfigError("`settings.per_site` must be a mapping of site -> settings.")

    out: dict[str, dict] = {}
    for site, overrides in raw.items():
        key = str(site).strip().lower()
        if key not in KNOWN_SITES:
            known = ", ".join(sorted(KNOWN_SITES))
            raise ConfigError(
                f"`settings.per_site` has unknown site {site!r}. Known sites: {known}."
            )
        if not isinstance(overrides, dict):
            raise ConfigError(f"`settings.per_site.{key}` must be a mapping.")

        parsed: dict[str, Any] = {}
        if "request_delay_seconds" in overrides:
            parsed["request_delay_seconds"] = _parse_delay(
                overrides["request_delay_seconds"],
                f"settings.per_site.{key}.request_delay_seconds",
            )
        if "user_agent" in overrides:
            ua = str(overrides["user_agent"]).strip()
            if not ua:
                raise ConfigError(
                    f"`settings.per_site.{key}.user_agent` must not be empty."
                )
            parsed["user_agent"] = ua

        unknown = set(overrides) - {"request_delay_seconds", "user_agent"}
        if unknown:
            raise ConfigError(
                f"`settings.per_site.{key}` has unsupported key(s): "
                f"{', '.join(sorted(unknown))}. "
                f"Supported: request_delay_seconds, user_agent."
            )
        out[key] = parsed

    return out


def _parse_source(raw: Any, search_name: str, index: int) -> Source:
    where = f"searches[{search_name!r}].sources[{index}]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a mapping with `site` and `url`.")

    site = str(raw.get("site", "")).strip().lower()
    if not site:
        raise ConfigError(f"{where} is missing `site`.")
    if site not in KNOWN_SITES:
        known = ", ".join(sorted(KNOWN_SITES))
        raise ConfigError(f"{where} has unknown site {site!r}. Known sites: {known}.")

    url = str(raw.get("url", "")).strip()
    if not url:
        raise ConfigError(f"{where} is missing `url`.")
    if PLACEHOLDER in url:
        raise ConfigError(
            f"{where} still contains the {PLACEHOLDER} placeholder. "
            f"Paste your {site} search URL from the browser address bar, "
            f"or delete this source block."
        )
    if not url.lower().startswith(("http://", "https://")):
        raise ConfigError(f"{where} url must start with http:// or https:// (got {url!r}).")

    expected = KNOWN_SITES[site]
    if not any(frag in url.lower() for frag in expected):
        raise ConfigError(
            f"{where} is declared as site {site!r} but its url does not point at "
            f"{' or '.join(expected)}: {url!r}"
        )

    return Source(site=site, url=url)


def parse_search(raw: Any, index: int = 0) -> SearchConfig:
    """Validate one raw `searches[]` mapping.

    Public because the config writer validates a draft with it before that
    draft ever reaches a file. Sharing this one function is what keeps the
    dashboard's error messages identical to the ones a hand-edited
    config.yaml produces.
    """
    if not isinstance(raw, dict):
        raise ConfigError(f"searches[{index}] must be a mapping.")

    name = str(raw.get("name", "")).strip()
    if not name:
        raise ConfigError(f"searches[{index}] is missing `name`.")

    raw_sources = raw.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ConfigError(f"searches[{name!r}] needs a non-empty `sources` list.")

    sources = [_parse_source(s, name, i) for i, s in enumerate(raw_sources)]

    seen: set[str] = set()
    for src in sources:
        if src.site in seen:
            raise ConfigError(
                f"searches[{name!r}] lists site {src.site!r} more than once. "
                f"Use one source per site (or split into two searches)."
            )
        seen.add(src.site)

    metadata = {k: raw[k] for k in METADATA_KEYS if k in raw and raw[k] is not None}

    return SearchConfig(
        name=name,
        sources=sources,
        enabled=bool(raw.get("enabled", True)),
        metadata=metadata,
    )


def load_config(path: str | Path = "config.yaml") -> Config:
    """Read, parse and validate config.yaml. Raises ConfigError on any problem."""
    cfg_path = Path(path).expanduser()
    if not cfg_path.is_file():
        raise ConfigError(f"Config file not found: {cfg_path}")

    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        hint = ""
        if "escape sequence" in str(exc):
            # Very common on Windows: db_path: "E:\carwatch\carwatch.db" — inside
            # double quotes YAML reads \c, \U etc. as escape sequences.
            hint = (
                "\nHint: a Windows path inside double quotes needs forward slashes "
                "(\"E:/carwatch/carwatch.db\"), single quotes, or doubled backslashes."
            )
        raise ConfigError(f"{cfg_path} is not valid YAML: {exc}{hint}") from exc

    if raw is None:
        raise ConfigError(f"{cfg_path} is empty.")
    if not isinstance(raw, dict):
        raise ConfigError(f"{cfg_path} must be a YAML mapping at the top level.")

    settings = _parse_settings(raw.get("settings"))

    raw_searches = raw.get("searches")
    if not isinstance(raw_searches, list) or not raw_searches:
        raise ConfigError(f"{cfg_path} needs a non-empty `searches` list.")

    searches = [parse_search(s, i) for i, s in enumerate(raw_searches)]

    names: set[str] = set()
    for s in searches:
        key = s.name.casefold()
        if key in names:
            raise ConfigError(f"Duplicate search name: {s.name!r}. Names must be unique.")
        names.add(key)

    return Config(settings=settings, searches=searches, path=cfg_path.resolve())
