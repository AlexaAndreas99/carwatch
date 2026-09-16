"""Rewrite config.yaml's `searches:` list without losing the file around it.

config.yaml is the single source of truth for what CarWatch collects, and the
dashboard now edits it. That makes this module the riskiest code in the
project: a bad write breaks every future collection, and a careless one
silently deletes the user's own notes.

Two things drive the design.

**Comments are most of the file.** 76 of config.yaml's 125 lines are comments —
the robots.txt notes on mobile.de, why the autovit URL carries no price filter,
the worked example at the bottom. `yaml.safe_dump` discards all of it, so this
module uses ruamel.yaml in round-trip mode and mutates nodes *in place*: an
edited URL keeps the paragraph explaining it.

**Nothing half-written may reach config.yaml.** Every write goes: guard against
a concurrent hand-edit, refuse while a collection is running, mutate the
round-tripped document, serialise to a temporary file, load that file back
through the real validator, back up the current file, and only then
`os.replace` it into position. A config the app cannot parse never becomes
config.yaml, and a crash mid-write cannot truncate it.

`settings:` is deliberately out of reach. The dashboard writes collection
targets; pacing and politeness stay hand-edited, which keeps the blast radius
of a UI bug to the part of the file the UI is meant to own.
"""

from __future__ import annotations

import io
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import DoubleQuotedScalarString as DQ

from carwatch import site_rules
from carwatch.collector.lock import active_holder
from carwatch.config import (
    METADATA_KEYS,
    Config,
    ConfigError,
    SearchConfig,
    Source,
    load_config,
    parse_search,
)

# How many `config.yaml.bak-*` files to keep. Enough to walk back from a bad
# afternoon, few enough not to litter the project directory.
KEEP_BACKUPS = 8

BACKUP_PREFIX = ".bak-"

# Years a car can plausibly have been first registered in. Wide on purpose —
# the point is to catch a slipped digit (20255, 202) rather than to have an
# opinion about classic cars.
YEAR_RANGE = (1900, 2100)

# From/to pairs that must not be the wrong way round. Display-only metadata, so
# an inverted pair breaks nothing — it just describes an empty set while
# looking like a filter, which is worse than an error.
RANGE_PAIRS = (
    ("year_min", "year_max", "year"),
    ("price_min", "price_max", "price"),
)


# The order keys are written in when this module *adds* one. Existing keys are
# never reordered — reordering is how you lose the comment attached to the line
# below the one you moved.
FIELD_ORDER = (
    "name",
    "make",
    "model",
    "trim",
    "drivetrain",
    "year_min",
    "year_max",
    "price_min",
    "price_max",
    "currency",
    "enabled",
    "sources",
)


# ------------------------------------------------------------------- errors


class ConfigWriteError(Exception):
    """A write to config.yaml was refused. The message is shown to the user."""


class StaleConfigError(ConfigWriteError):
    """config.yaml changed on disk after the form that is now saving was rendered."""


class CollectionRunningError(ConfigWriteError):
    """A collection holds the lock; rewriting its config underneath it is not safe."""


class InvalidConfigError(ConfigWriteError):
    """The edit would produce a config.yaml the app cannot load."""


class NoSuchSearchError(ConfigWriteError):
    """No configuration by that name exists in the file."""


class DuplicateNameError(ConfigWriteError):
    """Another configuration already uses that name."""


class ImpossibleRangeError(ConfigWriteError):
    """A from/to pair that cannot describe anything, or a value out of range."""


class DisallowedUrlError(ConfigWriteError):
    """A source URL CarWatch will not fetch — see `carwatch/site_rules.py`.

    Separate from `InvalidConfigError` because the URL is perfectly well formed;
    what is wrong is that we should not be asking the site for it.
    """


class NotArchivedError(ConfigWriteError):
    """Only an archived configuration can be deleted.

    Delete sits behind archive on purpose: two decisions on two pages, so no
    single stray click can destroy a configuration's price history.
    """


