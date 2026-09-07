# Dispatch — news globe & map

A concept viewer for geolocated daily news across three categories: conflict from [ACLED](https://acleddata.com/) (country-level, manually updated) with a GDELT fallback, political from [GDELT](https://www.gdeltproject.org/)'s free DOC 2.0 API, and climate from [NASA's EONET](https://eonet.gsfc.nasa.gov/) (real tracked wildfires, storms, floods, and drought).

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
python fetch_news_data.py --timespan 2d --maxrecords 250
python fetch_news_data.py --eonet-limit 150
python fetch_news_data.py --out data/news_data.json
```

**Conflict/political (GDELT):** the free API asks for at least 5 seconds between requests per IP and returns `429 Too Many Requests` if you exceed that (the script already paces itself and retries once or twice on a 429). If you keep seeing 429s after waiting a minute or two, you're likely on a shared/rate-limited network — try again later or from a different connection. GDELT can go down for these categories independently of climate; the script saves whatever it got even if one or two categories come back empty.

**Climate (NASA EONET):** no API key, no rate limit, and not subject to GDELT's throttling — this category should basically always work. Each event is one real tracked incident, so its "article count" is always 1, unlike GDELT's per-place article tallies.

EONET's *open* wildfire feed is fed almost entirely by IRWIN (the US interagency wildfire tracker), so wildfires are capped tightly (`--wildfire-cap`, default 15) to avoid the US burying every other country. Floods/storms/drought/temperature-extremes are pulled across a wider 21-day window with `status=all`, which gives genuinely global coverage instead.

**Conflict (ACLED, manual updates):** ACLED's per-incident export (with coordinates) needs a more advanced access tier than a free account gets. Instead:

1. Log into [acleddata.com/data-export-tool](https://acleddata.com/data-export-tool/) and download the "number of political violence events by country-year" spreadsheet (freely available with a basic account).
2. Convert it: `python convert_acled_countries.py path/to/your-export.xlsx` (needs `pip install pandas openpyxl` — a one-off dependency for this conversion step only; the main fetch script stays dependency-free). This joins ACLED's country names to real centroid coordinates and writes `data/acled_country_events.json`.
3. Re-run `python fetch_news_data.py` as usual — it picks up that file automatically if present.

This gives one marker per **country** (a yearly total, e.g. Ukraine: 58,999 events in 2026), not individual incidents like GDELT/EONET -- a coarser resolution, but real ACLED data. Re-do steps 1–2 whenever you want a fresher count (this data doesn't update live). If `data/acled_country_events.json` isn't present, conflict just falls back to whatever GDELT provides.

Each category is region-balanced independently before merging (rather than one shared pool) -- otherwise ACLED's huge country counts would always outrank EONET's per-event count of 1, silently crowding climate out of any region they both touch.

## Shipping lanes overlay

Both viewers have an optional "Shipping Lanes" toggle showing the world's busiest maritime trade routes, in `data/shipping_lanes.json`. The route geometry is adapted from [newzealandpaul/Shipping-Lanes](https://github.com/newzealandpaul/Shipping-Lanes) (CC-BY 4.0), itself georeferenced from the CIA's "Map of the World's Oceans" (Oct 2012) — real digitized routes, not live AIS tracking, so treat it as a snapshot rather than current traffic.

## Maritime chokepoints overlay

`index.html` also has a "Chokepoints" toggle labeling the world's 10 most important maritime chokepoints, in `data/chokepoints.json`. Compiled from [Visual Capitalist](https://www.visualcapitalist.com/mapping-the-worlds-key-maritime-choke-points/), [Mappr](https://www.mappr.co/important-straits-chokepoints-world/), and US EIA world oil transit chokepoint reporting — ranked by a blend of oil-transit volume, share of world trade, and vessel-traffic count, since no single metric captures "importance" the same way across all ten.

## Country borders overlay

A "Country Borders" toggle in `index.html` shows every country's outline on both the map and globe, and hovering anywhere inside a country shows its name. Geometry is `data/country_polygons.json` (full country polygons, not just outlines, so the whole interior is hoverable) — sourced from [Natural Earth](https://www.naturalearthdata.com/) (1:110m Admin 0 Countries, public domain) via [its official GitHub mirror](https://github.com/nvkelso/natural-earth-vector), the standard dataset for this, not hand-approximated. Off by default.

## Notes

- `index.html`'s map basemap is Esri World Imagery (satellite), chosen for maximum land/sea detail; continent labels are our own overlay (Esri's reference/labels layer duplicated them, so it isn't used). `globe.html` and standalone `map.html` still use Stadia Maps' `alidade_smooth_dark`, which is key-free for local/dev use only — **if you deploy `map.html` publicly**, Stadia requires a free API key for non-localhost domains (see [their docs](https://docs.stadiamaps.com/authentication/)).
- `data/news_data.sample.json` is static, hand-written sample data — safe to view in a browser with no setup.
- `data/news_data.json` (the live fetch output) doesn't exist until you run the fetch script — if you put this project under version control, you'll likely want to `.gitignore` it since it's regenerated data, not source. The same goes for `data/acled_country_events.json` and `data/country_centroids.csv` (both regenerated by `convert_acled_countries.py`) and your raw ACLED export itself.
