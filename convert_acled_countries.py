"""
convert_acled_countries.py

One-time (or per-update) conversion tool: turns a manually-exported ACLED
"number of political violence events by country-year" spreadsheet into
data/acled_country_events.json, ready for fetch_news_data.py to pick up as
the conflict category.

Why this exists: ACLED's individual per-event export (with lat/lon per
incident) requires a more advanced access tier than a free account gets.
The freely-downloadable chart data is aggregated to country-year event
counts instead, so this script maps each country to its geographic centroid
and uses the most recent year's count as a conflict-intensity marker.

This is a separate tool from fetch_news_data.py (which has zero external
dependencies by design) because turning country names into coordinates
reliably needs a real geocoding reference, which pandas/openpyxl make far
easier to join against. Re-run this only when you have a fresh ACLED export;
fetch_news_data.py itself just reads the small JSON this produces.

Requires: pip install pandas openpyxl

Usage:
    python convert_acled_countries.py "path/to/acled_export.xlsx"
"""

import csv
import json
import sys
import urllib.request

try:
    import pandas as pd
except ImportError:
    print("This tool needs pandas and openpyxl: pip install pandas openpyxl", file=sys.stderr)
    sys.exit(1)

CENTROIDS_URL = "https://raw.githubusercontent.com/gavinr/world-countries-centroids/master/dist/countries.csv"
CENTROIDS_CACHE = "data/country_centroids.csv"

# ACLED's country names that don't match the centroid dataset's names directly.
NAME_ALIASES = {
    "Russia": "Russian Federation",
    "Palestine": "Palestinian Territory",
    "Democratic Republic of Congo": "Congo DRC",
    "Republic of Congo": "Congo",
    "Ivory Coast": "Côte d'Ivoire",
    "Cape Verde": "Cabo Verde",
    "Brunei": "Brunei Darussalam",
    "East Timor": "Timor-Leste",
    "eSwatini": "Eswatini",
    "Virgin Islands, U.S.": "US Virgin Islands",
}

# Missing from the centroid dataset entirely (disputed-status omissions).
MANUAL_CENTROIDS = {
    "Taiwan": (23.7, 121.0),
    "Kosovo": (42.6, 20.9),
}


def load_centroids() -> dict:
    try:
        with open(CENTROIDS_CACHE, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        print("Fetching country centroids reference...")
        urllib.request.urlretrieve(CENTROIDS_URL, CENTROIDS_CACHE)
        with open(CENTROIDS_CACHE, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

    return {r["COUNTRY"]: (float(r["latitude"]), float(r["longitude"])) for r in rows}


def main():
    if len(sys.argv) != 2:
        print("Usage: python convert_acled_countries.py <path-to-acled-export.xlsx>", file=sys.stderr)
        sys.exit(1)

    xlsx_path = sys.argv[1]
    df = pd.read_excel(xlsx_path)

    latest_year = df["YEAR"].max()
    latest = df[df["YEAR"] == latest_year]
    print(f"Using {latest_year} data ({len(latest)} countries).")

    centroids = load_centroids()
    centroids.update({k: v for k, v in MANUAL_CENTROIDS.items()})

    countries_out = []
    skipped = []
    for _, row in latest.iterrows():
        name = row["COUNTRY"]
        lookup_name = NAME_ALIASES.get(name, name)
        if lookup_name not in centroids:
            skipped.append(name)
            continue
        lat, lon = centroids[lookup_name]
        countries_out.append({
            "country": name,
            "lat": lat,
            "lon": lon,
            "count": int(row["EVENTS"]),
            "year": int(latest_year),
        })

    if skipped:
        print(f"Skipped {len(skipped)} unmatched (mostly small territories/ocean regions): {', '.join(skipped)}")

    countries_out.sort(key=lambda c: c["count"], reverse=True)

    output = {"year": int(latest_year), "countries": countries_out}
    with open("data/acled_country_events.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    print(f"Saved {len(countries_out)} countries to data/acled_country_events.json")
    print("Top 5:", ", ".join(f"{c['country']} ({c['count']})" for c in countries_out[:5]))


if __name__ == "__main__":
    main()
