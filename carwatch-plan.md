# CarWatch — Build Spec

A local tool that tracks used-car listings across **olx.ro**, **autovit.ro**, and **mobile.de/ro** for a set of user-defined searches, keeps a price/availability history in a local database, and shows everything in a local web dashboard. Runs on Windows and macOS, on demand or on a daily schedule.

> This document is a spec to hand to Claude Code. It states the goals, the architecture, and the decisions already made, and leaves clearly-marked room for Claude Code to make implementation choices. Build it in the phases at the bottom — do **not** try to build everything at once.

---

## 1. Goals

- Track one or more **car searches** (e.g. "BMW 3-series, 2015–2018, diesel, automatic, under €18k") across three sites.
- For each run, record every matching listing and detect, versus the previous run:
  - **New** listings that appeared
  - **Price changes** (up or down) on existing listings
  - **Delisted** listings that disappeared
- Keep a **price history** per listing over time.
- Present all of this in a **local web dashboard** the user opens in a browser.
- Run **on demand** (a button in the dashboard + a CLI command) and **once a day** (OS scheduler).
- Work on **Windows and macOS** with the same codebase.

## 2. Non-goals (keep scope tight)

- No cloud hosting, no user accounts, no multi-user support. Single user, local machine.
- No mobile app.
- No buying/bidding/contacting sellers — read-only observation.
- No attempt to defeat CAPTCHAs or aggressive anti-bot systems. If a site blocks us, we degrade gracefully and report it (see §8).

## 3. Key design decisions (already made — follow these)

1. **Searches are defined by pasted search URLs, not by re-modelling each site's filter schema.**
   The user sets up filters in their browser on each site, copies the resulting search-results URL, and pastes it into the config. The tool paginates through that URL. This avoids having to reverse-engineer and maintain each site's filter parameters, which is the single biggest source of fragility. Structured filters (make/model/year/price) are stored as *metadata* for display/grouping only.

2. **One adapter per site, behind a common interface.**
   Each site is an isolated module implementing the same `SiteAdapter` contract (§7). A site breaking or getting blocked must never break the others. Adding a 4th site later = adding one file.

3. **Prefer structured data sources over HTML scraping, per site.**
   - autovit.ro and olx.ro are Next.js apps on the OLX Group platform — look for embedded JSON (`__NEXT_DATA__`) or the JSON API endpoints the page itself calls, and parse those instead of HTML where possible. Much more stable than CSS selectors.
   - Fall back to HTML parsing only when no clean JSON is available.

4. **Fetch strategy escalates only as needed.** Plain HTTP first (fast, cheap). Use a headless browser (Playwright) only for sites/pages that require JS execution or return bot walls to plain HTTP. Don't default everything to a browser.

5. **The collector and the dashboard are separate concerns.** The collector writes to SQLite; the dashboard reads from SQLite (and can *trigger* a collection). They share the DB and models, nothing else.

## 4. Architecture

```
                 ┌────────────────────┐
   config.yaml → │     Collector      │ ──writes──┐
   (searches)    │  (per-site adapters│           ▼
                 │   + diff engine)   │      ┌──────────┐
                 └────────┬───────────┘      │  SQLite  │
                          │ triggered by     │  (state) │
              CLI / scheduler / dashboard    └────┬─────┘
                                                  │ reads
                                          ┌───────▼────────┐
                                          │   Dashboard    │
                                          │ FastAPI + htmx │ ← user's browser
                                          └────────────────┘
```

Data flow per run: for each enabled search → adapter fetches all result pages → normalizes to a common `Listing` shape → diff engine compares against last-known state for that search → writes new snapshots, price-history rows, and marks disappeared listings inactive → records a `run` row with counts and any errors.

## 5. Tech stack

- **Language:** Python 3.11+. (Cross-platform, best scraping ecosystem.)
- **HTTP:** `httpx`.
- **HTML parsing (fallback path):** `selectolax` (fast) or `beautifulsoup4`.
- **Headless browser (escalation path):** `playwright` (Chromium). Install note: `playwright install chromium`.
- **DB / models:** SQLite via **SQLModel** (SQLAlchemy + Pydantic types — pairs cleanly with FastAPI). Plain `sqlite3` is acceptable if Claude Code prefers fewer deps.
- **Dashboard:** **FastAPI** + **Uvicorn**, server-rendered **Jinja2** templates with **htmx** for interactivity (the "Run now" button, filtering, live status) and **Chart.js** (CDN) for price-history sparklines/charts. This keeps the frontend near-zero-maintenance — no build step, no SPA framework.
- **Config:** `config.yaml` (see §11).
- **Env management:** `uv` if available, otherwise `venv` + `pip`. Provide a `requirements.txt` / `pyproject.toml`.

> **Alternative worth noting for Claude Code:** if a read-only dashboard with less code is preferable, **Streamlit** over the same SQLite DB is a valid substitute for the FastAPI+htmx layer. The collector, DB, and adapters stay identical either way. Default to the FastAPI+htmx design unless there's a reason to switch.

