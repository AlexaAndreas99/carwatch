# CarWatch — Front-End Plan

Follow-on to `carwatch-plan.md`, whose nine phases are complete. That plan built the
collection side; this one rebuilds what you look at. Same working method: build in the phases
at the bottom, one at a time, each leaving the app runnable.

---

## 1. Goals

- **Show the car, not just a row.** Photos on every listing.
- **One list for Romania.** autovit and olx are largely the same inventory; merge them so a car
  appears once, showing which sites carry it.
- **Germany stays separate.** mobile.de is different inventory (German imports) at a different
  scale (173 vs 42), so it gets its own tab rather than being blended in.
- **Keep it minimal.** The current look is liked; it is the density and the missing pictures
  that need fixing, not the restraint.
- **Filters that make 173 cars usable** on both markets.

## 2. Non-goals

- No change to how collection works. Adapters, diffing, price history and scheduling are done.
- No SPA framework, no build step. Server-rendered Jinja2 + htmx, as now.
- No cross-market merging. A Romanian ad and a German ad are never the same car.
- No image hosting or thumbnail pipeline (see §4).

## 3. Decisions already made (approved 2026-09-05)

1. **Both card grid and compact rows**, with a toggle. Cards for browsing, compact for scanning.
2. **Photos are in scope** — schema field, adapter capture, backfill.
3. **Merge on the exact autovit ad id only.** No fuzzy title/price/mileage matching.
4. **The autovit price is canonical** on a merged card.
5. **Merging is view-only.** The database keeps one listing row per site, with its own
   independent price history.
6. **Filters on both tabs**, not just Germany.

Decision 5 is the load-bearing one. Merging in the database would destroy the per-site price
history already collected, and it is not reversible. Merging in the view is a presentation
choice we can change any time.

## 4. Photos

`listing` gains one nullable column: `image_url`.

| Site | Where the image comes from | Backfill |
|------|---------------------------|----------|
| autovit | already in `raw_json` — `thumbnail.x1` (320x240) and `.x2` | **From the existing DB**, no re-fetch |
| olx | `img` inside the result card (`frankfurt.apollo.olxcdn.com`) | needs one collection run |
| mobile.de | `img` inside the result anchor (`img.classistatic.de`) | needs one collection run |

**Images are hot-linked, not downloaded.** No storage, no cache invalidation, and the URLs are
CDN-stable. The cost is that a delisted car's photo eventually 404s — acceptable, and the card
falls back to a neutral placeholder. If a site ever blocks hot-linking we can revisit; caching
would mean managing a growing image directory for a tool whose whole database is one file.

Cards use a fixed 4:3 box with `object-fit: cover` so mixed aspect ratios don't break the grid,
and `loading="lazy"` so a 173-card page doesn't fetch 173 images at once.

## 5. The merge layer

Pure presentation, living beside the existing read helpers in `web/`. Nothing writes.

**The key.** olx cards for these searches link straight to `autovit.ro`, so the autovit ad slug
in the URL (`.../nissan-qashqai-ID7HQf7M.html` -> `7HQf7M`) identifies the same physical car on
both sites. Measured against live data: **45 of 47 olx listings link to autovit, and 38 match an
autovit listing exactly.** Anything without that slug keys on `(site, site_listing_id)` and
simply appears on its own.

**The merged record** carries:

- the canonical row (autovit when present — it has the photo, structured specs and the price)
- `sources: {site: url}` so the card can badge and link every site the car is on
- the longest available price history, so the sparkline uses whichever site has watched longest

**Same car, two searches.** An ad matching both of your searches currently produces two listing
rows. The same key collapses those too, so "All searches" shows it once.

**Expected result today:** 87 rows -> 42 Romanian cars. Germany is unaffected at 173.

**What this must not do:** change any `listing`, `price_history`, `event` or `run` row. The
merge is computed on read. A test asserts the DB is byte-identical before and after rendering.

## 6. Pages

| Route | Purpose |
|-------|---------|
| `/` | **Listings** — the new home. Market tabs, filters, cards/compact. |
| `/changes` | Restyled, merged, and baselined — see the note below the table. |
| `/runs` | Run log, plus the per-search/per-site health table that Overview used to own. |
| `/listing/<id>/history` | Unchanged htmx fragment. |

Overview disappears as a separate page. Its useful half (which sources are healthy, when they
last ran, what the last run found) belongs with Runs; its other half — counts per search — is
better answered by the Listings page itself.

**`/changes` did not stay unchanged in function.** The plan wrote that before the merge
layer existed. Once Listings honestly showed 42 cars, the feed still showed 87 rows for them
— 38 cars repeated two or three times, on the screen you look at most. It now groups the same
way: one row per real change, badging every site that reported it, quoting autovit's numbers so
the feed and the cards never disagree.

