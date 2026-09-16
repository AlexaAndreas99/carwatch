"""A cross-process lock so only one collection runs at a time.

`JobRunner` serialises the dashboard's "Run now" presses, but that is a
`threading.Lock` inside one process. It does nothing about the case that
actually happens: the scheduled 08:00 run overlapping with someone pressing
"Run now", or a second terminal.

That overlap is not merely untidy. Each collection reads the current listings,
fetches, then diffs against what it read. Two overlapping runs both diff against
a pre-fetch snapshot, so the second one writes a diff computed from state the
first has already changed - double `new` events, or a delisting for something
the other run just re-inserted. SQLite would also start returning "database is
locked" once both tried to write.

A lock file next to the database is enough: collections are minutes apart at
most, and both writers are this same tool.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

# A collection that has held the lock longer than this is assumed dead - killed
# mid-run, or the machine was reset. Generous, because a slow site plus a
# browser render can legitimately take several minutes.
DEFAULT_STALE_AFTER_SECONDS = 2 * 60 * 60


class CollectionBusy(RuntimeError):
    """Another collection holds the lock."""

    def __init__(self, holder: dict, path: Path) -> None:
        pid = holder.get("pid", "?")
        started = holder.get("started_at", "?")
        source = holder.get("source", "?")
        super().__init__(
            f"another collection is already running (pid {pid}, started {started}, "
            f"via {source}). Lock file: {path}"
        )
        self.holder = holder
        self.path = path


def _read_holder(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Unreadable or half-written: treat as an unknown holder rather than
        # crashing. Staleness will deal with it.
        return {}


def lock_path_for(db_path: str | Path) -> Path:
    """Where the collection lock for this database lives."""
    return Path(db_path).expanduser().resolve().with_suffix(".lock")


def active_holder(
    db_path: str | Path,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
) -> dict | None:
    """Who is collecting right now, or None.

    A read-only peek at the lock, for callers that need to *refuse* something
    while a collection is in flight rather than queue behind it — the config
    writer, which must not rewrite config.yaml underneath a run that has
    already loaded it. A lock older than `stale_after_seconds` is treated as
    dead, exactly as `collection_lock` treats it.
    """
    path = lock_path_for(db_path)
    if not path.exists():
        return None

    holder = _read_holder(path)
    age = time.time() - float(holder.get("epoch") or 0)
    if age >= stale_after_seconds:
        return None
    return holder


@contextmanager
def collection_lock(
    db_path: str | Path,
    source: str = "cli",
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
) -> Iterator[Path]:
    """Hold the collection lock for `db_path`, or raise CollectionBusy.

    `source` is recorded in the lock file so the message can say what is
    holding it ("cli", "dashboard", ...).
    """
    path = lock_path_for(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(
        {
            "pid": os.getpid(),
            "source": source,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "epoch": time.time(),
        }
    )

    fd = None
    while fd is None:
        try:
            # O_EXCL makes creation atomic: whoever creates the file wins, on
            # both Windows and POSIX.
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise

            holder = _read_holder(path)
            age = time.time() - float(holder.get("epoch") or 0)
            if age < stale_after_seconds:
                raise CollectionBusy(holder, path) from None

            # Stale: a previous run died holding it. Take it over.
            log.warning(
                "Removing a stale collection lock (%.0f minutes old, pid %s): %s",
                age / 60,
                holder.get("pid", "?"),
                path,
            )
            try:
                path.unlink()
            except FileNotFoundError:
                pass  # someone else cleaned up first; loop and retry

    try:
        os.write(fd, payload.encode("utf-8"))
        os.close(fd)
        fd = None
        yield path
    finally:
        if fd is not None:
            os.close(fd)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