## 6. Data model

Tables (SQLModel/SQLAlchemy models):

- **`search`** — one saved search.
  `id, name, site, url (paginated search URL), filters_json (make/model/year_min/year_max/price_max/... for display only), enabled (bool), created_at`

- **`listing`** — one car, identified stably across runs.
  `id (pk), search_id (fk), site, site_listing_id (the site's own id, parsed from URL/JSON), url, title, make, model, year, price, currency, mileage_km, fuel, gearbox, location, first_seen (ts), last_seen (ts), is_active (bool), raw_json (text)`
  Uniqueness: `(search_id, site, site_listing_id)`.

- **`price_history`** — append-only.
  `id, listing_id (fk), price, currency, observed_at (ts)`
  Write a row only when the price differs from the last recorded price for that listing (or on first sighting).

- **`run`** — one collection run (optionally one row per search per run).
  `id, search_id, site, started_at, finished_at, status (ok|partial|blocked|error), listings_found (int), new_count, price_change_count, delisted_count, error_message (nullable)`

- **`event`** *(optional, for a clean dashboard "changes" feed)* — one row per notable change.
  `id, run_id, listing_id, type (new|price_up|price_down|delisted), old_price (nullable), new_price (nullable), created_at`

## 7. Site adapters

Common interface — every site implements this:

```python
class SiteAdapter(Protocol):
    site_name: str

    def fetch_listings(self, search_url: str) -> list[RawListing]:
        """Fetch ALL pages for the search URL and return normalized RawListings.
        Handles pagination internally. Raises BlockedError if the site
        returns a bot wall / 403 / challenge so the collector can record
        status='blocked' instead of treating it as 'no results'."""
```

`RawListing` is the normalized shape (maps onto the `listing` columns): `site_listing_id, url, title, make, model, year, price, currency, mileage_km, fuel, gearbox, location, raw`.

Per-site notes and difficulty:

| Site | Platform | Approach | Difficulty | Notes |
|------|----------|----------|------------|-------|
| **autovit.ro** | OLX Group / OtoMoto (Next.js) | Parse embedded `__NEXT_DATA__` JSON or the JSON endpoint the page calls; HTML fallback | **Medium** | Best-structured of the three. Build this adapter **first** as the reference implementation. |
| **olx.ro** | OLX Group (Next.js) | Same idea — prefer JSON/API over HTML; realistic headers | **Medium** | Has an internal offers API; use it if reachable, else `__NEXT_DATA__`. Watch for rate limiting. |
| **mobile.de/ro** | mobile.de (German) | Likely requires **Playwright**; expect anti-bot challenges | **Hard / best-effort** | Build **last**. Treat blocking as an expected, handled outcome — surface `status='blocked'` in the dashboard rather than failing the whole run. Do not attempt CAPTCHA solving. |

