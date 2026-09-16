# CarWatch — Configurations Plan

Follow-on to `carwatch-plan.md` (nine phases, the collection side) and
`carwatch-frontend-plan.md` (five phases, the current dashboard). Both are complete. Same
working method: build the phases at the bottom one at a time, each leaving the app runnable.

Written on day 4 of live collection, against a database holding 256 active cars, 5 delistings
and 267 price observations across five sources.

---

## 1. Goals

- **A configuration becomes a thing you can see and manage**, not a name buried in two
  different dropdowns.
- **A permanent left sidebar.** Click a configuration and everything scopes to it — the cars,
  and the changes.
- **Create, edit and save a configuration from the dashboard**, without hand-editing YAML.
- **Find one quickly** once there are more than a handful.
- **Make a silently-broken configuration visible.** The strict "2025 4x4 Tekna" search has
  returned 0 on every run since collection began, because autovit quietly relaxes it. That fact
  currently lives in a note on the Runs page and nowhere else.

## 2. Non-goals

- No change to collection. Adapters, diffing, price history, delisting and the merge layer are
  done and stay untouched.
- No SPA, no build step. Server-rendered Jinja2 + htmx, as now.
- **Not a saved-views feature.** A configuration is a collection target, not a stored filter
  over already-collected cars (decision 1). Filters stay in the query string as they are.
- No renaming of the `search` table. See §11.
- No multi-user, no auth, no cloud. Still one person, one machine, one SQLite file.

## 3. Decisions already made (approved 2026-09-08)

1. **A configuration is a collection target.** Metadata plus one pasted URL per site. Creating
   one from the UI changes what CarWatch actually goes and fetches.
2. **The UI writes `config.yaml`.** It remains the single source of truth. This is the reason
   §4 exists and is the riskiest part of the phase.
3. **The sidebar replaces both toolbar pickers.** One selection, shared between Listings and
   Changes, carried in the URL. Runs is out of scope for scoping.

Decision 2 is the load-bearing one, and it has a pleasant consequence: because `config.yaml`
stays authoritative, `sync_searches` keeps working exactly as it does today. Nothing needs an
`origin` column, and there is no second source of truth to reconcile.

## 4. Writing `config.yaml` safely

This is the part that can lose the user's work, so it gets built first and alone.

**Comments are the problem.** 76 of the 125 lines in `config.yaml` are comments — 61% of the
file, including the robots.txt notes on mobile.de, the explanation of why the autovit URL
carries no price filter, and the worked example at the bottom. `yaml.safe_dump` discards all of
it. **This phase therefore adds `ruamel.yaml`** (round-trip mode), which preserves comments,
key order and formatting. That is a new dependency and the only one this plan adds.

The write path, in order:

1. **Guard against a concurrent hand-edit.** The form records the file's mtime and size when
   rendered. On save, if either has changed, refuse and say so. Without this the dashboard
   silently clobbers an editor window that happened to be open.
2. **Refuse to write while a collection is running.** The collector loads config at the start
   of a run; rewriting underneath it is asking for a half-applied change. The existing
   `collection_lock` already gives us this signal.
3. **Round-trip, mutate, serialise** — touching only the `searches:` list. `settings:` is never
   written by the UI, which keeps the blast radius small and leaves the pacing and politeness
   knobs firmly hand-edited.
4. **Validate before committing.** Write to a temp file, load it back through the existing
   `carwatch/config.py` validation, and only then replace. A config the app can't parse breaks
   every future collection, so it must never reach disk as `config.yaml`.
5. **Atomic replace** (`os.replace`) so a crash mid-write cannot truncate the file.
6. **Timestamped backup** on every write — `config.yaml.bak-YYYYMMDD-HHMMSS`, most recent few
   kept. Cheap insurance against a bad edit.

Everything here is testable without a browser and should be tested exhaustively, including the
comment-preservation guarantee — a test that writes a new configuration and asserts the
mobile.de robots.txt note is still present afterwards.

## 5. The sidebar

A persistent left column on Listings and Changes, roughly 220px.

