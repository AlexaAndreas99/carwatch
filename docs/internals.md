# CarWatch internals

How it works underneath: the state of the build, what the dashboard does to `config.yaml`, how each site is read, and how a run decides what changed. For what CarWatch is and how to run it, see the [README](../README.md).

## Status

**All nine phases complete.** autovit.ro, olx.ro and mobile.de/ro feed a shared database with
price history and delisting detection, and a local dashboard reads it back. Collection is
manual - from the dashboard or the CLI - with an optional schedule (off by default) set from
the Runs page.

Notifications (spec §15) were deliberately not built: they are optional, off by default, and
the dashboard Changes feed already shows price drops as soon as a run finishes.

| Phase | What | State |
|-------|------|-------|
| 1 | Skeleton + DB | done |
| 2 | autovit adapter | done |
| 3 | Collector + diffing | done |
| 4 | Dashboard v1 | done |
| 5 | olx adapter | done |
| 6 | Changes feed + charts + "Run now" | done |
| 7 | Scheduling | done |
| 8 | mobile.de adapter (best-effort) | done |
| 9 | Polish (notifications declined) | done |

## Configuration, in depth

### Configurations, and the word `search`

The dashboard calls one of these blocks a **configuration**; the code and the database call it
a **search**. They are the same thing seen from two sides, and the split is deliberate:
renaming the `search` table would rewrite the rows every listing and price observation hangs
off, for no functional gain. So `Search`, `search_id` and the `search` table stay as they are,
and the UI says "configuration" throughout.

The mapping is one-to-many. One configuration with three `sources:` becomes three `search`
rows, one per site, so a single site being blocked only takes down its own row.
`carwatch/web/configurations.py` puts them back together for the UI, keyed on the name, which
is what `config.yaml` keys on too. URLs carry a slug of that name
(`?config=nissan-qashqai-2024`) rather than a database id, because a configuration has no
single id — it has one per site.

### Editing `config.yaml` from the dashboard

You can create, edit, duplicate and archive configurations from the browser as well as by
hand. `config.yaml` stays the single source of truth either way — the dashboard writes *this
file*, and the collector reads it exactly as before.

Because 61% of the shipped `config.yaml` is comments, and those comments carry real decisions
(the mobile.de robots.txt note, why the autovit URL has no price filter), the writer uses
`ruamel.yaml` in round-trip mode and mutates nodes in place. An edited URL keeps the paragraph
above it. Every write also:

- **refuses if the file changed on disk** since the form was opened, so the dashboard cannot
  silently clobber an editor window you had open;
- **refuses while a collection is running**, because the collector loads the config at the
  start of a run and rewriting it underneath one gives you a half-applied change;
- **validates before committing** — the new content is written to a temporary file, loaded
  back through the real validator, and only then moved into place, so a config the app cannot
  parse never becomes `config.yaml`;
- **replaces atomically**, so a crash mid-write cannot truncate the file;
- **keeps a timestamped backup** (`config.yaml.bak-YYYYMMDD-HHMMSS`, the most recent eight).

`settings:` is deliberately not editable from the browser. Pacing and politeness stay
hand-edited, which keeps a bug in the form from being able to change how hard CarWatch hits a
site.

The form also refuses a from/to pair that is the wrong way round (`year_min: 2025` with
`year_max: 2024`), a negative price, and a year with a slipped digit. These are display-only
fields, so such a configuration collects perfectly well — it just renders a pair of chips that
cannot describe anything, which is worse than an error. Like the site rules, the check lives
where the dashboard *writes*: `load_config` is untouched, so a file you hand-edited that way
still loads. Currency is a choice rather than a box, so it cannot drift into "eur" / "EURO";
a value already in the file that isn't offered is kept as an option of its own rather than
silently rewritten.

### What CarWatch will not fetch

Each site's robots.txt and routing rules used to live in prose — a docstring in
`adapters/mobilede.py`, a comment in `config.yaml` explaining why the autovit URL carries no
price filter. That was enough while adding a source meant hand-editing the file the comment was
in. It stopped being enough once you could paste a URL into a form and save it in one click,
having never read the paragraph. `carwatch/site_rules.py` turns that prose into code, and both
the form and "Test this URL" consult it.

It has three levels, and the difference between them is the point:

- **Blocked** — CarWatch will not fetch this, so saving is refused and no request is made.
  Today: an autovit URL containing `_price` or under `/api/`, both robots-disallowed;
  `suchen.mobile.de`, which answers 403 behind Akamai and which we will not work around; and
  mobile.de's `/ro/automobil/` category page, which renders listings but carries no advert ids,
  so every run would report the whole page as new.
- **Warn** — probably not what you want, but yours to decide. A mobile.de URL that isn't a
  `/ro/vehicule/cautare.html` address.