**Critical:** the `site_listing_id` must be stable across runs (parse it from the listing's canonical URL or JSON id). Diffing depends entirely on this being correct — get it right per site.

## 8. Collection & diffing logic

For each enabled search:
1. Call the adapter → list of `RawListing`. On `BlockedError`/network error, record a `run` with the appropriate status and **skip diffing** for that search (don't mass-delist listings just because a fetch failed — this is important; a failed fetch must never be interpreted as "everything got delisted").
2. Upsert listings: match on `(search_id, site, site_listing_id)`.
   - Not seen before → insert, `first_seen=now`, emit `new` event, write initial `price_history` row.
   - Seen before, price changed → update price, `last_seen=now`, append `price_history`, emit `price_up`/`price_down`.
   - Seen before, price same → update `last_seen=now`.
3. Delisting: any listing for this search that was `is_active` but **not** present in a *successful* fetch → set `is_active=false`, emit `delisted`. (Optionally require it to be absent for N consecutive successful runs before delisting, to absorb transient pagination flakiness — make N configurable, default 1.)
4. Write the `run` summary.

Be a **polite scraper** (see §14): randomized small delays between page requests, a sane User-Agent, honor obvious rate limits, cache nothing sensitive.

## 9. Dashboard features

Pages/sections (server-rendered, htmx for interactions):

- **Overview:** list of searches with, per search: active-listing count, last run time + status (ok/partial/blocked), and counts of new/price-changed/delisted since last run. A **"Run now"** button per search and a global one (triggers the collector in the background; status updates via htmx polling).
- **Search detail:** current active listings in a sortable/filterable table (price, year, mileage, km, location, link out to the original ad). Each row expandable to show its **price history chart** (Chart.js). Delisted-but-recently-seen listings shown in a muted section.
- **Changes feed:** reverse-chronological list of `events` (new / price drop ↓ / price rise ↑ / delisted) across all searches, with the price delta. This is the "what changed since I last looked" view — likely the most-used screen.
- **Runs log:** table of recent runs with status and counts, so blocking/errors are visible.

Keep styling minimal and clean; price **drops** should be visually obvious (that's the money event).

## 10. Scheduling (cross-platform)

Do **not** build a resident daemon. Provide a CLI entry point and document OS scheduling:

- CLI: `python -m carwatch.collect [--search <name>|--all]` runs a collection and exits.
- **macOS/Linux:** a `cron` example (e.g. daily at 08:00) + note about `launchd` as an alternative.
- **Windows:** a **Task Scheduler** example (daily trigger running the same command inside the venv).
- Provide a `run.sh` and `run.ps1`/`run.bat` wrapper that activates the environment and runs the collector, so the scheduler entry is a one-liner on both OSes.

The dashboard's "Run now" triggers the same collection code path in-process/background — one code path, two triggers.

## 11. Config format

`config.yaml`:

```yaml
settings:
  db_path: "./carwatch.db"
  request_delay_seconds: [1.5, 4.0]   # random range between page requests
  delist_after_missed_runs: 1
  user_agent: "CarWatch/1.0 (personal use)"

searches:
  - name: "BMW 3-series diesel auto"
    make: "BMW"            # display metadata only
    model: "Seria 3"
    year_min: 2015
    year_max: 2018
    price_max: 18000
    currency: "EUR"
    sources:
      - site: "autovit"
        url: "https://www.autovit.ro/autoturisme/bmw/seria-3/..."   # pasted from browser
      - site: "olx"
        url: "https://www.olx.ro/auto-masini-moto-ambarcatiuni/autoturisme/bmw/..."
      - site: "mobilede"
        url: "https://www.mobile.de/ro/..."
    enabled: true
```

One search can pull from one, two, or all three sites. `make/model/year/price` are metadata for display/grouping; the actual filtering lives in each pasted `url`.

## 12. Suggested project structure

```
carwatch/
  __init__.py
  config.py            # load + validate config.yaml
  db.py                # engine, session, create tables
  models.py            # SQLModel models (§6)
  collect.py           # CLI entry: python -m carwatch.collect
  collector/
    engine.py          # orchestration + diff logic (§8)
    diff.py
  adapters/
    base.py            # SiteAdapter protocol, RawListing, BlockedError
    autovit.py
    olx.py
    mobilede.py
    http.py            # shared httpx client, headers, delays
    browser.py         # shared Playwright helper (lazy-loaded)
  web/
    app.py             # FastAPI app
    routes.py
    templates/         # Jinja2
    static/
config.yaml
requirements.txt / pyproject.toml
run.sh / run.ps1
README.md
tests/
```

## 13. Build phases (build in this order — ship each before the next)

1. **Skeleton + DB:** project layout, config loader, SQLModel models, DB init, empty CLI. No scraping yet.
2. **autovit adapter (reference):** fetch + paginate + normalize one autovit search URL, print results. This proves the adapter contract.
3. **Collector + diffing:** wire the adapter into the engine; upsert, price history, delisting, run records. Verify diffs across two manual runs (change a price by hand in the DB to test).
4. **Dashboard v1:** FastAPI + Jinja2, Overview + Search detail (table, no charts yet), reading from the DB.
5. **olx adapter:** add second site behind the same interface.
6. **Changes feed + price-history charts + "Run now" button.**
7. **Scheduling:** CLI polish + cron / Task Scheduler docs + wrapper scripts.
8. **mobile.de/ro adapter (best-effort):** Playwright path; handle blocking gracefully; surface `blocked` status in the dashboard.
9. **Polish:** notifications (optional, see below), README, error handling review.

Each phase should leave the app runnable.

## 14. Responsible scraping & legal

- This is for **personal use**, low volume (a few searches, once a day). Keep it that way: rate-limit (randomized delays), don't parallelize aggressively, run at most a few times a day.
- Send an honest User-Agent; don't impersonate to evade.
- Check each site's `robots.txt` and Terms of Service. Scraping may be against a site's ToS even when technically possible; the user should be aware and accept that risk for personal use. **Do not** build anything to circumvent CAPTCHAs, login walls, or hard anti-bot systems — if a site blocks us, we record `blocked` and move on.
- Before investing in scrapers, note that **all three sites offer saved-search email alerts** for *new* listings. The unique value this tool adds is (a) consolidating across sites and (b) **price history + delisting detection**, which the native alerts don't provide. Worth stating in the README so the tool's niche is clear.

## 15. Optional extensions (phase 9+, don't build unless asked)

- **Notifications** on new listings / price drops: desktop notification, or email (SMTP), or a Telegram bot. Config-gated, off by default.
- Export a search's history to CSV.
- Simple "deal score" (price vs. median of comparable active listings in the same search).

## 16. Open questions for the user (Claude Code: ask before phase 8+)

- Confirm the mobile.de/ro searches are worth the extra effort given it's the hardest target — or whether autovit + olx alone is enough for v1.
- Preferred notification channel, if any (defer until phase 9).
- Desired daily run time for the scheduler examples.
