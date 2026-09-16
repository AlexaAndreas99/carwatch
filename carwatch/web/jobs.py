"""Background collection jobs for the dashboard's "Run now" button.

§10: the dashboard's "Run now" triggers *the same collection code path* as the
CLI and the scheduler — one implementation, three triggers. This module only
adds "run it off the request thread and remember how it went".

Collection is slow (polite delays, and a real browser for olx), so a run can
take minutes. Requests must never block on it: the button starts a job, and the
page polls a status fragment with htmx.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from carwatch.collector.engine import Progress, RunSummary, collect
from carwatch.collector.lock import CollectionBusy
from carwatch.config import Config
from carwatch.models import utcnow

log = logging.getLogger(__name__)


@dataclass
class Job:
    """One "Run now" press."""

    id: int
    label: str
    search_names: Optional[list[str]]
    sites: Optional[list[str]]
    started_at: datetime = field(default_factory=utcnow)
    finished_at: Optional[datetime] = None
    summaries: list[RunSummary] = field(default_factory=list)
    error: Optional[str] = None
    # Filled in by the collection as it goes, for the status strip.
    progress: Progress = field(default_factory=Progress)

    @property
    def running(self) -> bool:
        return self.finished_at is None

    @property
    def duration_seconds(self) -> float:
        end = self.finished_at or utcnow()
        return (end - self.started_at).total_seconds()

    @property
    def had_problems(self) -> bool:
        return bool(self.error) or any(not s.ok for s in self.summaries)

    @property
    def totals(self) -> dict[str, int]:
        return {
            "found": sum(s.listings_found for s in self.summaries),
            "new": sum(s.new_count for s in self.summaries),
            "price": sum(s.price_change_count for s in self.summaries),
            "delisted": sum(s.delisted_count for s in self.summaries),
        }


class JobRunner:
    """Runs collections in background threads, one at a time.

    Deliberately serial: §14 asks us not to parallelise scraping, and two
    concurrent runs over the same search would race on the same listing rows.
    A second press while a run is in progress returns the job already running
    rather than starting another.
    """

    def __init__(self, config: Config, keep_last: int = 25) -> None:
        self.config = config
        self.keep_last = keep_last
        self._lock = threading.Lock()
        self._jobs: dict[int, Job] = {}
        self._order: list[int] = []
        self._next_id = 1
        self._active: Optional[int] = None

    # ------------------------------------------------------------------ state

    @property
    def active_job(self) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(self._active) if self._active is not None else None

    def get(self, job_id: int) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def latest(self) -> Optional[Job]:
        with self._lock:
            return self._jobs[self._order[-1]] if self._order else None

    def is_busy(self) -> bool:
        return self.active_job is not None

    # ---------------------------------------------------------------- starting

    def start(
        self,
        label: str,
        search_names: Optional[list[str]] = None,
        sites: Optional[list[str]] = None,
    ) -> Job:
        """Start a collection, or return the one already running."""
        with self._lock:
            if self._active is not None:
                return self._jobs[self._active]

            job = Job(
                id=self._next_id, label=label, search_names=search_names, sites=sites
            )
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._next_id += 1
            self._active = job.id
            self._prune_locked()

        thread = threading.Thread(
            target=self._run, args=(job,), name=f"carwatch-collect-{job.id}", daemon=True
        )
        thread.start()
        return job

    def _prune_locked(self) -> None:
        while len(self._order) > self.keep_last:
            self._jobs.pop(self._order.pop(0), None)

    def _run(self, job: Job) -> None:
        try:
            job.summaries = collect(
                self.config,
                search_names=job.search_names,
                sites=job.sites,
                source="dashboard",
                progress=job.progress,
            )
        except CollectionBusy as exc:
            # The scheduled run got there first. Not an error worth a traceback.
            job.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - a job must never kill the thread silently
            log.exception("Collection job %s failed", job.id)
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            job.finished_at = utcnow()
            with self._lock:
                if self._active == job.id:
                    self._active = None

    # ------------------------------------------------------------------ tests

    def wait(self, timeout: float = 30.0) -> None:
        """Block until no job is running. For tests and shutdown, not requests."""
        deadline = time.monotonic() + timeout
        while self.is_busy() and time.monotonic() < deadline:
            time.sleep(0.02)