- **Note** — context, not a problem. The important one: mobile.de's search route *is*
  robots-disallowed, and you were shown that and accepted it for personal, low-volume use.
  Refusing it here would contradict a decision already taken and break the configuration that
  has been collecting all week. It says so every time; it does not block.

Two deliberate limits. **`load_config` is untouched**, so a URL you hand-edit into `config.yaml`
still loads and collects exactly as before — a rule about what we *ought* to fetch must never
stop the app reading its own configuration. And a source whose saved URL is blocked shows as
**red on its configuration page with the reason**, so it is visible rather than silently
fetched. (Consequence worth knowing: editing a configuration whose URL is blocked will refuse
until you fix the URL. Archiving it does not — that path skips validation on purpose.)

### Creating one: paste the links

The form is a name and three link boxes, nothing else. Every source is a URL you pasted from
the site's own address bar: run the search on autovit, olx or mobile.de, set the filters you
want, copy the address bar into that site's box. Pasting a link shows what it reads as (make,
model, years, price cap) and fills in the name if it is still empty. Nothing is fetched to do
that; *Test this URL* is the button that asks the site.

The description shown on a configuration's page is read off the links when you save — each
detail from the first link that states it. Save with the links unchanged (a rename, say) and
the description is left exactly as it is; change a link and it is re-read, keeping anything no
link states, such as a trim you wrote into `config.yaml` by hand. The edit page shows the
description read-only, so a detail read wrongly is visible; correcting one means editing
`config.yaml`. Whether a configuration
collects is not on the form either: a new one always does, and after that Archive and Restore
decide.

**CarWatch does not build URLs, on purpose.** It did, briefly, and that was withdrawn because a
wrong built URL does not fail. autovit and olx name a model with a slug, and an unrecognised
slug does not 404, does not return zero, and does not set autovit's own relaxation flag — it
quietly widens the search. Measured live:

| URL | returns | actually |
|-----|---------|----------|
| `autovit /nissan/juke/de-la-2020` | 29 | all Jukes |
| `autovit /nissan/not-a-real-model/2020` | 820 | assorted Nissans |
| `autovit /mercedes-benz/a-klasse/2020` | 4631 | first one a CLE 53 AMG |
| `olx` the same A-Klasse search | 40 | a trailer, some Smart parts, a **laptop** |

Checking the returned cars caught a wrong *model*, but nothing could catch a year, trim or
drivetrain the URL failed to carry. A link you pasted is a search you have already seen return
the right cars.

### Archive, then delete

**Archive** stops a configuration collecting and keeps every listing and price observation it
gathered. It is hidden from the sidebar behind "show archived", keeps its own page, and
**Restore** brings it back with its history intact.

**Delete** is offered only on an archived configuration, so losing history always takes two
decisions. It opens a page that counts what will go — listings, price observations, runs,
change events — and then removes the configuration's block from `config.yaml` *and* every one
of those rows. Before anything goes, the whole database is copied beside itself as
`carwatch.db.bak-<date-time>` (the last eight are kept), and `config.yaml` keeps its own
timestamped backup. To undo a delete, stop the dashboard and copy that database file back over
`carwatch.db` — which also rolls back anything collected since. Cars another configuration
also found stay under that configuration with their own history.

It refuses while a collection is running, if `config.yaml` changed since the page was opened,
if the database backup fails, and for the last configuration in the file (CarWatch will not load an empty `searches:`).
Comments are handled with care: the paragraph above the deleted block goes with it, and the one
below it — the next configuration's heading, or the worked example at the foot of the file —
stays.

## The dashboard and the CLI, in depth

```powershell
.\run.ps1 --status       # parsed config + current DB state
.\run.ps1 --init         # create the database and exit
.\run.ps1                # collect all enabled searches
.\run.ps1 --all --site autovit          # just one site
.\run.ps1 --search "Nissan Qashqai 2025 4x4 Tekna"
```

Exit codes: `0` every source ok, `1` bad arguments, `2` bad config, `5` at least one source
was blocked or errored (the rest still collected), `6` another collection was already running.

On macOS/Linux use `./run.sh` with the same arguments. Or call the module directly:

```bash
python -m carwatch.collect --status
python -m carwatch.collect --search "Nissan Qashqai 2025 4x4 Tekna"
python -m carwatch.collect --backfill-images    # see Photos, below
```

### Dashboard

**The easy way: the CarWatch icon.** On Windows, `.\install-shortcut.ps1` puts a CarWatch icon
on the desktop and in the Start menu (`-Remove` takes them away; re-run it after moving the
folder). Double-clicking it starts the dashboard if it is not already running — in a minimised
window titled *CarWatch dashboard*; close that window to stop it — and opens
http://127.0.0.1:8009 in your browser. Double-clicking again while it runs just opens the
browser. On a Mac, `CarWatch.command` does the same from Finder (once: `chmod +x
CarWatch.command`; written for macOS, not yet run on one).