Each entry shows the configuration's **name**, its **active car count**, and a **health dot**:

| Dot | Meaning |
|-----|---------|
| green | every source collected cleanly on its last run |
| amber | a source found 0, or the site relaxed the search, or the config is disabled |
| red | a source is blocked or errored |

The amber case is the point. Today, a configuration that has quietly returned nothing for four
days looks identical to a healthy one.

- **"All configurations"** sits at the top and is the default.
- **Selection lives in the URL** (`?config=<id>`), so a scoped view is linkable, and it survives
  switching market tab, changing filters, and moving between Listings and Changes.
- **A filter box** at the top of the sidebar narrows the list. With a handful of entries this is
  client-side; no round trip.
- **Below ~900px** the sidebar collapses to a horizontal chip row above the content, so the
  page stays usable on a narrow window.

## 6. The configuration page

New route: `/config/<id>`. This is where clicking a configuration's name takes you, and it is
the answer to "how is this configuration doing?".

- **Header** — name, its metadata as chips, enabled state, and edit / duplicate / archive.
- **Sources** — one row per site: last run, status, counts, and a link to the existing
  per-source detail page. This is the Runs health table, moved to where it belongs.
- **Its cars** — the listings grid, scoped.
- **Its recent changes** — the feed, scoped.
- **Its price summary** — median asking price over the week, count of drops and arrivals.

## 7. Create and edit

A form covering name, the display metadata (make, model, trim, drivetrain, year and price
range, currency), enabled, and a repeatable list of `(site, pasted URL)` sources.

Validation reuses what `carwatch/config.py` already enforces: a known site, a plausible URL for
that site, and the `PASTE_URL_HERE` placeholder actually replaced.

**"Test this URL" is the feature that earns this phase.** A button that runs the adapter once
against a pasted URL and reports what it would find — how many listings, and whether the site
relaxed the search — writing nothing. A URL that silently returns zero is the single most
common way a configuration fails, and today you only discover it after a full collection. It
also gives an honest answer before you commit a bad URL to the file.

**Duplicate** pre-fills a new configuration from an existing one, which is how the "same but
2024+" variant in the current config came about in the first place.

## 8. What gets retired

| Now | After |
|-----|-------|
| Listings toolbar `search` select (by name) | sidebar |
| Changes toolbar `search_id` select (by name + site) | sidebar |
| Runs health table | moves to the configuration page |
| `/search/<id>` | stays, as the per-source drill-down reached from a configuration |

The two dropdowns disagree with each other today — one keys on the configuration, the other on
the configuration *and* site. Collapsing both onto one sidebar selection removes that.

## 9. Improvements worth taking, beyond the ask

Ranked by value against what a week of real data has actually shown.

1. **The health dot, and the silent-zero alarm** (§5). Grounded in a live problem, not a
   hypothetical: four days of a configuration returning nothing with no visible signal.
2. **"Test this URL" before saving** (§7).
3. **Per-configuration price trend.** Median asking price over time, per configuration. This is
   the question the whole tool exists to answer — "is this segment getting cheaper?" — and it
   only became answerable once there was a week of history behind it.
4. **"Also in: <other configuration>" on a card.** A car matching two configurations is already
   merged into one card by the view layer; naming the other configuration makes an invisible
   relationship visible, and it is nearly free given `search_ids` is already on the merged record.
5. **Archive, never delete.** Removing a configuration must disable it, not drop rows. Its
   listings and price history are the thing this week has been spent accruing, and a delete
   would be irreversible. If real deletion is ever offered it must state the number of price
   observations it destroys and require confirmation. **This is a safety rule, not a
   preference.**
6. **Export a configuration** as a YAML snippet — useful for moving one between machines, and a
   natural fallback if the write path ever misbehaves.
7. **Keyboard switching** between configurations. Cheap, and this is a keyboard-at-a-desk tool.

Deliberately *not* proposed: per-configuration collection cadence. It is plausible but nothing
observed so far calls for it, and it would complicate the run loop.

## 10. Build phases