class LastConfigurationError(ConfigWriteError):
    """config.yaml must keep at least one configuration.

    `load_config` refuses an empty `searches:` list, and a file the app cannot
    load stops every collection — so the delete that would produce one is
    refused instead.
    """


# -------------------------------------------------------------- file stamp


@dataclass(frozen=True)
class FileStamp:
    """What config.yaml looked like when an edit form was rendered.

    Carried through the form as an opaque token and checked on save. Without it
    the dashboard silently clobbers an editor window that happened to be open
    on the same file — the failure mode with no error message and no way back.

    mtime *and* size, because a filesystem with coarse mtime resolution can
    give an edit the same timestamp as the read that preceded it.
    """

    mtime_ns: int
    size: int

    @classmethod
    def of(cls, path: str | Path) -> "FileStamp":
        st = Path(path).stat()
        return cls(mtime_ns=st.st_mtime_ns, size=st.st_size)

    @property
    def token(self) -> str:
        return f"{self.mtime_ns}-{self.size}"

    @classmethod
    def parse(cls, token: str | None) -> Optional["FileStamp"]:
        """A token back into a stamp, or None if it is missing or unusable.

        None means "don't check" — a caller with no stamp (a CLI, a test) is
        not the case this guard exists for.
        """
        if not token:
            return None
        try:
            mtime_ns, size = str(token).split("-", 1)
            return cls(mtime_ns=int(mtime_ns), size=int(size))
        except (TypeError, ValueError):
            return None


# ---------------------------------------------------------------- the draft


@dataclass
class SearchDraft:
    """One configuration as the UI wants it written.

    `metadata` holds only `config.METADATA_KEYS`; anything else is dropped
    rather than written, so a stray form field cannot invent a key in the file.
    """

    name: str
    sources: list[Source] = field(default_factory=list)
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.name = str(self.name).strip()
        self.sources = [
            s
            if isinstance(s, Source)
            else Source(site=str(s[0]).strip().lower(), url=str(s[1]).strip())
            for s in self.sources
        ]
        self.metadata = {
            k: v
            for k, v in self.metadata.items()
            if k in METADATA_KEYS and v is not None and v != ""
        }

    @classmethod
    def from_config(cls, search: SearchConfig) -> "SearchDraft":
        """A draft pre-filled from an existing configuration — edit and duplicate."""
        return cls(
            name=search.name,
            sources=[Source(site=s.site, url=s.url) for s in search.sources],
            enabled=search.enabled,
            metadata=dict(search.metadata),
        )

    def as_raw(self) -> dict:
        """This draft as the plain mapping `config.parse_search` validates."""
        raw: dict[str, Any] = {"name": self.name}
        raw.update(self.metadata)
        raw["enabled"] = self.enabled
        raw["sources"] = [{"site": s.site, "url": s.url} for s in self.sources]
        return raw

    def validate(self) -> SearchConfig:
        """Check the draft the way a hand-edited file is checked, then some.

        Reusing `parse_search` rather than reimplementing the rules is the whole
        point: the dashboard's error messages are then the same strings
        config.yaml produces, and they cannot drift apart.

        On top of that, a URL the dashboard is about to write is checked against
        `site_rules` — a robots-disallowed autovit price search, mobile.de's
        403 route or its id-less category page. Deliberately *here* and not in
        `parse_search`: this must never make an existing config.yaml fail to
        load, because that would stop collection for a URL that has been
        working, on a rule about what we ought to fetch rather than what we can.
        """
        try:
            parsed = parse_search(self.as_raw())
        except ConfigError as exc:
            raise InvalidConfigError(str(exc)) from exc

        _check_ranges(parsed.metadata)

        for source in parsed.sources:
            refusal = site_rules.refusal(source.site, source.url)
            if refusal:
                raise DisallowedUrlError(f"{source.site}: {refusal}")

        return parsed