The rule for "the same change reported twice" versus "two changes" is **not** a time window.
That was tried first and is wrong: the duplicates in this database are ~130 minutes apart,
because autovit and olx get collected in separate runs, and since collection is manual that gap
is whatever the user makes it. Instead, duplicates are recognised by coming from *different
listing rows* for the same car — which is what produces them — while a second real change
necessarily produces another event on the *same* row. A group therefore takes at most one event
per row, and a repeat starts the next group. No clock involved, so it holds whether the sites
were collected minutes or days apart.

**The first run's arrivals are not changes** (added 2026-09-06, day 2 of collection). Every
listing is "new" the moment you start watching a search, so the feed opened on 215 baseline
rows and 1 real event. Those are now hidden by default, per search, anchored on that search's
**first successful run** — with the count always visible and one click to restore them.

Anchoring on the first run that produced *events* was tried first and is subtly wrong: a search
that collects cleanly but finds nothing (autovit relaxes the strict Tekna search, so it returns
0 every run) would have had no baseline until real matches finally appeared, and would then have
hidden them as "already there". A blocked or errored first run is still skipped. The baseline
never advances: later runs cannot move it, or the feed would hide everything forever.

**Toolbar, on both tabs:** search text, price min/max, year min/max, max mileage, fuel,
sort (price, mileage, year, biggest drop, newest). A search picker defaults to "All searches".
Filter state lives in the query string so a filtered view is linkable; the cards/compact choice
is per-viewer and lives in `localStorage`.

## 7. Visual language

Carried over from the mockup, which was built against real data:

- **One accent, one signal colour.** Blue for interaction, green reserved for price drops. A
  drop gets a corner badge and a green left border; nothing else competes.
- **Source badges** on the photo: `AUTOVIT`, `OLX`, `MOBILE.DE`. A merged card shows both.
- **Inline sparkline** next to the price on each card — a real price trace, not decoration.
  Only rendered with 2+ observations. The full Chart.js chart stays on the detail expansion.
- **The toolbar applies itself.** No Apply button: changing any field submits the form
  (on `change`, so a text box commits when you leave it or press Enter, not per keystroke).
  A `<noscript>` Apply button remains for browsers without JavaScript.
- **Only the view in use is rendered.** Emitting both the card grid and the compact rows and
  hiding one with CSS cost 528KB of HTML on the Germany tab, half of it never looked at. The
  cards/compact preference therefore rides in a cookie rather than `localStorage`, since the
  server has to read it; toggling costs one reload, and the page halves to ~256KB.
- **Cards** 268px minimum, auto-filling the width; **compact rows** with a thumbnail
  (specified as 64px; raised to 104px on 2026-09-05 after looking at it — 64 was too
  small to tell two silver Qashqais apart. The changes feed keeps 64px).
- Light and dark, following the system, as now.

## 8. Build phases — all five complete (2026-09-05)

Each phase shipped on its own and left the app working.

1. **Photos.** Add `image_url`, capture it in all three adapters, backfill autovit from
   `raw_json`, re-collect olx and mobile.de. No UI change yet — verify via `--status`/DB that
   every site now stores an image.
2. **Merge layer.** The grouping function plus tests against the real 38-way overlap, including
   the "must not touch the database" assertion. No UI change yet.
3. **Listings page.** Market tabs, card grid, compact toggle, wired to the merge layer.
   Overview retires; its health table moves to Runs.
4. **Filters and sorting** on both tabs, query-string backed.
5. **Restyle Changes and Runs** to match, including photos in the changes feed.

Two things were fixed along the way that the plan had not anticipated, both found by looking
at the real pages:

- olx renders a card's photo only once it scrolls into view, so the first capture had photos
  for 10 of 47 listings. The browser fetcher now walks the page before reading the DOM. The
  ceiling is 30 of 47: the other 17 ads genuinely have no photo on olx and carry OLX's own
  placeholder graphic — and since those are syndicated autovit ads, the merged card shows
  autovit's photo instead.
- mobile.de's attribute line carries fuel consumption ("8,6 l/100km") as well as the odometer,
  and the mileage parser was reading the "100km" in it. Every brand-new car — which states no
  mileage at all — came back with an invented, entirely plausible 100 km. 21 of 173 listings.
  Fixed, with a regression test.

## 9. Open questions — resolved 2026-09-05

- **Sorting default: newest first on every tab.** Arrivals are what you are watching
  for on both markets, and "newest" is the one ordering that is meaningful on day one,
  before there is enough price history for "biggest drop" to mean anything.
- **The Germany tab shows a "cheaper than the Romanian median" marker.** Noted at the
  time that German list prices are frequently pre-VAT and exclude import and
  registration cost, so this compares list prices and not landed cost. The marker is
  therefore labelled as a list-price comparison, not a saving.
- **Delisted cars keep their muted section at the bottom**, as on the current search
  detail page, rather than moving behind a filter toggle.