Each ships on its own and leaves the app working.

1. **The config writer.** ruamel round-trip, mtime guard, validate-then-atomic-replace, backups.
   Fully tested, no UI. Nothing else can be trusted until this is.
2. **The sidebar**, with URL-carried selection on Listings and Changes; retire both dropdowns.
   Read-only — no editing yet.
3. **The configuration page** (`/config/<id>`): sources and health, scoped cars, scoped changes.
   The Runs health table moves here.
4. **Create / edit / duplicate / archive**, including "Test this URL". This is the first phase
   that writes to disk from the browser.
5. **Price trend and the health dot**, plus the "also in" cross-reference.

## 11. Open questions — answered 2026-09-08, and built

- **Terminology.** *Accept the split and document it.* The UI says "Configuration"; the code
  and database keep `Search`, `search_id` and the `search` table. Renaming the table would
  rewrite the rows every listing and price observation hangs off for no functional benefit.
  `carwatch/web/configurations.py` is the seam that translates, and its module docstring plus
  a README section state the mapping.
- **Archive vs delete.** *Archive only.* There is no delete route, and a test asserts there
  isn't one. Removing a configuration disables it in `config.yaml`; `sync_searches` then
  disables its `search` rows and its listings and price history stay untouched. Restoring is
  one click.
- **After saving a new configuration.** *Collect it immediately.* Saving writes the file,
  re-reads it, and starts a scoped background collection for what was just saved, so a new
  configuration proves itself at once instead of waiting for the next manual run. The
  redirect does not wait for it — the existing run strip reports it.
- **Disabled configurations in the sidebar.** *Hidden behind a "show archived" toggle*,
  mirroring how Changes hides the initial listings. The count of hidden ones is always on
  screen, and a configuration you are currently scoped to is always listed even when archived.

## 12. Build log

All five phases are built, each with its own tests. 552 tests pass.

| Phase | What shipped | Where |
|-------|--------------|-------|
| 1 | The config writer: ruamel round-trip, mtime+size guard, collection-lock guard, validate-then-atomic-replace, timestamped backups, export | `carwatch/config_writer.py`, `tests/test_config_writer.py` |
| 2 | The sidebar, URL-carried scope on Listings and Changes, both dropdowns retired | `carwatch/web/configurations.py`, `templates/partials/sidebar.html` |
| 3 | The configuration page; the Runs health table moved onto it | `templates/configuration.html` |
| 4 | Create / edit / duplicate / archive / restore / export, and "Test this URL" | `templates/configuration_form.html`, `templates/partials/url_test.html` |
| 5 | The 14-day median price trend, the health dot, the "also in" cross-reference | `_price_trend`, `_also_in` in `web/routes.py` |

Three things were found by building rather than by planning:

1. **"All configurations" cannot sum the per-configuration counts.** A car matching two
   configurations belongs to both, so adding them up claimed 219 cars where the pages could
   only show 211. The total is now computed over the whole set once.
2. **"Also in" must exclude archived configurations.** Naming one would be a claim about the
   past dressed as the present — and it links to a scope the sidebar deliberately hides.
3. **A `<form>` cannot live inside a `<p>`.** The parser closed the paragraph early and the
   Archive and Run buttons fell out of the action row.

### Follow-up, same day

`/search/<id>` was the loose end §8 left: it stayed as the per-source drill-down, but it was
the one page the new navigation dropped you out of — no sidebar, a breadcrumb back to Listings
that never named the configuration you had come from, "Run this search now" in the old
vocabulary, and the same invalid `<form>`-inside-`<p>` nesting fixed elsewhere. It now carries
the sidebar, names and links its configuration, states that source's health note in the same
words the dot uses, and offers "Edit this URL".

That surfaced two more:

- **The sidebar built its links from the current path**, so on `/search/<id>` — and on a
  configuration's own page — selecting an entry produced `/search/7?config=juke` or
  `/config/a?config=b`, which kept you where you were. Pages with nothing to scope now opt into
  a *navigate* mode; Listings and Changes still scope in place.
