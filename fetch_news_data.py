"""
fetch_news_data.py

Pulls today's geolocated events for three categories and saves them as a
single combined JSON file that the viewers (index.html, map.html, globe.html)
load directly:

  - conflict: GDELT's raw bulk event export files (updated every 15 min),
    filtered to CAMEO's protest/coercion/violence codes -- real individual
    incidents (battles, assaults, riots, protests), not a yearly total.
  - climate: NASA's EONET API (real tracked natural events -- wildfires,
    storms, floods, drought). No API key, no rate limit.
  - political: the same GDELT bulk event files, filtered instead to CAMEO
    codes for diplomatic/economic cooperation, agreements, aid, and
    sanctions -- i.e. nation-state actions that affect geopolitics (trade
    deals, treaties, diplomatic visits, sanctions), not general political
    news.

Conflict and political both read the bulk files rather than GDELT's DOC 2.0
search API, which is aggressively rate-limited from every network this was
tested on, GitHub Actions runners included -- the bulk files are plain
static file hosting with no such limit.

Usage:
    python fetch_news_data.py
    python fetch_news_data.py --gdelt-bulk-hours 24 --target-locations 150
    python fetch_news_data.py --out data/news_data.json

Docs: http://data.gdeltproject.org/gdeltv2/ (raw event files)
      https://www.gdeltproject.org/data/documentation/GDELT-Event_Codebook-V2.0.pdf
      https://eonet.gsfc.nasa.gov/docs/v3
"""

import argparse
import glob
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone

GDELT_BULK_BASE = "https://data.gdeltproject.org/gdeltv2"
EONET_ENDPOINT = "https://eonet.gsfc.nasa.gov/api/v3/events"
EONET_CATEGORIES = "drought,floods,severeStorms,wildfires,tempExtremes"

# CAMEO root event codes covering nation-state cooperation/friction that
# affects geopolitics -- trade, treaties, diplomacy, aid, sanctions -- as
# opposed to the general "politics" grab-bag (elections, court rulings)
# GDELT's text search used to cover. Root codes are the well-documented
# top level of the CAMEO taxonomy; see the codebook linked above.
GEOPOLITICAL_ROOT_CODES = {"03", "04", "05", "06", "07", "12", "16"}
CAMEO_ROOT_LABELS = {
    "03": "Expressed intent to cooperate",
    "04": "Diplomatic consultation",
    "05": "Diplomatic cooperation",
    "06": "Economic or material cooperation",
    "07": "Provided aid",
    "12": "Rejected cooperation",
    "16": "Reduced relations / sanctions",
}
# Same CAMEO taxonomy, opposite end of it: root codes 14/17-20 are GDELT's
# protest/coercion/violence categories -- the same real-world ground ACLED
# covers (battles, violence against civilians, riots, protests), just
# machine-coded from wire reporting every 15 minutes instead of a manually
# re-exported yearly country total.
CONFLICT_ROOT_CODES = {"14", "17", "18", "19", "20"}
CONFLICT_ROOT_LABELS = {
    "14": "Protest",
    "17": "Coercion",
    "18": "Assault",
    "19": "Armed clash",
    "20": "Mass violence",
}
# A handful of specific 3-digit codes worth a more precise label than their
# root category -- only ones we're confident about, everything else falls
# back to the root label above rather than guessing.
CAMEO_CODE_LABELS = {
    "042": "Diplomatic visit",
    "057": "Signed formal agreement",
    "061": "Economic cooperation",
    "070": "Provided aid",
    "163": "Imposed sanctions or embargo",
}
# GDELT tags a country code onto an actor even when the actor itself is a
# company, university, or media outlet merely located in that country (e.g.
# "Toyota" gets Japan's country code attached) -- these are still genuinely
# geopolitically relevant (a company signing a deal with a foreign
# government is real news), so rather than exclude them, label the actor's
# real type honestly instead of implying every pairing is government-to-
# government.
ACTOR_TYPE_LABELS = {
    "GOV": "government", "MIL": "military", "BUS": "company",
    "MNC": "multinational company", "EDU": "university", "MED": "media outlet",
    "NGO": "NGO", "IGO": "international org", "JUD": "judiciary",
    "LEG": "legislature", "COP": "police", "OPP": "opposition group",
    "ELI": "business/elite figure", "CVL": "civilian group",
    "CRM": "criminal group", "LAB": "labor group",
    "REB": "rebel group", "INS": "insurgent group", "SEP": "separatist group",
    "UAF": "unaligned armed forces", "PTY": "political party",
}


def _actor_label(name: str, type_code: str) -> str:
    tag = ACTOR_TYPE_LABELS.get(type_code)
    return f"{name} ({tag})" if tag else name

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


