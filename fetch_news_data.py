"""
fetch_news_data.py

Pulls today's geolocated events for three categories and saves them as a
single combined JSON file that the viewers (index.html, map.html, globe.html)
load directly:

  - conflict, political: GDELT's free DOC 2.0 API (full-text news search)
  - climate: NASA's EONET API (real tracked natural events -- wildfires,
    storms, floods, drought). No API key, no rate limit, and more precise
    than a news-text search for this category -- swapped in after GDELT's
    aggressive/opaque rate-limiting made climate unreliable.

Usage:
    python fetch_news_data.py
    python fetch_news_data.py --timespan 3d --target-locations 150
    python fetch_news_data.py --out data/news_data.json

Docs: https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/
      https://eonet.gsfc.nasa.gov/docs/v3
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

GDELT_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
EONET_ENDPOINT = "https://eonet.gsfc.nasa.gov/api/v3/events"
EONET_CATEGORIES = "drought,floods,severeStorms,wildfires,tempExtremes"

# Rough geographic buckets used only to keep the final selection from being
# dominated by whichever regions English-language wire services cover most
# heavily on a given day. Boxes are approximate and checked in order, so put
# smaller/more-specific regions (e.g. Middle East) before the broader boxes
# (Europe, Africa) they overlap with.
REGIONS = [
    ("North America", 5, 85, -170, -50),
    ("South America", -60, 15, -85, -30),
    ("Middle East", 12, 42, 25, 63),
    ("Europe", 35, 72, -25, 45),
    ("Africa", -35, 37, -20, 55),
    ("South & Central Asia", -10, 45, 60, 100),
    ("East & Southeast Asia", -10, 55, 100, 150),
    ("Oceania", -50, 0, 110, 180),
]


def region_for(lat: float, lon: float) -> str:
    for name, lat_min, lat_max, lon_min, lon_max in REGIONS:
        if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
            return name
    return "Other"


def balance_by_region(features: list, target: int) -> list:
    """Cap the result at `target` while spreading it across regions instead
    of just taking the top N by count (which would skew toward wherever
    English-language coverage happens to be heaviest that day)."""
    if len(features) <= target:
        return features

    buckets = defaultdict(list)
    for f in features:
        buckets[region_for(f["lat"], f["lon"])].append(f)
    for bucket in buckets.values():
        bucket.sort(key=lambda f: f.get("count", 1), reverse=True)

    order = list(buckets.keys())
    selected = []
    while len(selected) < target and any(buckets[r] for r in order):
        for r in order:
            if len(selected) >= target:
                break
            if buckets[r]:
                selected.append(buckets[r].pop(0))

    print("  region spread: " + ", ".join(
        f"{r}={sum(1 for f in selected if region_for(f['lat'], f['lon']) == r)}"
        for r in order
    ))
    return selected

# Tune these queries to taste. GDELT supports boolean OR/AND and phrase
# matching in quotes. Single generic words (bare "attack", "president",
# "congress"...) pull in a lot of unrelated matches, so these favor specific
# phrases over loose single terms to keep results on-topic.
QUERIES = {
    "conflict": (
        '(war OR "armed conflict" OR airstrike OR "air strike" OR shelling OR '
        '"armed clash" OR insurgency OR militant OR ceasefire OR '
        '"military offensive" OR "rebel attack") sourcelang:english'
    ),
    "political": (
        '("general election" OR "presidential election" OR parliament OR '
        'referendum OR "prime minister" OR "no-confidence vote" OR '
        '"supreme court ruling" OR "cabinet reshuffle" OR "coalition government" OR '
        'impeachment OR protest OR unrest) sourcelang:english'
    ),
}


def fetch_category(category: str, query: str, timespan: str, maxrecords: int) -> list:
    """Fetch one category from GDELT and return a list of feature dicts."""
    params = {
        "query": query,
        "mode": "geojson",
        "timespan": timespan,
        "maxrecords": str(maxrecords),
    }
    url = f"{GDELT_ENDPOINT}?{urllib.parse.urlencode(params)}"

    req = urllib.request.Request(url, headers={"User-Agent": "news-map-hobby-project/0.1"})

    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 2:
                wait = 10 * (attempt + 1)
                print(f"  ! rate-limited fetching '{category}', retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"  ! failed to fetch '{category}': {exc}", file=sys.stderr)
            return []
        except urllib.error.URLError as exc:
            print(f"  ! failed to fetch '{category}': {exc}", file=sys.stderr)
            return []

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        print(f"  ! GDELT returned non-JSON for '{category}' (likely rate-limited or bad query)", file=sys.stderr)
        return []

    features_out = []
    for feature in payload.get("features", []):
        geom = feature.get("geometry", {})
        coords = geom.get("coordinates")
        props = feature.get("properties", {})

        if not coords or len(coords) != 2:
            continue

        lon, lat = coords
        features_out.append({
            "category": category,
            "lat": lat,
            "lon": lon,
            "name": props.get("name", "Unknown location"),
            "count": props.get("count", 1),
            "html": props.get("html", ""),  # raw article links from GDELT
        })

    return features_out


def _eonet_point(geometry_entry: dict):
    """EONET geometry is either a Point (most events, standard GeoJSON
    [lon, lat]) or a Polygon (floods, reported as an affected area) --
    return a representative (lon, lat) either way.

    Quirk: the GDACS-sourced flood Polygons use [lat, lon] order, the
    opposite of the Point geometries -- confirmed by inspecting a real
    "Flood in Japan" event where coordinates only make sense as [lat, lon]
    (36.4, 136.8), not [lon, lat]. Handled explicitly below rather than
    trusting GeoJSON's usual [lon, lat] convention for this shape."""
    gtype = geometry_entry.get("type")
    coords = geometry_entry.get("coordinates")
    if gtype == "Point" and coords and len(coords) == 2:
        return coords
    if gtype == "Polygon" and coords and coords[0]:
        ring = coords[0]
        lat = sum(p[0] for p in ring) / len(ring)
        lon = sum(p[1] for p in ring) / len(ring)
        return [lon, lat]
    return None