- **There was no base `a` rule** in the stylesheet — links are styled per context, so any plain
  prose link fell back to the browser's default blue, unreadable on the dark palette. It
  already affected two empty states. One rule, which every contextual rule outranks.

Deliberately still not done, as §9 said: no per-configuration collection cadence, and no
keyboard switching (§9.7) — nothing observed so far calls for either.

### Robots-disallowed URLs

`carwatch/site_rules.py`. The rules were real and written down — in an adapter docstring and a
`config.yaml` comment — but a form that lets you paste and save in one click is a form you can
use without ever reading them. So they became code, consulted by both the form and "Test this
URL".

The design decision that took the thinking: **robots-disallowed is not the same as refused.**
mobile.de's search route is disallowed, and that was put to the user and accepted for personal
low-volume use. A checker that refused everything disallowed would have contradicted a decision
already taken and broken the configuration collecting today — so there are three levels, and
mobile.de's route is a `NOTE` that states the fact every time without blocking. What is
`BLOCKED` is what nobody has accepted: autovit's `_price` and `/api/`, `suchen.mobile.de`'s
Akamai 403, and mobile.de's id-less category page.

Two limits held deliberately: `load_config` is untouched, so a hand-edited config still loads
and collects; and a *saved* blocked URL shows red with its reason on the configuration page,
rather than being quietly fetched. A regression test asserts the live `config.yaml` is not
refused by any of it.

### The sidebar row, as §5 and §6 actually specify it

§5 says selecting a configuration scopes the view; §6 says clicking its *name* opens its page.
The first build had the row scoping and a small chevron opening — which put the page §6 calls
the answer to "how is this configuration doing?" behind the smallest target in the sidebar.

Now the name is the link to `/config/<slug>` and the count beside it is the scope control, with
a border at rest so it reads as a control rather than a label. It toggles: clicking the count of
the configuration you are already scoped to clears the scope, so there is a way out that is not
"find the All configurations row". On pages with nothing to scope the count is a plain span.

### Form input hardening

Currency became a choice (EUR / RON, plus whatever the file already says, so editing never
rewrites a value the form merely does not know). From/to pairs are refused when reversed, prices
when negative, years when a digit has slipped — in the writer, not in `parse_search`, on the
same reasoning as the site rules: display-only fields must never stop an existing config loading.

**A correction to what prompted this.** The case made for it twice was that `_eur_prices`
silently drops anything that is not `"EUR"`, so a typo in the currency box would make cars
vanish from every median. That was wrong: the config's `currency` is display-only and never
reaches a `Listing`. Medians filter on the currency the *adapter scraped*, which no form
controls. The select is worth having for consistency in a field the UI shows; it fixes no bug.

The real gap that description belongs to is still open: a configuration collecting RON-priced
olx ads has those cars excluded from every median with nothing on screen saying so. Worth a line
in the totals ("N in RON not counted") if it ever bites.

### The sidebar's width

220px was the plan's sketch and it truncated every configuration name to an ellipsis. Now 280px,
with `main` widened from 1200 to 1320 so the cars beside it did not pay for it.

---

## 13. Creating a configuration (2026-09-09)

Two doors into the form: describe it and have the URLs built, or paste one link and have the
rest built from it. Decided with the user: description means the fields the form already has,
and anything needing an id that cannot be derived from a name asks for a URL outright.

### What the investigation found

Building a URL is a guess. autovit and olx name a model with a slug, and the only way to know
theirs is to try it — Nissan Juke, Dacia Duster and Skoda Octavia all worked first time. The
problem is the guess that misses:

| URL | returns | actually |
|-----|---------|----------|
| `nissan/juke/de-la-2020` | 29 | all Jukes |
| `nissan/not-a-real-model/2020` | 820 | assorted Nissans |
| `mercedes-benz/a-klasse/2020` | 4631 | first one a CLE 53 AMG |
| olx, same A-Klasse search | 40 | a trailer, Smart parts, **a laptop** |

