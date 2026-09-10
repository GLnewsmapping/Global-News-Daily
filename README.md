# Dispatch — news globe & map

A concept viewer for geolocated daily news across three categories: conflict and political both from [GDELT](https://www.gdeltproject.org/)'s raw bulk event files (real individual incidents, refreshed every 15 minutes), and climate from [NASA's EONET](https://eonet.gsfc.nasa.gov/) (real tracked wildfires, storms, floods, and drought).

- **[index.html](index.html)** — the main page: a shared sidebar (masthead, category filters, shipping-lanes toggle, story list) with a switcher between a rotating 3D globe (via [globe.gl](https://globe.gl)) and a 2D dark map (via [Leaflet](https://leafletjs.com)). Both views share the same loaded data and filter state, so switching between them never loses your place.
- **[globe.html](globe.html)** / **[map.html](map.html)** — the original standalone single-view pages, kept working as lighter-weight alternates.

All three read the same `data/news_data.json` file and fall back to bundled sample data if it's missing or empty, so they work out of the box before you've fetched anything.

## Running it

These pages fetch local JSON at runtime, which browsers block under `file://`, so serve the folder over HTTP:

```bash
python -m http.server 8123
```

Then open `http://localhost:8123/` (serves `index.html`) — or `globe.html` / `map.html` directly for the standalone single-view pages.

## Fetching live data

```bash
python fetch_news_data.py
```

Options:

```bash
python fetch_news_data.py --gdelt-bulk-hours 24 --target-locations 150
python fetch_news_data.py --eonet-limit 150
python fetch_news_data.py --out data/news_data.json
```

**Conflict & political (GDELT bulk event files):** both categories read the same raw bulk event files at `data.gdeltproject.org/gdeltv2/*.export.CSV.zip`, published every 15 minutes -- plain static file hosting, not the rate-limited DOC 2.0 search API (which is aggressively throttled from every network this was tested on, GitHub Actions runners included). `--gdelt-bulk-hours` (default 12) controls how many hours of files to scan -- both fetchers scan the *entire* window before picking winners (rather than stopping as soon as they find enough matches), since files are scanned newest-first and stopping early would silently bias toward "most recent" over "most covered." The two categories are just different CAMEO event-code filters over the same files:

- **Conflict** -- real wars and armed conflicts, not any violent incident anywhere. Two gates, both required: the event must be real violence (root codes `18, 19, 20` in full -- assault, armed clash, mass violence -- plus the violent sub-codes of two otherwise non-violent roots: `145`/`1451-1454` "protest violently, riot" and `175` "use tactics of violent repression"), *and* it must be located inside a currently-active conflict zone. Which countries count as active is pulled live every run from Wikipedia's ["List of ongoing armed conflicts"](https://en.wikipedia.org/wiki/List_of_ongoing_armed_conflicts) (`fetch_conflict_zone_fips_codes`) -- only its two most severe tiers (1,000+ combat deaths/year), since the lower tiers mix in things like cartel/gang violence. Falls back to no geo-filter (not to zero results) if that fetch ever fails, so a Wikipedia hiccup doesn't hollow out the category. Real individual incidents, not a yearly country total. The 10 stories with the most corroborating articles are always included in the final selection regardless of region spread (see `guaranteed_top` in `balance_by_region`) -- a real spike in coverage shouldn't lose out to spreading picks across regions.
- **Political** -- root codes `03, 04, 05, 06, 07, 12, 16` (diplomatic cooperation, aid, sanctions, reduced relations): nation-state actions that affect geopolitics, not general political news.

Both filter out events where GDELT's actor extraction came back with a bare nationality/ethnic/religious adjective ("Chinese", "Islamic", "Muslims") instead of an actual named person, group, or institution -- treated as if no actor had been identified, rather than shown as if it were a specific entity (see `GENERIC_ACTOR_NAMES`). Both also filter out a stray place name mistaken for an actor -- a US state or city mentioned in passing in the article text ("Armed clash: Kansas" for an event actually in Florida) -- detected generically via GDELT's own geo-resolution for that actor rather than a hardcoded list of place names (see `_is_place_name_actor`; the country-level part of the geo hierarchy is deliberately excluded from this check, since a country genuinely can be a real actor). Conflict additionally excludes actor types that aren't plausible participants in violence -- a company, media outlet, university, or business figure ("Armed clash: Companies vs Europe", "Armed clash: Landlord") is almost always GDELT misattributing an incidentally-mentioned name, not a real party to the violence (see `IMPLAUSIBLE_VIOLENCE_ACTOR_TYPES`; not applied to political, where a company genuinely can be the actor).

**Climate (NASA EONET):** no API key, no rate limit. Each event is one real tracked incident, so its "article count" is always 1, unlike GDELT's per-event article tallies.

EONET's *open* wildfire feed is fed almost entirely by IRWIN (the US interagency wildfire tracker), so wildfires are capped tightly (`--wildfire-cap`, default 15) to avoid the US burying every other country. The rest of the climate budget is split between `status=open` events (no age limit -- still unfolding by definition) for what's currently developing, and `status=closed` events over a 45-day window for real settled history, rather than one "last 21 days, everything" query -- confirmed live that meaningful closed-event depth exists well past 21 days. Every climate summary states plainly whether the event is still active or when it ended.

Each category is region-balanced independently before merging (rather than one shared pool) -- otherwise whichever category has the most raw hits in a given window would crowd the others out of any region they share. The story list in `index.html` sorts by article count within whatever categories are active, so the highest-coverage stories naturally surface at the top.

## Real article headlines (conflict & political)

A conflict/political story's headline was, until now, a label synthesized purely from GDELT's own event classification ("Armed clash: Police") -- not the article's actual headline. That classification is sometimes flatly wrong about what the linked article covers (confirmed by inspecting real output: "Armed clash: Police" once linked to an article about stolen cars turning up overseas). `enrich_with_real_headlines()` fetches each selected story's real `<title>` and meta description and swaps them in, so the headline matches the source. `--no-headline-fetch` skips this (faster for local dev iteration).

**Conflict is additionally relevance-filtered** on the real article content: high-news-volume conflict-zone countries (China, India, Pakistan, Nigeria, Colombia...) generate far more everyday news than actual conflict coverage, and GDELT's assault/armed-clash event codes can't tell the difference -- confirmed live, this was letting through airline PR, stock-trade reports, a decades-old criminal sentencing, even a zombie-movie culture piece, all tagged as "conflict" because they happened to be geolocated in a country with a real internal conflict somewhere within it. `CONFLICT_RELEVANCE_KEYWORDS` (word-boundary matched, not substring -- a naive substring check would match "fighters" inside "Firefighters") requires the real title/description to actually mention war or violence before a conflict story survives. Because most matches don't survive this (confirmed: usually 15-25%), conflict fetches a *larger* raw pool (`--target-locations` × 2) and filters it *before* capping down to the display target, rather than after -- filtering an already-small capped set would leave far too few stories. A fetch that fails or times out leaves the story's original synthesized label in place rather than dropping it -- benefit of the doubt when a site simply couldn't be reached, not a penalty.

Political is not relevance-filtered (out of scope so far -- its own mismatches, like a Country Music Association Awards story once appearing in the feed, are a separate, smaller issue than conflict's).

## Daily archive (groundwork for a timeline feature)

Every run also writes a dated copy of that day's output to `data/archive/YYYY-MM-DD.json` (in addition to overwriting `data/news_data.json`), pruning anything older than `--archive-days` (default 30). `--archive-dir` changes the folder, `--no-archive` skips this entirely. The scheduled GitHub Action commits `data/archive/` alongside `data/news_data.json`, so history builds up automatically day by day. Nothing reads this yet -- it's the data plumbing for a future "view the last N days" timeline scrubber.

## Shipping lanes overlay

Both viewers have an optional "Shipping Lanes" toggle showing the world's busiest maritime trade routes, in `data/shipping_lanes.json`. The route geometry is adapted from [newzealandpaul/Shipping-Lanes](https://github.com/newzealandpaul/Shipping-Lanes) (CC-BY 4.0), itself georeferenced from the CIA's "Map of the World's Oceans" (Oct 2012) — real digitized routes, not live AIS tracking, so treat it as a snapshot rather than current traffic.

## Maritime chokepoints overlay

`index.html` also has a "Chokepoints" toggle labeling the world's 10 most important maritime chokepoints, in `data/chokepoints.json`. Compiled from [Visual Capitalist](https://www.visualcapitalist.com/mapping-the-worlds-key-maritime-choke-points/), [Mappr](https://www.mappr.co/important-straits-chokepoints-world/), and US EIA world oil transit chokepoint reporting — ranked by a blend of oil-transit volume, share of world trade, and vessel-traffic count, since no single metric captures "importance" the same way across all ten.

## Country borders overlay

A "Country Borders" toggle in `index.html` shows every country's outline on both the map and globe, and hovering anywhere inside a country shows its name. Geometry is `data/country_polygons.json` (full country polygons, not just outlines, so the whole interior is hoverable) — sourced from [Natural Earth](https://www.naturalearthdata.com/) (1:110m Admin 0 Countries, public domain) via [its official GitHub mirror](https://github.com/nvkelso/natural-earth-vector), the standard dataset for this, not hand-approximated. Off by default.

## Notes

- `index.html` has a favicon, meta description, and Open Graph/Twitter card tags (so shared links get a proper title/description), plus an "About & data sources" link in the sidebar that opens a modal summarizing each data source. `robots.txt`, `sitemap.xml`, and a themed `404.html` round out the "real site" basics for GitHub Pages.
- Below 768px wide, `index.html` switches to a mobile layout: the globe/map fills the screen and the sidebar becomes a slide-in drawer opened by a menu button top-left (tap the backdrop, or pick a story, to close it again).
- `index.html`'s map basemap is Esri World Imagery (satellite), chosen for maximum land/sea detail; continent labels are our own overlay (Esri's reference/labels layer duplicated them, so it isn't used). `globe.html` and standalone `map.html` still use Stadia Maps' `alidade_smooth_dark`, which is key-free for local/dev use only — **if you deploy `map.html` publicly**, Stadia requires a free API key for non-localhost domains (see [their docs](https://docs.stadiamaps.com/authentication/)).
- Category colors are kept exactly consistent between the globe, the map, and the sidebar: globe points are lit 3D shapes by default (globe.gl gives no material override), so the same hex rendered different shades depending on lighting/position -- fixed by moving the color from the lit `color` property to the unlit `emissive` one, which ignores scene lighting entirely (`makePointsUnlit()`). Map markers were at `fillOpacity: 0.55`, letting the satellite terrain underneath shift the perceived color -- now fully opaque.
- `data/news_data.sample.json` is static, hand-written sample data — safe to view in a browser with no setup.
- `data/news_data.json` (the live fetch output) doesn't exist until you run the fetch script — if you put this project under version control, you'll likely want to `.gitignore` it since it's regenerated data, not source.