def _check_ranges(metadata: dict[str, Any]) -> None:
    """Refuse a from/to pair that describes nothing, or a value off the scale.

    Here rather than in `parse_search`, for the same reason the site rules are:
    this must never stop an existing config.yaml loading. These are display-only
    fields, so a config with `year_min: 2025, year_max: 2024` collects perfectly
    well — it just renders a chip pair that cannot match anything. Catching it
    where the dashboard writes it is the useful place; refusing to start over it
    would not be.
    """
    for low_key, high_key, what in RANGE_PAIRS:
        low, high = metadata.get(low_key), metadata.get(high_key)
        if low is not None and high is not None and low > high:
            raise ImpossibleRangeError(
                f"The {what} range is the wrong way round: {low_key} is {low} "
                f"but {high_key} is {high}. Nothing can be both."
            )

    for key in ("price_min", "price_max"):
        value = metadata.get(key)
        if value is not None and value < 0:
            raise ImpossibleRangeError(f"`{key}` cannot be negative (got {value}).")

    first, last = YEAR_RANGE
    for key in ("year_min", "year_max"):
        value = metadata.get(key)
        if value is not None and not (first <= value <= last):
            raise ImpossibleRangeError(
                f"`{key}` is {value}, which is not a year — it should be "
                f"between {first} and {last}. A slipped digit is the usual cause."
            )


# ------------------------------------------------------------------ ruamel


def _yaml() -> YAML:
    yaml = YAML()  # round-trip mode: comments, key order and quoting survive
    yaml.preserve_quotes = True
    # config.yaml's own shape: `- ` two in from its parent key, content four.
    yaml.indent(mapping=2, sequence=4, offset=2)
    # Never fold a long line. A pasted search URL is 300+ characters, and
    # wrapping it — while valid YAML — makes the file unreadable and the URL
    # painful to copy back out.
    yaml.width = 4096
    return yaml


def _load_document(path: Path) -> CommentedMap:
    with path.open("r", encoding="utf-8") as fh:
        doc = _yaml().load(fh)
    if doc is None:
        doc = CommentedMap()
    if not isinstance(doc, dict):
        raise InvalidConfigError(f"{path} must be a YAML mapping at the top level.")
    return doc


def _dump(doc: CommentedMap) -> str:
    stream = io.StringIO()
    _yaml().dump(doc, stream)
    return stream.getvalue()


def _searches(doc: CommentedMap) -> CommentedSeq:
    node = doc.get("searches")
    if node is None:
        node = CommentedSeq()
        doc["searches"] = node
    if not isinstance(node, list):
        raise InvalidConfigError("`searches` must be a list.")
    return node


def _block_name(block: Any) -> str:
    if not isinstance(block, dict):
        return ""
    return str(block.get("name", "")).strip()


def _find_block(doc: CommentedMap, name: str) -> tuple[int, CommentedMap]:
    """The `searches[]` entry with this name, matched the way names are compared."""
    target = str(name).strip().casefold()
    for index, block in enumerate(_searches(doc)):
        if _block_name(block).casefold() == target and _block_name(block):
            return index, block
    raise NoSuchSearchError(f"No configuration named {name!r} in the config file.")


def _names(doc: CommentedMap) -> list[str]:
    return [n for n in (_block_name(b) for b in _searches(doc)) if n]


def _set_key(block: CommentedMap, key: str, value: Any) -> None:
    """Set, or remove when `value is None`, keeping a sensible key order.

    An existing key is assigned in place — that is what keeps its comment. A new
    key is inserted at the position `FIELD_ORDER` gives it rather than appended,
    so metadata does not end up below `sources:`.
    """
    if value is None:
        block.pop(key, None)
        return
    if key in block:
        block[key] = value
        return

    rank = FIELD_ORDER.index(key) if key in FIELD_ORDER else len(FIELD_ORDER)
    position = 0
    for index, existing in enumerate(list(block.keys())):
        existing_rank = (
            FIELD_ORDER.index(existing) if existing in FIELD_ORDER else len(FIELD_ORDER)
        )
        if existing_rank <= rank:
            position = index + 1
    block.insert(position, key, value)