Or by hand:

```bash
python -m carwatch.web              # http://127.0.0.1:8000
python -m carwatch.web --port 8080
python -m carwatch.web --reload     # auto-reload while developing
```

Binds to localhost only. A **permanent sidebar** on Listings and Changes lists your
configurations with their active car count and a health dot:

| Dot | Meaning |
|-----|---------|
| green | every source collected cleanly on its last run |
| amber | a source found 0, or the site relaxed the search, or the configuration is archived |
| red | a source is blocked or errored |

The amber case is the point of it. Before this, a configuration that had quietly returned
nothing for four days — because autovit relaxes the strict Tekna search — looked identical on
screen to a healthy one. Hovering the dot says which source, and why.

Each row has two targets. **The name opens that configuration's page**; **the count beside it
scopes the current page** to it, and clicking the count of the one you are already scoped to
clears the scope again. The scope lives in the URL (`?config=<slug>`), so a scoped view is a
link, and it survives switching market tab, changing a filter, and moving between Listings and
Changes. On pages with nothing to scope — a configuration's own page, its form, one of its
sources — the count is just a count and selecting a row navigates.

That replaced two toolbar dropdowns which disagreed with each other: Listings picked a
configuration by name, Changes picked a configuration *and* a site.

Five pages:

- **Listings** (`/`) — the home page. One card per *car*, not per database row: autovit and
  olx carry largely the same Romanian inventory, so an ad appearing on both is shown once with
  a badge for each site (see **Merging**, below). Germany is a separate tab, because mobile.de
  is different inventory at a different scale. Every listing has a photo. Toggle between a
  **card grid** and **compact rows** — the choice is remembered in your browser (in a cookie,
  so the server can render only the view you are actually using; emitting both and hiding one
  with CSS made the Germany tab 528KB of HTML for 173 cars). Filter by
  text, price range, year range, max mileage, fuel and search, and sort by newest, price,
  mileage, year or biggest drop; all of it lives in the query string, so a filtered view is a
  link you can keep. A price drop gets a green corner flag, a green left border and a green
  sparkline, and nothing else on the page uses that colour.

  Number boxes have no spinner arrows: every number here is a price, a year or a mileage —
  values you type rather than nudge, since stepping a price by 1 EUR is meaningless, and the
  arrows ate enough width from the paired from/to boxes to make them look broken. They are
  still `type="number"`, so a phone offers a numeric keypad.

  **The toolbar filters as you type.** A 250ms debounce means one request per pause rather than
  one per keystroke, and only the results are replaced — the toolbar itself is outside the
  swapped region, so the box you are typing in is never re-rendered and never loses the cursor.
  That is the part a full-page reload could not manage: reloading on every keystroke stole the
  focus, so it only reloaded when you *left* the field, which made filtering feel like it needed
  an Apply button. The market tabs and the "clear" link come back as out-of-band swaps, because
  each tab's href carries the filters and switching market would otherwise drop what you just
  typed. The address bar still gets the tidy canonical URL, not the form's serialisation with
  every empty box in it.
- **Changes** — the "what changed since I last looked" feed: new listings, price drops and
  rises with their delta and percentage, and delistings, newest first, each with its photo.
  **Folded into one collapsible block per collection**, newest open and the rest closed, each
  headed with its time, a tally (▼2 · 4 new · 5 delisted) and the sites that reported. There is
  no collection id in the database — `collect()` writes one run per search and site and nothing
  ties them together — so the grouping is reconstructed by clustering run start times; runs less
  than 30 minutes apart are one collection, which is what a three-site run actually looks like.
  Filter by kind (drops only, all price changes, new, delisted), and scope it with the
  sidebar. Price drops get
  the one tinted row, because that's the event worth acting on.

  **The first run for each search is excluded.** When you start watching a search, every
  listing on it is "new" — that is the starting position, not a change, and it swamps the page:
  on the first day here it was 215 rows of baseline hiding the 1 thing that had happened. Those
  arrivals are always accounted for on screen ("show 215 initial listings") and one click
  brings them back.

  The baseline is each search's **first successful run**, and it never moves — later runs can't
  shift it, or the feed would end up hiding everything. Two details matter. It is per search,
  so adding a fourth site to `config.yaml` gives that source its own baseline instead of
  reporting its whole inventory as news. And it is anchored on the first run that *completed*,
  not the first that found something: a search can collect cleanly and legitimately find
  nothing for days (autovit relaxes the strict Tekna search, so it has returned 0 on every run),
  and the day real matches appear they are the most newsworthy thing that search can do — not
  a baseline to be hidden. A blocked or errored first run is still skipped, since it establishes
  nothing about what was there.
