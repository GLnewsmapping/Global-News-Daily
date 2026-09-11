# Dispatch — Project Handoff

Last updated: 2026-09-11

## Project goal

A live, publicly-shareable news globe/map ("Dispatch") that plots real geolocated events across three categories — **conflict**, **climate**, and **political** — refreshed automatically, with a strong emphasis on data accuracy and honesty over volume or visual polish. The site should reflect what's actually happening in the world right now, not a curated or embellished version of it. Long-term direction: buy a real domain and launch it publicly (currently paused, see Next Steps).

## Current state

Live at **https://glnewsmapping.github.io/Global-News-Daily/** (GitHub Pages, repo `https://github.com/GLnewsmapping/Global-News-Daily.git`, branch `main`). Working tree is clean; latest commit is `13c115d`.

All three categories are fully automated end to end — no manual steps anywhere in the pipeline:

- **Conflict**: GDELT bulk event files, filtered to real violence (CAMEO roots 18/19/20 in full, plus only the violent sub-codes of PROTEST and COERCE), further restricted to events located inside a currently-active conflict zone (list pulled live from Wikipedia), and relevance-filtered against the real fetched article content so a story has to actually be about armed conflict, not just technically match GDELT's classification.
- **Climate**: NASA EONET, mixing currently-active events (no age limit) with settled ones over a 45-day window, so the feed has both breaking and historical depth. Each summary states plainly whether the event is still active or when it ended.
- **Political**: the same GDELT bulk files, filtered to diplomatic/geopolitical cooperation codes, and relevance-filtered the same way conflict is.

A GitHub Actions cron (`.github/workflows/refresh-data.yml`) re-runs the fetch daily at 06:00 UTC and auto-commits the result. A dated snapshot is also archived each day (`data/archive/YYYY-MM-DD.json`, pruned after 30 days) as groundwork for a future timeline feature — 4 days of history exist so far (2026-09-07 through 2026-09-10).

The site itself: a shared sidebar (masthead, category filters as solid color blocks, shipping-lanes/chokepoints/country-borders toggles, live-searchable story list) driving either a 3D globe (globe.gl) or a 2D map (Leaflet), sharing one data load and filter state. Mobile (below 768px) gets a full-screen globe/map with the sidebar as a slide-in drawer. Confirmed working on the user's actual phone, not just emulated viewports.

## Files being worked on

- **`fetch_news_data.py`** — the entire data pipeline. This is where almost all of the recent work has happened. No external dependencies (stdlib only) by design.
- **`index.html`** — the main combined page (globe + map + sidebar). Most of the visual/interaction work lives here.
- **`map.html`** / **`globe.html`** — lighter standalone single-view alternates, kept in sync for basics (e.g. the map world-wrap fix) but not actively developed further.
- **`README.md`** — kept in sync with every architectural change; it's the most detailed technical reference and should be read alongside this file.
- **`.github/workflows/refresh-data.yml`** — the daily automation.
- **`data/`** — generated output (`news_data.json`, `archive/*.json`) plus static reference data (`country_polygons.json`, `shipping_lanes.json`, `chokepoints.json`) that doesn't change often.

## What's been changed (most recent first)

- Extended the real-article relevance filter (see below) from conflict-only to political too — both categories now fetch 2x their display target as a raw pool and filter for real relevance *before* capping down, since 70-85% of raw candidates don't survive the filter.
- Added `enrich_with_real_headlines()`: conflict/political story headlines used to be labels synthesized from GDELT's own event classification ("Armed clash: Police"), not the article's real headline. Now fetches each selected story's actual `<title>`/meta description and swaps them in. This is what *surfaced* the need for the relevance filter in the first place — reading real headlines revealed many "conflict" stories were about completely unrelated things (see Failed Attempts).
- Excluded the cartel/gang-violence country cluster (US, Mexico, Belize, Guatemala, Honduras, El Salvador, Nicaragua) from the conflict-zone list.
- Added a conflict-zone geo-filter: conflict events must be located inside a currently-active war/armed-conflict zone, not just be violent anywhere. The zone list is pulled live every run from Wikipedia's "List of ongoing armed conflicts" (top 2 severity tiers only) and mapped to FIPS 10-4 country codes (confirmed empirically that's what GDELT's own geocoding uses, not ISO).
- Redefined conflict to real violence only — narrowed from all of CAMEO's PROTEST/COERCE root codes down to just their violent sub-codes, plus assault/armed-clash/mass-violence in full.
- Filtered out implausible actor types (companies, media outlets, universities showing up as if they were combatants) and bare place names GDELT sometimes mistakes for actors (a US state or city name standing in as an "actor").
- Fixed category-color consistency between the globe, map, and sidebar (globe points were lit 3D shapes that shaded with scene lighting; map markers were too transparent, letting terrain bleed through the color).
- Broadened and enriched climate coverage (open + closed events, 45-day window; explicit "still active" / "ended" status per story).
- Switched conflict off ACLED's yearly country totals onto the same real-time GDELT pipeline as political.
- Fixed a severe globe drag/zoom performance bug caused by the Country Borders overlay.
- Sidebar redesign, mobile responsive layout, site legitimacy basics (favicon, meta tags, About modal, robots.txt/sitemap/404), map world-wrap fix.