def _scalar(value: Any) -> Any:
    """Strings are written double-quoted, matching the file's existing style."""
    return DQ(value) if isinstance(value, str) else value


def _source_block(source: Source) -> CommentedMap:
    block = CommentedMap()
    block["site"] = DQ(source.site)
    block["url"] = DQ(source.url)
    return block


def _apply_sources(block: CommentedMap, sources: list[Source]) -> None:
    """Bring one configuration's `sources:` into line with the draft.

    Matched on site, mutated in place, and existing entries keep their original
    order even if the form listed them differently. Order carries no meaning to
    the collector, and every source block in the shipped config.yaml has a
    paragraph of explanation above it that reordering would strand next to the
    wrong URL.
    """
    existing = block.get("sources")
    if not isinstance(existing, list):
        existing = CommentedSeq()
        _set_key(block, "sources", existing)

    wanted = {s.site: s for s in sources}
    kept: set[str] = set()

    for index in range(len(existing) - 1, -1, -1):
        entry = existing[index]
        site = (
            str(entry.get("site", "")).strip().lower() if isinstance(entry, dict) else ""
        )
        source = wanted.get(site)
        if source is None:
            del existing[index]
            continue
        entry["url"] = DQ(source.url)
        kept.add(site)

    for source in sources:
        if source.site not in kept:
            existing.append(_source_block(source))


def _write_block(block: CommentedMap, draft: SearchDraft) -> None:
    """Draft -> YAML block, touching only the keys the draft speaks for."""
    _set_key(block, "name", DQ(draft.name))
    for key in METADATA_KEYS:
        _set_key(block, key, _scalar(draft.metadata.get(key)))
    _set_key(block, "enabled", draft.enabled)
    _apply_sources(block, draft.sources)


# ------------------------------------------------------------ the write path


def _backup_paths(path: Path) -> list[Path]:
    """Existing backups for this config, newest first."""
    return sorted(
        path.parent.glob(path.name + BACKUP_PREFIX + "*"),
        key=lambda p: p.name,
        reverse=True,
    )


def _make_backup(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(path.name + BACKUP_PREFIX + stamp)
    # Two writes inside the same second would otherwise overwrite each other's
    # backup, which is exactly when you most want both of them.
    counter = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}{BACKUP_PREFIX}{stamp}-{counter}")
        counter += 1

    backup.write_bytes(path.read_bytes())

    for old in _backup_paths(path)[KEEP_BACKUPS:]:
        try:
            old.unlink()
        except OSError:  # pragma: no cover - a backup we can't prune is harmless
            pass
    return backup


def _check_stamp(path: Path, expected: Optional[FileStamp]) -> None:
    if expected is None:
        return
    if FileStamp.of(path) != expected:
        raise StaleConfigError(
            f"{path.name} has changed on disk since this form was opened "
            f"(it may be open in an editor). Nothing was written — reload the "
            f"page to pick up the current file, then make your change again."
        )


def _check_not_collecting(db_path: Path) -> None:
    holder = active_holder(db_path)
    if holder is None:
        return
    raise CollectionRunningError(
        f"A collection is running (started {holder.get('started_at', '?')} "
        f"via {holder.get('source', '?')}). It has already loaded the config, "
        f"so the file is not written while it runs. Try again when it finishes."
    )


def _validated(path: Path, text: str) -> Path:
    """Write `text` beside config.yaml and prove it loads. Returns the temp path.

    Beside, not in the system temp directory, because `db_path` is resolved
    relative to the config file's own directory — validating somewhere else
    would check a different database path than the one about to go live, and
    `os.replace` is only atomic within a filesystem.
    """
    temp = path.with_name(path.name + ".tmp-write")
    temp.write_text(text, encoding="utf-8")
    try:
        load_config(temp)
    except ConfigError as exc:
        temp.unlink(missing_ok=True)
        raise InvalidConfigError(
            f"That change would produce a config file CarWatch cannot load, so "
            f"nothing was written: {exc}"
        ) from exc
    return temp


