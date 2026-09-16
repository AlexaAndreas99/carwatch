"""Politeness controls: per-site pacing, blocked-cooldown, Retry-After.

These exist so a site has no reason to block us — they make CarWatch
lower-impact, not harder to detect.
"""

import textwrap

import httpx
import pytest
from sqlmodel import select

from carwatch.adapters.base import AdapterError, BlockedError, RawListing
from carwatch.adapters.http import (
    MAX_RETRY_AFTER_SECONDS,
    PoliteClient,
    _retry_after_seconds,
)
from carwatch.collector.engine import collect
from carwatch.config import ConfigError, load_config
from carwatch.db import session_scope
from carwatch.models import Run, RunStatus

BASE_CONFIG = """
settings:
  db_path: "{db}"
  request_delay_seconds: [1.5, 4.0]
{extra}
searches:
  - name: "Q"
    sources:
      - site: "autovit"
        url: "https://www.autovit.ro/autoturisme/nissan/qashqai"
      - site: "mobilede"
        url: "https://www.mobile.de/ro/vehicule/cautare.html?x=1"
"""


def write_config(tmp_path, extra=""):
    p = tmp_path / "config.yaml"
    p.write_text(
        textwrap.dedent(BASE_CONFIG).format(
            db=(tmp_path / "t.db").as_posix(), extra=extra
        ),
        encoding="utf-8",
    )
    return load_config(p)


def raw(ad_id, price=20000.0):
    return RawListing(
        site_listing_id=ad_id,
        url=f"https://example.com/{ad_id}",
        title="Car",
        price=price,
        currency="EUR",
    )


class Scripted:
    """Adapter factory returning a canned result (or exception) per site."""

    def __init__(self, per_site):
        self.per_site = per_site
        self.calls = []

    def __call__(self, site, settings):
        outer = self

        class A:
            last_relaxation = None

            def fetch_listings(self, url):
                outer.calls.append(site)
                result = outer.per_site[site]
                if isinstance(result, Exception):
                    raise result
                return result

        return A()


# ------------------------------------------------------------ per-site pacing

PER_SITE = "  per_site:\n    mobilede:\n      request_delay_seconds: [5.0, 12.0]\n"


def test_per_site_delay_overrides_the_default(tmp_path):
    config = write_config(tmp_path, PER_SITE)

    assert config.settings.request_delay_seconds == (1.5, 4.0)
    assert config.settings.for_site("mobilede").request_delay_seconds == (5.0, 12.0)
    # A site without an override keeps the default.
    assert config.settings.for_site("autovit").request_delay_seconds == (1.5, 4.0)


def test_for_site_does_not_mutate_the_base_settings(tmp_path):
    config = write_config(tmp_path, PER_SITE)
    config.settings.for_site("mobilede")
    assert config.settings.request_delay_seconds == (1.5, 4.0)


def test_per_site_rejects_unknown_site(tmp_path):
    with pytest.raises(ConfigError, match="unknown site"):
        write_config(tmp_path, "  per_site:\n    craigslist:\n      user_agent: x\n")


def test_per_site_rejects_unsupported_key(tmp_path):
    with pytest.raises(ConfigError, match="unsupported key"):
        write_config(tmp_path, "  per_site:\n    mobilede:\n      max_pages: 3\n")


def test_adapter_receives_the_site_specific_settings(tmp_path):
    """The whole point: the adapter must be built with the slower cadence."""
    config = write_config(tmp_path, PER_SITE)
    seen = {}

    def factory(site, settings):
        seen[site] = settings.request_delay_seconds

        class A:
            last_relaxation = None

            def fetch_listings(self, url):
                return []

        return A()

    collect(config, adapter_factory=factory)
    assert seen["mobilede"] == (5.0, 12.0)
    assert seen["autovit"] == (1.5, 4.0)


# ----------------------------------------------------------- blocked cooldown

COOLDOWN = "  blocked_cooldown_hours: 12\n"