None of those failed. No 404, no zero, and — the part that matters here — autovit's own
`relaxation` flag stayed unset, so the health dot, the run note and the "Test this URL" button
would all have called them fine. `verify.py` therefore judges a URL by the cars it returns, not
by whether it returned: two thirds of the first page must be the model asked for.

mobile.de was the other unknown. Its makes turn out to be embedded in the search page (619
pairs, Nissan 18700), but its models are not — they load once you pick a make. Per the user's
decision, it asks for a URL rather than falling back to a make-only search.

### Two bugs this turned up in already-shipped code

1. **"Test this URL" fetched the entire search.** Its docstring claimed "one page, not the whole
   paginated search"; `fetch_listings` paginates to its cap, which is fifty pages on autovit.
   `max_pages` now makes the sentence true, and collection is untouched — it never passes it.
2. **"Test this URL" was broken for olx.** The route is `async`, olx renders through
   Playwright's *sync* API, and sync Playwright refuses to run inside a running asyncio loop.
   Every press against olx returned "use the Async API instead" — on the one site the button is
   most needed for. Both routes now run the adapter in a threadpool.

---

## 14. Links only, and delete (2026-09-10)

Two reversals, both the user's call.

**Creation is links only.** The description builder and "paste one link, build the others" from
§13 are gone: `/config/build`, `verify.py` and the URL builders in `search_urls.py` were
removed. Reason: §13's own finding — a wrong generated URL widens silently, and verifying the
returned cars catches a wrong model but not a dropped year, trim or drivetrain. What stayed:
reading a pasted link back into the description (`/config/read-link`, blank boxes only, fetches
nothing) and "Test this URL".

**Delete exists, behind archive.** This reverses §11's "archive only". Delete is offered only on
an archived configuration and goes to a confirmation page that counts the rows first. It
removes the config block (`config_writer.delete_search`, backed up) and then every row the
configuration owns (`db.purge_configuration`, under the collection lock: events, price history,
listings, runs, search rows). Refused while collecting, on a stale stamp, and for the last
configuration. A configuration already removed from the file by hand can still be deleted from
the database.

One thing this turned up: ruamel attaches a comment to the node *before* it, so a configuration's
heading lives on the block above, and the file's footer lives on the last block. A naive delete
loses the neighbour's comment and keeps its own. `_remove_block` moves the trailing comment up
into the deleted block's place; tested against the real config.yaml in both directions.

**Later the same day: the form is a name and the links.** The description boxes and the
"Collect this configuration" checkbox were removed at the user's request. The description
filtered nothing (only the chips on a configuration's page and `--status` read it), and could
contradict the URLs; it is now read off the links on save (`routes._description`) — kept
byte-identical when the links are unchanged, re-read when they change, with keys no link states
carried over. The checkbox duplicated Archive/Restore; a new configuration always collects, an
edit keeps its state, and saving an archived configuration no longer starts a collection that
`collect()` would silently skip.

**And four follow-ups (threading was considered and left out).**

- *Database backup before delete.* `db.backup_database` copies the whole database through
  SQLite's online backup API (WAL mode means a file copy can miss committed pages) to
  `carwatch.db.bak-<stamp>`, keeping eight. Taken first, so a failed backup stops the delete
  with nothing touched. The confirmation page says how to undo.
- *Non-EUR prices named, not silently dropped.* Medians and ranges stay EUR-only; Listings, the
  configuration page and the per-source page now say "(1 priced in RON left out)". The
  per-source page's median also mixed currencies and took the upper-middle value; both fixed.
- *Collection progress.* `engine.Progress` is filled in as a collection runs, and each adapter
  reports the page it is about to fetch (`adapters.base.report_page`). The status strip reads
  "MOBILE.DE · Nissan Qashqai 2025 4x4 Tekna · page 4 (3 of 5)".
- *Description on the edit page*, read-only, since it can no longer be typed.

Threading across sites was measured and deferred: collections take ~2 min, mobile.de ~85 s of
it (mostly its deliberate 5–12 s pauses), so per-site threads would save ~35 s.