def _clean_eonet_title(title: str) -> str:
    """GDACS-sourced titles (mainly floods) carry a trailing internal event
    ID, e.g. "Flood in Japan 1104140" -- strip it. Titles from other sources
    (wildfires, storms) don't have this and are left untouched."""
    return re.sub(r"\s+\d{5,}$", "", title).strip()


def _format_eonet_date(iso_date: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        return dt.strftime("%b %d, %Y")
    except ValueError:
        return iso_date


def _format_magnitude(value, unit: str):
    """Turn EONET's raw magnitude into a readable stat. Only wildfires
    (acres) and storms (knots) reliably carry this -- floods/drought from
    GDACS don't, so this returns None for those rather than inventing
    a number."""
    if value is None or not unit:
        return None
    if unit == "acres":
        return f"Size: {value:,.0f} acres"
    if unit == "kts":
        mph = round(value * 1.15078)
        return f"Peak winds: {value:.0f} kts (~{mph} mph)"
    return f"Magnitude: {value} {unit}"


def _build_eonet_summary(event: dict, geometry: list, event_type: str, source_name: str) -> list:
    """Build as many genuinely-informative points as the event actually
    supports. EONET's fields vary a lot by source: wildfires (IRWIN) often
    carry acreage and a plain-language location note; storms (JTWC/NOAA)
    are tracked over many days with wind-speed readings; floods (GDACS)
    typically carry none of that, just a place and a date -- so their
    summary stays short rather than padded with invented detail."""
    summary = [f"Type: {event_type}"]

    mag = _format_magnitude(geometry[-1].get("magnitudeValue"), geometry[-1].get("magnitudeUnit"))
    if mag:
        summary.append(mag)

    description = (event.get("description") or "").strip()
    if description:
        summary.append(f"Location note: {description}")

    if len(geometry) > 1:
        first_dt = geometry[0].get("date", "")
        last_dt = geometry[-1].get("date", "")
        try:
            d0 = datetime.fromisoformat(first_dt.replace("Z", "+00:00"))
            d1 = datetime.fromisoformat(last_dt.replace("Z", "+00:00"))
            days = max(1, round((d1 - d0).total_seconds() / 86400))
            summary.append(f"Tracked for {days} day{'s' if days != 1 else ''} (since {_format_eonet_date(first_dt)})")
        except ValueError:
            pass
    else:
        summary.append(f"Reported: {_format_eonet_date(geometry[-1].get('date', ''))}")

    summary.append(f"Source: {source_name}")
    return summary


def _fetch_eonet_events(category: str, status: str, limit: int, days: int = None) -> list:
    params = {"status": status, "limit": str(limit), "category": category}
    if days:
        params["days"] = str(days)
    url = f"{EONET_ENDPOINT}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "news-map-hobby-project/0.1"})

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        print(f"  ! failed to fetch EONET '{category}': {exc}", file=sys.stderr)
        return []

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        print("  ! EONET returned non-JSON", file=sys.stderr)
        return []

    features_out = []
    for event in payload.get("events", []):
        geometry = event.get("geometry", [])
        if not geometry:
            continue

        # Storms/wildfires are tracked over time as a series of points --
        # the last one is the most recent known position/extent.
        point = _eonet_point(geometry[-1])
        if not point:
            continue
        lon, lat = point

        title = _clean_eonet_title(event.get("title", "Unnamed event"))
        sources = event.get("sources") or []
        link = sources[0]["url"] if sources else "#"
        source_name = sources[0]["id"] if sources else "Unknown"
        event_type = event.get("categories", [{}])[0].get("title", "Event")

        features_out.append({
            "category": "climate",
            "lat": lat,
            "lon": lon,
            "name": title,
            "count": 1,  # EONET events are individually-tracked incidents, not article counts
            "html": f"<a href='{link}' target='_blank'>{title}</a>",
            "summary": _build_eonet_summary(event, geometry, event_type, source_name),
        })

    return features_out


def fetch_eonet_climate(limit: int, wildfire_cap: int = 15) -> list:
    """Fetch live climate/natural-event data from NASA's EONET -- no API key,
    no rate limit, and each event is a real tracked incident rather than a
    news-text match.

    Wildfires are capped tightly: EONET's "open" wildfire feed is fed almost
    entirely by IRWIN, the US interagency wildfire tracker, so pulling it
    without a cap buries every other category and country under US fires.
    The other categories are fetched across a wider time window with
    status=all (open + closed), which gives genuinely global coverage --
    floods/storms/drought from Japan, Vietnam, Lithuania, etc, not just the US.
    """
    features = _fetch_eonet_events("wildfires", status="open", limit=wildfire_cap)
    remaining = max(0, limit - len(features))
    features += _fetch_eonet_events(
        "drought,floods,severeStorms,tempExtremes", status="all", limit=remaining, days=21
    )
    return features


def load_acled_countries(path: str) -> list:
    """Load country-level conflict intensity from a manually-exported ACLED
    spreadsheet, pre-processed by convert_acled_countries.py into
    {country, lat, lon, count, year} entries. Optional -- if the file isn't
    there (never converted, or user hasn't re-exported), this just returns
    an empty list and conflict falls back to whatever GDELT provides.

    Note: this is one marker per COUNTRY (a yearly total), not individual
    incidents like GDELT/EONET give -- ACLED's per-event export with
    coordinates needs a more advanced access tier than a free account gets.
    """
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        print(f"  ! {path} is not valid JSON", file=sys.stderr)
        return []

    year = payload.get("year", "unknown")
    countries = payload.get("countries", [])
    total_countries = len(countries)

    features_out = []
    for rank, c in enumerate(countries, start=1):
        count = c["count"]
        features_out.append({
            "category": "conflict",
            "lat": c["lat"],
            "lon": c["lon"],
            "name": c["country"],
            "count": count,
            "html": f"<a href='https://acleddata.com/' target='_blank'>{c['country']}: political violence &amp; conflict events</a>",
            "summary": [
                "Type: Political violence & conflict (country total)",
                f"Events in {year}: {count:,}",
                f"Global rank: #{rank} of {total_countries} countries tracked",
                "Source: ACLED",
            ],
        })
    return features_out


def main():
    parser = argparse.ArgumentParser(description="Fetch daily geolocated news for the map viewer.")
    parser.add_argument("--timespan", default="2d", help="GDELT timespan, e.g. 1d, 6h, 3d (default: 2d)")
    parser.add_argument("--maxrecords", type=int, default=250, help="Max records per category, up to 250 -- GDELT's own cap (default: 250)")
    parser.add_argument("--target-locations", type=int, default=100, help="Cap on total locations, spread across world regions (default: 100)")
    parser.add_argument("--eonet-limit", type=int, default=100, help="Max EONET events to fetch for climate (default: 100)")
    parser.add_argument("--wildfire-cap", type=int, default=15, help="Max wildfires within that (US-heavy IRWIN source, capped to avoid dominating) (default: 15)")
    parser.add_argument("--acled-file", default="data/acled_country_events.json", help="Pre-processed ACLED country data from convert_acled_countries.py, optional (default: data/acled_country_events.json)")
    parser.add_argument("--out", default="data/news_data.json", help="Output path (default: data/news_data.json)")
    args = parser.parse_args()

    all_features = []
    for category, query in QUERIES.items():
        print(f"Fetching '{category}' events (timespan={args.timespan})...")
        features = fetch_category(category, query, args.timespan, args.maxrecords)
        print(f"  -> {len(features)} locations")
        all_features.extend(features)
        time.sleep(6)  # GDELT asks for at least 5s between requests per IP

    print("Fetching 'climate' events from NASA EONET...")
    climate_features = fetch_eonet_climate(args.eonet_limit, args.wildfire_cap)
    print(f"  -> {len(climate_features)} locations")
    all_features.extend(climate_features)

    acled_features = load_acled_countries(args.acled_file)
    if acled_features:
        print(f"Loaded {len(acled_features)} countries from ACLED ({args.acled_file})")
        all_features.extend(acled_features)

    print(f"\n{len(all_features)} locations fetched before region balancing.")

    # Balance per category, not across the whole pool: ACLED's country
    # totals (tens of thousands of events) would otherwise always outrank
    # EONET's individually-tracked events (count=1) within any region they
    # share, silently crowding climate out of e.g. Europe. Each category
    # gets its own fair share of the overall target instead.
    by_category = defaultdict(list)
    for f in all_features:
        by_category[f["category"]].append(f)
    active_categories = [c for c, feats in by_category.items() if feats]
    per_category_target = max(1, args.target_locations // max(1, len(active_categories)))

    all_features = []
    for feats in by_category.values():
        all_features.extend(balance_by_region(feats, per_category_target))

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "timespan": args.timespan,
        "feature_count": len(all_features),
        "features": all_features,
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    print(f"\nSaved {len(all_features)} total locations to {args.out}")
    if len(all_features) == 0:
        print("No results came back -- check your internet connection or try a longer --timespan.")


if __name__ == "__main__":
    main()