def _mutate(
    path: str | Path,
    change: Callable[[CommentedMap], None],
    *,
    expected: Optional[FileStamp] = None,
    db_path: Optional[str | Path] = None,
) -> Path:
    """Apply `change` to config.yaml's document, safely. Returns the config path.

    The order here is the safety story, and it is why this module is one funnel
    rather than three routes that each write a file.
    """
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ConfigWriteError(f"Config file not found: {path}")

    _check_stamp(path, expected)

    if db_path is None:
        # The database this config points at — the one a collection would lock.
        db_path = load_config(path).db_file
    _check_not_collecting(Path(db_path))

    doc = _load_document(path)
    change(doc)
    text = _dump(doc)

    temp = _validated(path, text)
    _make_backup(path)
    os.replace(temp, path)
    return path


# ------------------------------------------------------------ public writes


def save_search(
    path: str | Path,
    draft: SearchDraft,
    *,
    original_name: Optional[str] = None,
    expected: Optional[FileStamp] = None,
    db_path: Optional[str | Path] = None,
) -> Path:
    """Create a configuration, or update the one called `original_name`.

    Passing `original_name` is what makes this an edit rather than an insert,
    and it is kept separate from `draft.name` so a configuration can be renamed.
    """
    draft.validate()

    def change(doc: CommentedMap) -> None:
        taken = {
            n.casefold()
            for n in _names(doc)
            if original_name is None or n.casefold() != original_name.strip().casefold()
        }
        if draft.name.casefold() in taken:
            raise DuplicateNameError(
                f"A configuration named {draft.name!r} already exists. "
                f"Names must be unique."
            )

        if original_name is None:
            block = CommentedMap()
            _write_block(block, draft)
            _searches(doc).append(block)
        else:
            _, block = _find_block(doc, original_name)
            _write_block(block, draft)

    return _mutate(path, change, expected=expected, db_path=db_path)


def set_enabled(
    path: str | Path,
    name: str,
    enabled: bool,
    *,
    expected: Optional[FileStamp] = None,
    db_path: Optional[str | Path] = None,
) -> Path:
    """Turn one configuration on or off, leaving everything else alone."""

    def change(doc: CommentedMap) -> None:
        _, block = _find_block(doc, name)
        _set_key(block, "enabled", bool(enabled))

    return _mutate(path, change, expected=expected, db_path=db_path)


def archive_search(
    path: str | Path,
    name: str,
    *,
    expected: Optional[FileStamp] = None,
    db_path: Optional[str | Path] = None,
) -> Path:
    """Retire a configuration — the reversible half of removing one.

    Archiving is disabling. `sync_searches` disables the `search` rows rather
    than removing them, so a configuration archived here can be switched back
    on with its history intact. `delete_search` is the irreversible half, and
    accepts only a configuration that has been archived first.
    """
    return set_enabled(path, name, False, expected=expected, db_path=db_path)


def delete_search(
    path: str | Path,
    name: str,
    *,
    expected: Optional[FileStamp] = None,
    db_path: Optional[str | Path] = None,
) -> Path:
    """Remove an archived configuration's block from config.yaml.

    Only the file. The database rows — listings, price history, runs, change
    events — are `db.purge_configuration`'s, and the delete route calls both.
    They are separate because the two halves differ in the one way that
    matters: this one leaves a `config.yaml.bak-*` behind, that one leaves
    nothing.
    """

    def change(doc: CommentedMap) -> None:
        index, block = _find_block(doc, name)
        if bool(block.get("enabled", True)):
            raise NotArchivedError(
                f"{_block_name(block)!r} is still collecting. Archive it first — "
                f"only an archived configuration can be deleted."
            )
        if len(_searches(doc)) == 1:
            raise LastConfigurationError(
                f"{_block_name(block)!r} is the only configuration in "
                f"{Path(path).name}, and CarWatch needs at least one to load. "
                f"Create another before deleting this one."
            )
        _remove_block(doc, index)

    return _mutate(path, change, expected=expected, db_path=db_path)