Full technical detail and rationale for all of the above is in `README.md`.

## Failed attempts (so this doesn't get relearned the hard way)

- **Filtering for relevance *after* capping to the display count starves the category.** First pass applied `CONFLICT_RELEVANCE_KEYWORDS` to the already-balanced 33-story set — dropped it to ~7 stories. Fix: fetch a larger raw pool (2x target) and filter *before* the region-balancing/capping step.
- **Naive substring keyword matching is unsound.** `"fighters" in text.lower()` matched inside `"Firefighters"`, causing a completely unrelated entertainment story to pass the conflict filter. Same risk exists for "coup" inside "coupon", etc. Fixed by switching to `\b`-bounded regex matching.
- **Including "United States" in the conflict-zone list let through inappropriate content** — a child-sexual-abuse-material sentencing story got tagged "conflict" because it was geolocated in the US (on the Wikipedia list via cartel/gang violence) and GDELT's assault code fired on it. Root cause: high-news-volume countries generate far more routine crime/court reporting than actual conflict coverage, and GDELT's event codes can't tell the difference. This is why the whole cartel-violence country cluster is excluded, not just the US.
- **`opacity: 0` on invisible meshes is expensive.** The Country Borders overlay's invisible-but-hoverable country shapes used transparent materials, which forced WebGL to depth-sort and alpha-blend every mesh every frame (~35ms/frame measured). Geometry simplification and coarser curvature resolution were tried first and had ~zero effect — the real fix was `colorWrite: false` on an otherwise-opaque material (~3-15x faster), which achieves the same visual invisibility without the sort/blend cost.
- **Toggling `enablePointerInteraction` mid-drag-gesture broke OrbitControls worse than the original bug.** Attempted as a globe performance fix; measured rotation dropping to near-zero (worse than doing nothing). Reverted.
- **This sandbox's Browser pane throttles `requestAnimationFrame` when backgrounded**, which makes synthetic drag/hover tests here noisy and untrustworthy for performance work specifically (the underlying feature testing is fine). Timing `renderer.render()` directly, bypassing rAF, is what actually gave reliable performance numbers.
- **GDELT's DOC 2.0 search API is rate-limited from every network tested**, including GitHub Actions' own runners — not a local/network-specific issue. This is why conflict and political both read GDELT's raw bulk event files instead (published every 15 min, no rate limit).
- **ACLED's free tier only gives yearly country aggregates**, not real per-incident data with coordinates — didn't fit the site's real-time premise. Dropped in favor of GDELT bulk files for conflict too.
- **Don't assume a domain name is available — verify live.** `dispatchworld.com` was assumed available based on earlier discussion; a live RDAP check showed it's been registered since 2002. `dispatchsphere.com` is the verified-available recommendation.

## Next steps

1. **Domain purchase + launch.** Paused pending data-quality improvements — those are now largely done, so this is a natural point to pick it back up. Recommended domain: `dispatchsphere.com` (verified available). Full setup steps, DNS records, and a staggered launch plan with ready-to-paste post copy (Show HN, X, Reddit, Product Hunt) are published at the launch-plan artifact from this session.
2. **Let the new relevance filter run for a few more days** before tuning `CONFLICT_RELEVANCE_KEYWORDS`/`POLITICAL_RELEVANCE_KEYWORDS` further — they were calibrated against a single day's sample, and a broader sample across different news cycles will surface edge cases a single snapshot can't.
3. **Timeline scrubber**, once the archive has more history (currently 4 days, started 2026-09-07). Nothing reads the archive yet — it's pure groundwork so far.
4. **Analytics before driving real traffic** — Cloudflare Web Analytics was recommended in the launch plan (free, no cookie banner needed) but not yet implemented.
5. Consider whether the real-headline/description content already being fetched could enrich the summary boxes further, since the fetching infrastructure now exists for both conflict and political.
