"""A collection must not hold the database's write lock while it fetches.

SQLite allows one writer. The Run row used to be written before the fetch,
which on mobile.de takes minutes, so the dashboard's own writes (opening Changes
records when you last looked) waited out their busy timeout and failed: a 500
for anyone who opened Changes during a scheduled run.
"""

import sqlite3
import textwrap

from carwatch.adapters.base import RawListing
from carwatch.collector.engine import collect
from carwatch.config import load_config
from carwatch.db import init_db

CONFIG = """
settings:
  db_path: "{db}"
searches:
  - name: "Qashqai"
    sources:
      - site: "autovit"
        url: "https://www.autovit.ro/autoturisme/nissan/qashqai"
"""


class WritesWhileFetching:
    """An adapter that, mid-fetch, writes to the database as the dashboard would."""

    last_relaxation = None

    def __init__(self, db_file):
        self.db_file = db_file
        self.error = None

    def __call__(self, site, settings):
        return self

    def fetch_listings(self, url, max_pages=None):
        conn = sqlite3.connect(self.db_file, timeout=0.5)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO app_state (name, value) VALUES ('changes_seen_at', 'x')"
            )
            conn.commit()
        except sqlite3.OperationalError as exc:
            self.error = exc
        finally:
            conn.close()
        return [
            RawListing(
                site_listing_id="1", url="https://www.autovit.ro/anunt/a-ID1.html",
                title="Nissan", price=25000.0, currency="EUR",
            )
        ]


def test_the_dashboard_can_write_while_a_site_is_being_fetched(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        textwrap.dedent(CONFIG).format(db=(tmp_path / "t.db").as_posix()), encoding="utf-8"
    )
    config = load_config(path)
    init_db(config.db_file)
    adapter = WritesWhileFetching(config.db_file)

    [summary] = collect(config, adapter_factory=adapter)

    assert adapter.error is None
    assert summary.ok and summary.new_count == 1