# ----------------------------------------------- removing a block cleanly

# ruamel attaches a comment to the node *before* it. The paragraph introducing
# a configuration is therefore stored on the last line of the configuration
# above it, and whatever follows a configuration — the next one's heading, or
# the worked example at the foot of config.yaml — is stored on its own last
# line. Deleting a block naively loses its neighbour's comment and strands its
# own heading above the wrong block: exactly backwards.


def _trailing_slot(node: Any) -> Optional[tuple[Any, Any]]:
    """(container, key) whose comment slot follows `node`'s last scalar."""
    if isinstance(node, CommentedMap) and node:
        key: Any = list(node.keys())[-1]
    elif isinstance(node, CommentedSeq) and node:
        key = len(node) - 1
    else:
        return None
    value = node[key]
    if isinstance(value, (CommentedMap, CommentedSeq)) and value:
        return _trailing_slot(value)
    return node, key


def _slot_position(container: Any) -> int:
    # A mapping keeps the comment after a value in position 2 of its entry; a
    # sequence keeps the comment after an item in position 0.
    return 2 if isinstance(container, CommentedMap) else 0


def _get_trailing(slot: Optional[tuple[Any, Any]]) -> Any:
    if slot is None:
        return None
    container, key = slot
    entry = container.ca.items.get(key)
    return entry[_slot_position(container)] if entry else None


def _set_trailing(slot: tuple[Any, Any], token: Any) -> None:
    container, key = slot
    entry = container.ca.items.setdefault(key, [None, None, None, None])
    entry[_slot_position(container)] = token


def _remove_block(doc: CommentedMap, index: int) -> None:
    """Delete `searches[index]`, handing its trailing comment to what now precedes it.

    The block's own heading goes with it. The comment that followed it — the
    next block's heading, or the file's footer — moves up into the heading's
    place. An end-of-line comment on the preceding block's last line stays on
    that line.
    """
    seq = _searches(doc)
    trailing = _get_trailing(_trailing_slot(seq[index]))

    if index > 0:
        before = _trailing_slot(seq[index - 1])
        old = _get_trailing(before)
        # `url: "..."  # note\n<heading>` — up to the first newline is the
        # end-of-line comment, and it belongs to the line it sits on.
        eol = ""
        if old is not None and not old.value.startswith("\n"):
            eol = old.value.split("\n", 1)[0] + "\n"
        if before is not None and trailing is not None:
            value = trailing.value
            if eol:
                value = eol + (value[1:] if value.startswith("\n") else value)
            trailing.value = value
            _set_trailing(before, trailing)
        elif before is not None and old is not None:
            if eol:
                old.value = eol
            else:
                _set_trailing(before, None)
    elif trailing is not None:
        # Nothing precedes the first block but `searches:` itself, which keeps
        # the comments above its first item in position 3 of its entry.
        entry = doc.ca.items.setdefault("searches", [None, None, None, None])
        trailing.value = trailing.value.lstrip("\n")
        entry[3] = [trailing]

    del seq[index]


def export_search(path: str | Path, name: str) -> str:
    """One configuration as a standalone YAML snippet, comments included.

    For moving a configuration between machines, and as the fallback if the
    write path ever misbehaves: whatever else breaks, you can always read a
    configuration out and paste it in by hand.
    """
    doc = _load_document(Path(path).expanduser())
    _, block = _find_block(doc, name)

    snippet = CommentedMap()
    seq = CommentedSeq()
    seq.append(block)
    snippet["searches"] = seq
    return _dump(snippet)


def read_config(path: str | Path) -> Config:
    """`load_config`, re-exported so callers that write don't import two modules."""
    return load_config(path)


def stamp_of(path: str | Path) -> FileStamp:
    return FileStamp.of(path)