- **Favorites** (`/favorites`) — the cars you starred with ☆, shown exactly as Listings shows
  cars: same toolbar, market tabs, cards or rows, and sidebar scope. Every card and row on
  Listings has the star; it toggles in place. A favorite is kept against the *ad* (site and
  the site's own ad id), not a database row, so a star on olx's copy of an autovit ad stars
  the merged card, and a star survives its configuration being deleted — such favorites are
  listed under "No longer tracked" with their title, link and a Remove button. Sold favorites
  stay on the page in their own section. Unstarring on Favorites fades the car until the page
  reloads, so a slip is one click to undo.
- **Configuration** (`/config/<slug>`) — everything about one configuration in one place. Its
  metadata; its **sources** table (active count, last run status, that run's
  found/new/price/delisted counts, and blocked, errored or relaxed sources called out by
  reason, so a scraping problem can't hide behind a zero); this week's figures; a **median
  asking price** trend over the last fourteen days; its cars; and its recent changes. Edit,
  Duplicate, Export YAML, Archive (Restore and Delete once archived) and "Run this now" sit
  at the top. The sources table used to
  live on Runs — it moved here, because "is this configuration collecting?" is a question about
  the configuration.

  The trend is the question the tool exists to answer — *is this segment getting cheaper?* —
  and it only became answerable once there was a week of history. It is a median (a mean would
  follow one mispriced ad) over cars actually listed that day, and days with fewer than three
  cars are left out rather than drawn as a spike. The 7-day figure beside "median asking"
  compares the same cars then and now, so it does not report the expensive ones selling as
  prices falling.
- **Runs** — the full run log, newest first, with blocked and errored runs called out so a
  scraping problem stays visible.
- **Search detail** (`/search/<id>`) — one source's raw, unmerged listings in a sortable,
  filterable table, with the same photos and sparklines as everywhere else. Reached from Runs
  and Changes; useful when you want to see exactly what a single site returned, at that site's
  own prices, rather than the merged view. It carries the sidebar, names the configuration it
  belongs to, and repeats that source's health note in the same words the dot uses — it is a
  drill-down *from* a configuration, not a page you should have to find your own way out of.

  On this page, and on a configuration's own page and its form, selecting an entry in the
  sidebar **opens** that configuration rather than scoping in place: there is nothing here to
  scope, and a link like `/search/7?config=juke` would leave you exactly where you were.
  Listings and Changes, which do have filters worth keeping, still scope in place.

A card that matches more than one configuration says so — "in *A* and *B*", or "also in *B*"
when you are already scoped to *A*. The relationship was always in the data, since a merged
record carries every search it turned up in, but nothing showed it.

Every filter form applies itself — change a field and the page reloads with it applied, no
button to press. (There is an Apply button behind `<noscript>` for browsers without
JavaScript.) The cursor is put back where you left it, so setting a price range and moving on
works the way it looks like it should.

**Test this URL.** The configuration form states each site's robots.txt and routing rules
beside the box you paste into (see **What CarWatch will not fetch**), and has a button that
fetches one page and reports what it would find — how many listings, a sample of them, and whether the site
relaxed the search — writing nothing. A URL that silently returns zero is the commonest way a
configuration fails, and until now you only discovered it after a full collection. Saving a
configuration also starts a collection for it straight away, so a new one proves itself
immediately rather than waiting for the next manual run.

**Run now.** "Run all now" in the header stays one click, because it is what you want almost
every time. The caret beside it opens a picker with a checkbox per configuration — **every box
starts ticked**, so opening it and pressing Run is still a run of everything; it narrows a run
rather than making you assemble one, and ticking all of them is treated as "all". Archived
configurations are not offered, since `collect()` skips them whatever it is asked for. The
picker's contents are fetched when it is first opened, so the topbar needs no database context.

"Run all now", "Run this now" on a configuration, and "Run" on one
of its sources trigger
the same collection code path as the CLI and the scheduler — §10's one implementation, three
triggers. The run happens on a background thread and a status strip polls until it finishes,
then reports what changed and stops polling. Runs are serialised: pressing again while one is
in flight joins the running job rather than starting a second, since §14 asks us not to
parallelise scraping and two concurrent runs would race on the same rows.

The dashboard reads the same SQLite file the collector writes. It's safe to leave running
during a collection: the DB is in WAL mode, so reads don't block writes. Searches you add to
`config.yaml` appear immediately as "never run", before their first collection.

htmx and Chart.js load from cdnjs. Chart.js is fetched only when you first expand a row, and
if it can't be reached the row explains that rather than showing an empty box — the history
itself is in the database either way. The small sparkline on a card is plain inline SVG and
needs no library; it is drawn only once a listing has two or more recorded prices, because one
observation is not a trace.

### Photos

Each listing stores an `image_url`. Images are **hot-linked** from each site's own CDN, never
downloaded: no image directory to manage, no cache to invalidate, for a tool whose entire
database is one file. The cost is that a delisted car's photo eventually 404s, and the card
falls back to a neutral placeholder — as it also does for the ads that genuinely have no photo
(on olx, 17 of 47, which carry OLX's own "no thumbnail" graphic). Cards use a fixed 4:3 box
with `object-fit: cover` so mixed aspect ratios can't make the grid ragged, and `loading="lazy"`
so a 173-card page fetches about twenty images rather than 173.

Where a site offers several sizes, CarWatch asks for the smallest one that still fills a card
on a 2x display rather than the largest on offer — mobile.de will happily serve a 1600px photo
for a 268px card.

If you upgrade a database from before photos existed, autovit's images can be recovered from
data already stored:

```bash
python -m carwatch.collect --backfill-images
```

That fills in every autovit listing from the JSON already in `raw_json`, with no re-fetch, and
tells you how many olx and mobile.de rows need a collection run to pick their photos up.

### "New" rather than a blank year

A brand-new car has never been registered, so mobile.de gives it no first-registration date and
no odometer reading — 21 of 173 listings here. Leaving both cells empty reads as a broken
parser, especially since newest-first puts every one of them on the first screen. Listings
therefore carry a `condition` column: `"new"` when the site says the vehicle is unregistered,
and the Year cell says **New** instead of a dash. A year we merely failed to parse still shows
a dash — only a fact we actually established earns the word.

### Merging

autovit and olx are largely the same Romanian inventory: an olx result card for these searches
links straight to `autovit.ro`, so the autovit ad id in the URL
(`…/nissan-qashqai-ID7HQf7M.html` → `7HQf7M`) identifies the same physical car on both sites.
The Listings page groups on exactly that — no fuzzy matching on title, price or mileage — and
the same key also collapses one car that matched two of your searches. Anything without that
id keys on `(site, listing id)` and simply appears on its own. Against live data this turns
87 rows into 42 Romanian cars, with 38 of them carried by both sites.

On a merged card the autovit row is canonical: it has the photo, the structured specs and the
price shown. The sparkline uses whichever site has the longest price history; the two series
are never spliced together, because interleaving them would invent price movements that never
happened.

**Merging is a view, not a migration.** The database still keeps one row per site, each with
its own independent price history, and nothing about rendering a merged page writes to it —
there is a test that asserts the database is unchanged, contents and all, before and after.
Merging in the database would destroy the per-site history already collected and could not be
undone; merging on read is a presentation choice that can be changed at any time.

German listings are never merged with Romanian ones. They are different cars, and the two
markets stay on separate tabs. The Germany tab does mark cars priced below the Romanian median
— treat that as a **list-price** comparison only: German ads are frequently pre-VAT and exclude
import and registration cost, so it is not a comparison of what a car would cost you landed.

### Previewing an adapter

`carwatch.probe` runs a search URL through its adapter and prints the parsed listings without
touching the database. Use it to check a site adapter still works, or to try a URL before
committing it to `config.yaml`:

```bash
python -m carwatch.probe --search "Nissan Qashqai 2025 4x4 Tekna" --site autovit
python -m carwatch.probe --url "https://www.autovit.ro/autoturisme/nissan/qashqai/de-la-2024"
python -m carwatch.probe --url "..." --json --limit 3
```

Exit codes: `0` ok, `3` blocked by the site, `4` adapter/parse error.

## Running it

**CarWatch runs when you tell it to**, unless you turn on a schedule (below) — collect from
the dashboard's "Run now" button, or from the CLI:

```powershell
.\run.ps1                  # collect everything
.\run.ps1 --all --site mobilede
.\run.ps1 --force          # ignore a site's post-block cooldown
```

`run.ps1` / `run.sh` activate the venv, move to the project directory, append everything to
`logs/collect-YYYY-MM.log`, and exit with the collector's own code — so a run always leaves a
trace you can read afterwards.

**Only one collection at a time.** A lock file next to the database makes that true across
processes, not just within one: pressing "Run now" while a CLI collection is going gets a clear
"already running" message rather than two runs diffing against each other's half-written state.
A lock left behind by a killed run is taken over automatically after two hours.

### Optional: a schedule

It is **off unless you turn it on**. The easy way is the **Schedule** panel at the top of the
Runs page: off, once a day at a time you pick, or every 6, 8 or 12 hours from that time.
Nothing more often than every six hours — mobile.de, whose robots.txt disallows the route
CarWatch uses, was accepted on the basis of low volume. Each run starts up to 20 minutes late
at random: a job firing at exactly 08:00:00.000 daily is the most machine-looking traffic
pattern there is.

The panel does not run anything itself. It registers the schedule with the operating system,
so collections happen **whether or not the dashboard is open** — but only while the computer is
**on and you are logged in**:

- **Windows** — a Task Scheduler task, *CarWatch scheduled collection*. A run due while the
  PC was off or asleep happens as soon as it is back on.
- **macOS** — a launchd LaunchAgent (`~/Library/LaunchAgents/ro.carwatch.collect.plist`). A run
  due while the Mac slept happens when it wakes; one due while it was shut down is skipped.
  (The macOS side is tested against the documented launchd format but has not yet been run on
  a real Mac.)
- **Linux** — not offered in the panel; use `./schedule.sh`, below.

**When the next run really is.** Because of the random delay, a run "at 18:50" collects some time
between 18:50 and 19:10, so that is what the dashboard says: a small line in the top bar of every
page reads *Next run today 18:50–19:10*. Once a run has started it has picked its minute — the
wrapper writes it to `logs/scheduled-run.json` — and the line changes to *Scheduled run collecting
at 19:07*, then *collecting now*, and goes back to the next window when it is done. The Runs panel
says the same at more length. The top bar asks the system scheduler at most once a minute (every
20 seconds while a run is under way), so it never slows a page down.

The registration *is* the setting: the panel reads it back from Task Scheduler or launchd each
time, and nothing is stored in `config.yaml` or the database. Scheduled runs collect the
project's own `config.yaml`; the panel warns if the dashboard was started on another file.

The same thing from the command line — the panel calls this script, so the two always agree:

```powershell
.\schedule.ps1 -At 08:00                  # daily
.\schedule.ps1 -At 08:00 -EveryHours 6    # 08:00, 14:00, 20:00, 02:00
.\schedule.ps1 -Status                    # is anything registered?
.\schedule.ps1 -Remove
```

```bash
./schedule.sh --at 08:00      # macOS / Linux, via cron (daily only)
./schedule.sh --status
./schedule.sh --remove
```

On a Mac prefer the panel: cron skips a run the Mac slept through and needs Full Disk Access
for `/usr/sbin/cron`; the panel warns if a `schedule.sh` cron entry exists alongside it.
`-JitterMinutes 0` / `--jitter 0` turns the random delay off. On Windows a scheduled run opens
no window: the task action is `conhost.exe --headless powershell.exe … run.ps1 --all --jitter 20
--scheduled`. conhost reports exit code 0 whatever happened, so `--scheduled` makes the wrapper
record the real result in `logs/last-scheduled-run.json`, which is what the panel shows.
`run.bat` is a cmd.exe shim if a `.bat` is easier to point something at.

**If you schedule it, install Playwright's browsers into the project**, not your user profile.
A Windows scheduled task cannot reliably read `%LOCALAPPDATA%\ms-playwright` — the task's
session sees it as empty, so olx fails every night while working perfectly by hand:

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH="$PWD\.playwright"; .\.venv\Scripts\python.exe -m playwright install chromium
```

CarWatch picks up `./.playwright` automatically from every entry point when it exists.

### Checking on a run

```powershell
Get-Content .\logs\collect-2026-09.log -Tail 20
```

Exit codes: **0** every source collected, **5** at least one was blocked or errored (the rest
still ran), **6** another collection was already in progress, **2** the config is broken. The
dashboard's Runs page shows the same history with reasons.

## How the autovit adapter gets its data

autovit is a Next.js app that embeds its GraphQL (urql) SSR cache in a `__NEXT_DATA__` script
tag on the search page. The adapter parses `advertSearch` out of that — structured JSON, far
more stable than CSS selectors.

It deliberately does **not** call the GraphQL endpoint directly: autovit's `robots.txt` sets
`Disallow: /api/` for `User-agent: *`. The search results page is allowed (`Allow: /`) and
already contains the same data, so we read the page a crawler may read and take the JSON it
embeds — same stability benefit, no robots violation.

Pagination is `?page=N` at 32 results per page, stopping at `totalCount`, on an empty
`edges` list, or if the site starts repeating results. `site_listing_id` is the site's own
advert id (e.g. `7060859828`), which is what makes diffing across runs reliable.

One field is unavailable: **gearbox is not returned in autovit search results** (only on the
ad detail page), so it stays null rather than being guessed at.

### Search relaxation — why a search can report 0 matches

When a search would return nothing, autovit silently **widens** it and returns near-misses
instead. A `year >= 2025` search comes back with 2024 cars, and the page looks normal. The
payload admits it (`relaxation.applied: true`), naming the filter it changed.

The adapter detects this and **discards the results**, reporting 0. Ingesting them would
create phantom `new` events for cars that don't match your search, and then phantom
`delisted` events later when a real match appears and the site stops relaxing. The CLI and
probe print what the site changed, e.g. `year: asked 2025, used 2024`, so a relaxed search is
never confused with a genuinely empty one.

## How the olx adapter gets its data

**olx.ro needs a real browser.** It sits behind CloudFront and returns 403 to every
plain-HTTP request — an honest User-Agent, no User-Agent, and a full Chrome header set all get
the same "Request blocked" page, and even `/robots.txt` is 403. The block is at the
network/TLS-fingerprint layer, not the User-Agent, so no header combination fixes it (and per
§14 we don't impersonate to evade one).

So this adapter follows §4's escalation path: try plain HTTP first (cheap, and the block may
not apply from every network), and fall back to rendering the page in a real headless Chromium
via Playwright. That requires a one-time install:

```bash
pip install playwright
playwright install chromium
```

Without it, olx runs record `status=blocked` with those instructions, and every other site
keeps working normally.

There is no `__NEXT_DATA__` on olx search pages, so listings are parsed from the DOM cards
(`[data-cy="l-card"]`). `site_listing_id` is the card's `id` — OLX's own ad id. Pagination is
`?page=N` at ~50 cards a page; out-of-range pages re-serve the last page, so the adapter stops
when a page adds no new ids, capped at 25 pages.

Two quirks worth knowing:

- **Year comes from the card's params line**, where it sits next to the mileage
  (`2025  21 700 km`). The card text also contains the posting date and often a year in the
  title, so reading "the first year in the card" returns the *listing date* — 2025 cars came
  back as 2026 until this was fixed.
- **make / model / fuel / gearbox are not available** — olx cards carry only free-text titles,
  so these stay null rather than being guessed at from substring matching.

### Duplicates across sites

OLX syndicates autovit.ro ads, so a listing found via olx may be the same physical car as one
found via autovit — its link often points straight at `autovit.ro`. CarWatch stores them as
separate listings, one per search/site, which is deliberate: each site gets its own independent
price history, and sellers do sometimes price the same car differently on each. There is no
cross-source de-duplication.

## How the mobile.de adapter gets its data

**Use a `/ro/vehicule/cautare.html` URL.** Search on mobile.de/ro and copy the address bar —
that is the Romanian search app, and it is the only mobile.de route that works. It is reachable
over plain HTTP with an honest User-Agent (HTTP 200, no challenge, no browser needed), and each
result card is an anchor to `detalii.html?id=<ad id>`, which gives the stable
`site_listing_id` that diffing depends on. Pagination is `&pageNumber=N`.

Two other mobile.de routes look plausible and are dead ends:

- `suchen.mobile.de/fahrzeuge/search.html` — the German search app. Returns **HTTP 403** behind
  Akamai Bot Manager on first contact. §14 forbids working around that, so the adapter never
  touches this host.
- `www.mobile.de/ro/automobil/<make>/vhc:car,ms1:…` — the SEO category page. Renders listings
  but exposes **no ad id at all**: bare `<article>` cards with `href=""` and only a positional
  `data-testid`. Unusable, because listings reorder between runs and positional ids would
  invent `new` and `delisted` events every time.

If the working route ever starts serving an access-denied page, the adapter raises `blocked`
rather than reporting zero — and does not try to get around it.

### robots.txt

`www.mobile.de/robots.txt` disallows `/ro/*/*.html?` for `User-agent: *`, which covers this
search URL. **This is a deliberate, informed exception** — the only one in CarWatch. §14 leaves
that call to the user: *"Scraping may be against a site's ToS even when technically possible;
the user should be aware and accept that risk for personal use."*

What CarWatch does *not* do is relax anywhere else: one run a day, randomized 1.5–4s delays, an
honest User-Agent, a hard 30-page cap, and no attempt to defeat any anti-bot system. autovit's
`Disallow: /api/` is still respected, and the Akamai-protected mobile.de host is left alone.

If you would rather not take that risk, delete the `mobilede` source from `config.yaml` — the
other sites are unaffected.

### Parsing notes

- **Year comes from "Prima înmatriculare 05/2025"**, anchored on that label. A card also
  contains power figures, postcodes, and model designations like `MY24` that a bare year regex
  picks up wrongly. New, never-registered cars legitimately have no year.
- **Only the EUR price is read.** Cards also show a RON conversion derived from EUR at the
  day's rate — tracking it would record exchange-rate drift as price changes.
- **Fuel and gearbox are matched per bullet segment**, not by substring. A `1.3 DIG-T` petrol
  car was coming back as `Electric` because "electric" appears elsewhere in the spec text.
- **Stored URLs are canonicalised** to `detalii.html?id=<id>`. The card href carries
  `searchId`/`refId` UUIDs that change every run plus a copy of every filter, which would churn
  the URL in the database on every collection.
- mobile.de uses two ad-id formats (9-digit and 14-digit). Both are stable; both are kept.

### Staying welcome

CarWatch collects mobile.de against its robots.txt (your informed choice — see above), so it
leans hard the other way on everything else. These are all "give the site no reason to block
us" measures, not "make us harder to spot" ones.

**Per-site pacing.** `settings.per_site` overrides the delay for one site:

```yaml
settings:
  request_delay_seconds: [1.5, 4.0]
  per_site:
    mobilede:
      request_delay_seconds: [5.0, 12.0]
```

mobile.de is the heaviest source — ~9 pages versus 1–2 for the others — and the one collected
against robots.txt, so it runs at roughly a third of the pace. That turns its run from ~25s
into ~80s of very light traffic, once a day. Nothing depends on it finishing quickly.

**Back off after a block.** `settings.blocked_cooldown_hours: 12` means a site that blocked us
is left alone for 12 hours rather than being asked again on the very next run. A site that just
said no is the last one to hammer, and retrying immediately is how a temporary block becomes a
permanent one. No `run` row is written for a skipped source, since nothing was attempted, and —
as with any block — **no listing is touched**. Only `blocked` triggers it; an `error` is
usually ours (a parse break, a network blip) and is retried straight away. `0` disables it.

**Randomised start time.** The scheduler passes `--jitter <minutes>` to the wrapper, which
sleeps a random 0–N minutes before collecting (default 20). A job firing at exactly 08:00:00.000
every single day is the most machine-looking traffic pattern there is, and it means every
CarWatch user would hit these sites at the same instant. Set `-JitterMinutes 0` / `--jitter 0`
to turn it off.

**Rate limits are obeyed, not fought.** An HTTP 429 is treated as "slow down", not "go away":
CarWatch honours `Retry-After` (seconds or HTTP date, capped at 120s so one run can't hang for
hours) and retries. A site that keeps refusing still ends as `blocked` — never as zero results,
which would delist everything.

Unchanged: one run a day, an honest User-Agent, no parallelism, page caps per adapter, and no
attempt to defeat any anti-bot system anywhere.

## Collection and diffing

Per run, per search/site: fetch → diff against last-known state → write listings, price
history, events, and a `run` row.

- A listing is matched across runs on `(search_id, site, site_listing_id)`.
- `price_history` gets a row on first sighting and on every price change — never a duplicate
  row for an unchanged price.
- A listing missing from a **successful** fetch has its `missed_runs` incremented, and is
  marked inactive once that reaches `delist_after_missed_runs` (default 1). Raise it to 2 if
  you see listings flicker in and out.
- A delisted listing that reappears is reactivated, keeping its original `first_seen` and its
  full price history.

**The rule that protects your history:** a fetch that was blocked or errored is *never*
diffed. No listing is touched — not `last_seen`, not `missed_runs`, not `is_active`. A blocked
site is indistinguishable from a site with zero results, so the only safe response is to
change nothing. Sites are isolated from each other too: one being blocked has no effect on the
others' runs.

A run that found nothing because the site relaxed the search is `status=ok` with a note in
`error_message` explaining why, rather than a silent zero.

## Layout

```
carwatch/
  config.py            # load + validate config.yaml
  config_writer.py     # round-trip writes to config.yaml (comments survive)
  search_urls.py       # read a pasted search URL back into the form's description
  site_rules.py        # per-site robots.txt / routing rules for a pasted URL
  db.py                # engine, session, create tables, config->DB sync
  models.py            # SQLModel models: search, listing, price_history, run, event
  collect.py           # CLI entry: python -m carwatch.collect
  adapters/base.py     # SiteAdapter protocol, RawListing, BlockedError
  collector/           # orchestration + diff logic (phase 3)
  web/                 # FastAPI dashboard (phase 4)
  web/configurations.py  # `search` rows grouped as the UI's "configurations"
config.yaml
run.ps1 / run.sh       # venv wrappers, used as the scheduler one-liner
tests/
```

## Responsible use

This is for **personal use at low volume** — a few searches, once a day. It sends an honest
User-Agent, pauses a randomized 1.5–4s between page requests, and doesn't parallelize. It
does **not** attempt to defeat CAPTCHAs, login walls, or anti-bot systems: if a site blocks
us, the run is recorded as `blocked` and the other sites carry on.

Scraping may be against a site's Terms of Service even where it's technically possible.
Check each site's `robots.txt` and ToS; using this tool means accepting that risk for your
own personal use.

Where a rule is known, CarWatch now enforces it rather than leaving it in a comment:
`carwatch/site_rules.py` refuses to save or fetch a URL that a site's robots.txt disallows and
you have not explicitly accepted, and says so with the reason. The one accepted exception is
mobile.de's search route, which is disallowed and which you accepted for personal, low-volume
use — that is stated every time you paste one, and never silently.