def test_blocked_site_is_skipped_during_cooldown(tmp_path):
    config = write_config(tmp_path, COOLDOWN)
    adapter = Scripted({"autovit": [raw("1")], "mobilede": BlockedError("403")})

    collect(config, adapter_factory=adapter)              # mobilede blocks
    adapter.calls.clear()
    summaries = collect(config, adapter_factory=adapter)  # second run

    assert "mobilede" not in adapter.calls   # not even asked
    assert "autovit" in adapter.calls        # other sites unaffected

    skipped = next(s for s in summaries if s.site == "mobilede")
    assert skipped.status is RunStatus.BLOCKED
    assert "backing off" in skipped.error_message


def test_cooldown_writes_no_run_row_for_the_skipped_site(tmp_path):
    """Nothing was attempted, so nothing should look like an attempt."""
    config = write_config(tmp_path, COOLDOWN)
    adapter = Scripted({"autovit": [raw("1")], "mobilede": BlockedError("403")})
    collect(config, adapter_factory=adapter)
    collect(config, adapter_factory=adapter)

    with session_scope(config.db_file) as session:
        runs = [r for r in session.exec(select(Run)).all() if r.site == "mobilede"]
    assert len(runs) == 1   # only the run that actually got blocked


def test_cooldown_never_delists_anything(tmp_path):
    """A skipped site must leave its listings alone, exactly like a blocked one."""
    from carwatch.models import Listing

    config = write_config(tmp_path, COOLDOWN)
    ok_then_blocked = Scripted({"autovit": [raw("1")], "mobilede": [raw("m1")]})
    collect(config, adapter_factory=ok_then_blocked)

    blocked = Scripted({"autovit": [raw("1")], "mobilede": BlockedError("403")})
    collect(config, adapter_factory=blocked)   # blocks
    collect(config, adapter_factory=blocked)   # skipped by cooldown

    with session_scope(config.db_file) as session:
        listings = [x for x in session.exec(select(Listing)).all() if x.site == "mobilede"]
    assert listings and all(x.is_active for x in listings)


def test_cooldown_disabled_by_default(tmp_path):
    config = write_config(tmp_path)
    assert config.settings.blocked_cooldown_hours == 0.0

    adapter = Scripted({"autovit": [raw("1")], "mobilede": BlockedError("403")})
    collect(config, adapter_factory=adapter)
    adapter.calls.clear()
    collect(config, adapter_factory=adapter)
    assert "mobilede" in adapter.calls   # retried immediately


def test_cooldown_does_not_apply_to_ordinary_errors(tmp_path):
    """An error is usually ours — a parse break, a network blip — and is worth
    retrying. Only an explicit block earns a back-off."""
    config = write_config(tmp_path, COOLDOWN)
    adapter = Scripted({"autovit": [raw("1")], "mobilede": AdapterError("parse broke")})

    collect(config, adapter_factory=adapter)
    adapter.calls.clear()
    collect(config, adapter_factory=adapter)
    assert "mobilede" in adapter.calls


def test_negative_cooldown_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="must not be negative"):
        write_config(tmp_path, "  blocked_cooldown_hours: -1\n")


# ---------------------------------------------------------------- Retry-After


@pytest.mark.parametrize(
    "header,expected",
    [("30", 30.0), ("0", 0.0), ("  45 ", 45.0), (None, None), ("garbage", None)],
)
def test_retry_after_parsing(header, expected):
    headers = {"Retry-After": header} if header is not None else {}
    assert _retry_after_seconds(httpx.Response(429, headers=headers)) == expected


def test_retry_after_accepts_an_http_date():
    import email.utils
    import time as _time

    when = email.utils.formatdate(_time.time() + 60, usegmt=True)
    seconds = _retry_after_seconds(httpx.Response(429, headers={"Retry-After": when}))
    assert seconds is not None and 30 <= seconds <= 90