def _gdelt_bulk_timestamps(hours: int) -> list:
    """GDELT publishes a new file every 15 minutes, named by UTC timestamp.
    Step back an extra 15 minutes from now before starting, since the very
    latest file may not have finished publishing yet."""
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    now -= timedelta(minutes=(now.minute % 15) + 15)
    return [(now - timedelta(minutes=15 * i)).strftime("%Y%m%d%H%M%S") for i in range(hours * 4)]


def _fetch_gdelt_bulk_file(timestamp: str) -> list:
    """Download and parse one raw GDELT events file. Plain static file
    hosting -- no rate limit, unlike the DOC 2.0 search API."""
    url = f"{GDELT_BULK_BASE}/{timestamp}.export.CSV.zip"
    req = urllib.request.Request(url, headers={"User-Agent": "news-map-hobby-project/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError):
        return []

    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            content = zf.read(zf.namelist()[0]).decode("utf-8", errors="replace")
    except zipfile.BadZipFile:
        return []

    return [line.split("\t") for line in content.splitlines() if line]


def fetch_gdelt_bulk_political(hours: int, limit: int) -> list:
    """Political via GDELT's raw bulk event files rather than the
    rate-limited DOC 2.0 search API. Filtered to CAMEO codes for
    cooperation/agreements/aid/sanctions between two distinct countries --
    i.e. nation-state actions affecting geopolitics, not general political
    news (elections, court rulings, etc, which this deliberately excludes).
    """
    seen_urls = set()
    features = []

    for ts in _gdelt_bulk_timestamps(hours):
        for row in _fetch_gdelt_bulk_file(ts):
            if len(row) < 61:
                continue

            root_code = row[28]
            if root_code not in GEOPOLITICAL_ROOT_CODES:
                continue

            actor1_country, actor2_country = row[7], row[17]
            if not actor1_country or not actor2_country or actor1_country == actor2_country:
                continue  # only genuine two-country actions, not vague/domestic statements

            url = row[60]
            if not url or url in seen_urls:
                continue  # one source article often generates several rows (per actor/location mentioned)

            try:
                lat, lon = float(row[56]), float(row[57])
            except ValueError:
                continue
            if lat == 0 and lon == 0:
                continue

            seen_urls.add(url)

            event_code = row[26]
            label = CAMEO_CODE_LABELS.get(event_code) or CAMEO_ROOT_LABELS.get(root_code, "Diplomatic/political action")
            actor1_name = _actor_label((row[6] or actor1_country).title(), row[12])
            actor2_name = _actor_label((row[16] or actor2_country).title(), row[22])
            try:
                date_str = datetime.strptime(row[1], "%Y%m%d").strftime("%b %d, %Y")
            except ValueError:
                date_str = row[1]
            try:
                num_articles = max(1, int(float(row[33])))
            except ValueError:
                num_articles = 1

            title = f"{label}: {actor1_name} ↔ {actor2_name}"
            features.append({
                "category": "political",
                "lat": lat,
                "lon": lon,
                "name": title,
                "count": num_articles,
                "html": f"<a href='{url}' target='_blank'>{title}</a>",
                "summary": [
                    f"Type: {label}",
                    f"Actors: {actor1_name} ↔ {actor2_name}",
                    f"Reported: {date_str}",
                    "Source: GDELT (bulk event data)",
                ],
            })

            if len(features) >= limit:
                return features

    return features


def fetch_gdelt_bulk_conflict(hours: int, limit: int) -> list:
    """Conflict via the same GDELT bulk event files as political, filtered
    to CAMEO's protest/coercion/violence codes instead of cooperation codes
    -- real individual incidents (battles, assaults, riots, protests)
    refreshed every 15 minutes, in place of the old ACLED yearly country
    totals, so "conflict" actually means real-time like the rest of the
    site rather than a once-a-year snapshot.

    Unlike political, this doesn't require two distinct countries as
    actors -- most conflict (a government vs. a domestic rebel group, a
    protest against a country's own government) is single-country by
    nature, so that check would throw out most genuine conflict events.
    """
    seen_urls = set()
    features = []

    for ts in _gdelt_bulk_timestamps(hours):
        for row in _fetch_gdelt_bulk_file(ts):
            if len(row) < 61:
                continue

            root_code = row[28]
            if root_code not in CONFLICT_ROOT_CODES:
                continue

            url = row[60]
            if not url or url in seen_urls:
                continue  # one source article often generates several rows (per actor/location mentioned)

            try:
                lat, lon = float(row[56]), float(row[57])
            except ValueError:
                continue
            if lat == 0 and lon == 0:
                continue

            seen_urls.add(url)

            event_code = row[26]
            label = CAMEO_CODE_LABELS.get(event_code) or CONFLICT_ROOT_LABELS.get(root_code, "Conflict event")
            place = row[52] or "Unknown location"
            actor1_name = _actor_label(row[6].title(), row[12]) if row[6] else None
            actor2_name = _actor_label(row[16].title(), row[22]) if row[16] else None

            if actor1_name and actor2_name:
                actors_str = f"{actor1_name} vs {actor2_name}"
                title = f"{label}: {actors_str}"
            elif actor1_name or actor2_name:
                actors_str = actor1_name or actor2_name
                title = f"{label}: {actors_str}"
            else:
                actors_str = "Not identified in wire report"
                title = f"{label} in {place}"

            try:
                date_str = datetime.strptime(row[1], "%Y%m%d").strftime("%b %d, %Y")
            except ValueError:
                date_str = row[1]
            try:
                num_articles = max(1, int(float(row[33])))
            except ValueError:
                num_articles = 1

            features.append({
                "category": "conflict",
                "lat": lat,
                "lon": lon,
                "name": title,
                "count": num_articles,
                "html": f"<a href='{url}' target='_blank'>{title}</a>",
                "summary": [
                    f"Type: {label}",
                    f"Actors: {actors_str}",
                    f"Location: {place}",
                    f"Reported: {date_str}",
                    "Source: GDELT (bulk event data)",
                ],
            })

            if len(features) >= limit:
                return features

    return features


def main():
    parser = argparse.ArgumentParser(description="Fetch daily geolocated news for the map viewer.")
    parser.add_argument("--target-locations", type=int, default=100, help="Cap on total locations, spread across world regions (default: 100)")
    parser.add_argument("--eonet-limit", type=int, default=100, help="Max EONET events to fetch for climate (default: 100)")
    parser.add_argument("--wildfire-cap", type=int, default=15, help="Max wildfires within that (US-heavy IRWIN source, capped to avoid dominating) (default: 15)")
    parser.add_argument("--gdelt-bulk-hours", type=int, default=12, help="Hours of GDELT bulk event files to scan for conflict/political actions (default: 12)")
    parser.add_argument("--out", default="data/news_data.json", help="Output path (default: data/news_data.json)")
    parser.add_argument("--archive-dir", default="data/archive", help="Directory to keep one dated snapshot per day for the timeline feature (default: data/archive)")
    parser.add_argument("--archive-days", type=int, default=30, help="Days of dated snapshots to keep before pruning the oldest (default: 30)")
    parser.add_argument("--no-archive", action="store_true", help="Skip writing/pruning the dated archive snapshot")
    args = parser.parse_args()

    all_features = []

    print("Fetching 'climate' events from NASA EONET...")
    climate_features = fetch_eonet_climate(args.eonet_limit, args.wildfire_cap)
    print(f"  -> {len(climate_features)} locations")
    all_features.extend(climate_features)

    print(f"Fetching 'political' events from GDELT bulk data ({args.gdelt_bulk_hours}h window)...")
    political_features = fetch_gdelt_bulk_political(args.gdelt_bulk_hours, args.target_locations)
    print(f"  -> {len(political_features)} locations")
    all_features.extend(political_features)

    print(f"Fetching 'conflict' events from GDELT bulk data ({args.gdelt_bulk_hours}h window)...")
    conflict_features = fetch_gdelt_bulk_conflict(args.gdelt_bulk_hours, args.target_locations)
    print(f"  -> {len(conflict_features)} locations")
    all_features.extend(conflict_features)

    print(f"\n{len(all_features)} locations fetched before region balancing.")

    # Balance per category, not across the whole pool -- otherwise whichever
    # category has the most raw hits that hour would crowd the others out of
    # any region they share. Each category gets its own fair share instead.
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
        "feature_count": len(all_features),
        "features": all_features,
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    print(f"\nSaved {len(all_features)} total locations to {args.out}")
    if len(all_features) == 0:
        print("No results came back -- check your internet connection or try a longer --gdelt-bulk-hours.")

    if not args.no_archive:
        save_archive_snapshot(output, args.archive_dir, args.archive_days)


def save_archive_snapshot(output, archive_dir, keep_days):
    """Keep one dated snapshot per day (for the timeline feature) alongside
    the always-current news_data.json, pruning anything older than keep_days."""
    os.makedirs(archive_dir, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    archive_path = os.path.join(archive_dir, f"{today}.json")
    with open(archive_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    print(f"Archived today's snapshot to {archive_path}")

    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    for path in glob.glob(os.path.join(archive_dir, "*.json")):
        stem = os.path.splitext(os.path.basename(path))[0]
        try:
            day = datetime.strptime(stem, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if day < cutoff:
            os.remove(path)
            print(f"Pruned old archive snapshot {path}")


if __name__ == "__main__":
    main()