def test_rate_limit_is_retried_then_reported(monkeypatch):
    """429 means 'slow down', so it is retried — but a site that keeps saying it
    still ends as `blocked`, never as zero results."""
    slept = []
    monkeypatch.setattr("carwatch.adapters.http.time.sleep", lambda s: slept.append(s))

    calls = {"n": 0}

    def always_429(self, url):
        calls["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "5"}, text="slow down")

    monkeypatch.setattr("httpx.Client.get", always_429)

    client = PoliteClient("CarWatch/1.0", (0, 0), max_retries=2)
    with pytest.raises(BlockedError):
        client.get_html("https://example.com/x")

    assert calls["n"] == 3   # initial + 2 retries
    assert 5 in slept        # honoured Retry-After


def test_retry_after_is_capped(monkeypatch):
    """A multi-hour Retry-After should not hold a collection run hostage."""
    slept = []
    monkeypatch.setattr("carwatch.adapters.http.time.sleep", lambda s: slept.append(s))
    monkeypatch.setattr(
        "httpx.Client.get",
        lambda self, url: httpx.Response(429, headers={"Retry-After": "99999"}, text="x"),
    )

    with pytest.raises(BlockedError):
        PoliteClient("CarWatch/1.0", (0, 0), max_retries=1).get_html("https://example.com")

    assert max(slept) <= MAX_RETRY_AFTER_SECONDS


# ------------------------------------------------- one collection at a time


def test_second_collection_is_refused_while_one_runs(tmp_path):
    """The scheduler and a dashboard "Run now" can overlap. Both would diff
    against a pre-fetch snapshot, so the second must not proceed."""
    from carwatch.collector.lock import CollectionBusy, collection_lock

    config = write_config(tmp_path)
    adapter = Scripted({"autovit": [raw("1")], "mobilede": []})

    with collection_lock(config.db_file, source="scheduler"):
        with pytest.raises(CollectionBusy, match="already running"):
            collect(config, adapter_factory=adapter)

    # Released again once the first run finishes.
    assert collect(config, adapter_factory=adapter)


def test_lock_is_released_even_if_collection_raises(tmp_path):
    from carwatch.collector.lock import collection_lock

    config = write_config(tmp_path)

    class Boom:
        def __call__(self, site, settings):
            raise RuntimeError("kaboom")

    # collect() contains per-search errors, so force the failure outside them.
    with pytest.raises(RuntimeError):
        with collection_lock(config.db_file):
            raise RuntimeError("kaboom")

    assert not config.db_file.with_suffix(".lock").exists()
    assert collect(config, adapter_factory=Scripted({"autovit": [], "mobilede": []}))


def test_stale_lock_from_a_dead_run_is_taken_over(tmp_path):
    """A run killed mid-collection must not block the tool forever."""
    import json
    import time as _time

    from carwatch.collector.lock import collection_lock

    config = write_config(tmp_path)
    lock_path = config.db_file.with_suffix(".lock")
    lock_path.write_text(
        json.dumps({"pid": 999999, "source": "cli", "epoch": _time.time() - 10_000}),
        encoding="utf-8",
    )

    with collection_lock(config.db_file, stale_after_seconds=3600):
        pass  # taken over without raising

    assert not lock_path.exists()


def test_unreadable_lock_file_does_not_crash(tmp_path):
    from carwatch.collector.lock import CollectionBusy, collection_lock

    config = write_config(tmp_path)
    config.db_file.with_suffix(".lock").write_text("not json", encoding="utf-8")

    # No epoch means age is huge, so it is treated as stale and taken over.
    with collection_lock(config.db_file, stale_after_seconds=60):
        pass


def test_force_bypasses_the_cooldown(tmp_path):
    """Runs are manual, so a back-off meant for unattended scheduling must not
    silently refuse a user who is sitting there asking for one."""
    config = write_config(tmp_path, COOLDOWN)
    adapter = Scripted({"autovit": [raw("1")], "mobilede": BlockedError("403")})

    collect(config, adapter_factory=adapter)          # mobilede blocks
    adapter.calls.clear()
    collect(config, adapter_factory=adapter)          # skipped
    assert "mobilede" not in adapter.calls

    adapter.calls.clear()
    collect(config, adapter_factory=adapter, force=True)
    assert "mobilede" in adapter.calls                # asked anyway
