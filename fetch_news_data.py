"""
fetch_news_data.py

Pulls today's geolocated events for three categories and saves them as a
single combined JSON file that the viewers (index.html, map.html, globe.html)
load directly:

  - conflict: GDELT's raw bulk event export files (updated every 15 min),
    filtered to real violence (battles, assaults, riots, violent
    repression) happening inside a currently-active conflict zone -- real
    wars and armed conflicts, not any violent incident anywhere. Which
    countries count as an active conflict zone is itself pulled live from
    Wikipedia's "List of ongoing armed conflicts" rather than hardcoded.
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
import html
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

GDELT_BULK_BASE = "https://data.gdeltproject.org/gdeltv2"
EONET_ENDPOINT = "https://eonet.gsfc.nasa.gov/api/v3/events"
EONET_CATEGORIES = "drought,floods,severeStorms,wildfires,tempExtremes"
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"

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
# Conflict means real violence -- military/security force action or a
# protest that turned violent -- not the full CAMEO 14/17-20 "friction"
# range, which also covers plenty of non-violent ground (peaceful
# marches, strikes, boycotts, curfews, arrests, property seizure, cyber
# attacks). Root codes 18/19/20 are kept in full since every code under
# them is inherently violent; roots 14 (PROTEST) and 17 (COERCE) are
# collapsed down to just their violent sub-codes via CONFLICT_VIOLENT_SUBCODES
# below -- confirmed against GDELT's own CAMEO codebook, not guessed.
CONFLICT_ROOT_CODES = {"18", "19", "20"}
CONFLICT_ROOT_LABELS = {
    "18": "Assault",
    "19": "Armed clash",
    "20": "Mass violence",
}
# The only violent slices of PROTEST (14) and COERCE (17): 145/1451-1454
# is CAMEO's "protest violently, riot" (as opposed to 140-144, peaceful
# demonstrations/hunger strikes/strikes/blockades); 175 is "use tactics
# of violent repression" (as opposed to 170-174/176, arrests, curfews,
# property seizure, deportation, cyberattacks -- coercive but not violent).
CONFLICT_VIOLENT_SUBCODES = {"145", "1451", "1452", "1453", "1454", "175"}
CONFLICT_SUBCODE_LABELS = {
    "145": "Riot", "1451": "Riot", "1452": "Riot", "1453": "Riot", "1454": "Riot",
    "175": "Violent repression",
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
    **CONFLICT_SUBCODE_LABELS,
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

# Python's .title() mangles acronyms it title-cases word-by-word (US -> Us,
# UK -> Uk) -- these are the ones that actually show up as GDELT country/
# actor names, fixed back up after title-casing rather than skipping
# .title() entirely (which would leave genuinely-lowercase raw names alone).
_ACRONYM_FIXES = {"Us": "US", "Uk": "UK", "Un": "UN", "Eu": "EU", "Uae": "UAE", "Usa": "USA"}


def _title_case(name: str) -> str:
    return " ".join(_ACRONYM_FIXES.get(word, word) for word in name.title().split(" "))

# GDELT's actor extraction sometimes comes back with a bare nationality/
# ethnic/religious adjective instead of an actual named person, group, or
# institution -- "Chinese", "Muslims", "Westerners" aren't a real identified
# actor, just noise from the underlying NLP (and pinning a violent or
# coercive action on an entire nationality/religion as if it were a named
# party is a real accuracy and fairness problem, not just a vague one).
# Treated as if no actor had been extracted at all. Demonyms are covered
# broadly (most UN member states) rather than patched one miss at a time.
_DEMONYMS = """
afghan albanian algerian american andorran angolan argentine argentinian
armenian australian austrian azerbaijani bahamian bahraini bangladeshi
barbadian belarusian belgian belizean beninese bhutanese bolivian bosnian
botswanan brazilian british bruneian bulgarian burkinabe burmese burundian
cambodian cameroonian canadian chadian chilean chinese colombian comoran
congolese costa rican croatian cuban cypriot czech danish djiboutian
dominican dutch ecuadorian egyptian salvadoran eritrean estonian ethiopian
fijian finnish french gabonese gambian georgian german ghanaian greek
grenadian guatemalan guinean guyanese haitian honduran hungarian icelandic
indian indonesian iranian iraqi irish israeli italian ivorian jamaican
japanese jordanian kazakh kenyan kosovar kuwaiti kyrgyz lao laotian latvian
lebanese liberian libyan lithuanian luxembourgish macedonian malagasy
malawian malaysian maldivian malian maltese mauritanian mauritian mexican
moldovan mongolian montenegrin moroccan mozambican namibian nepali nepalese
nicaraguan nigerien nigerian norwegian omani pakistani palauan palestinian
panamanian paraguayan peruvian filipino polish portuguese qatari romanian
russian rwandan salvadoran samoan saudi senegalese serbian seychellois
singaporean slovak slovenian somali surinamese swazi swedish swiss syrian
taiwanese tajik tanzanian thai togolese tongan trinidadian tunisian turkish
turkmen ugandan ukrainian emirati uruguayan uzbek venezuelan vietnamese
yemeni zambian zimbabwean
""".split()
GENERIC_ACTOR_NAMES = set(_DEMONYMS) | {d + "s" for d in _DEMONYMS} | {
    "european", "europeans", "african", "africans", "asian", "asians",
    "western", "westerners", "eastern", "arab", "arabs", "muslim", "muslims",
    "islamic", "islamist", "islamists", "christian", "christians", "jewish",
    "jews", "hindu", "hindus", "buddhist", "buddhists", "catholic", "catholics",
    "protestant", "protestants", "sunni", "sunnis", "shia", "shiite", "shiites",
    "men", "man", "woman", "women", "people", "person", "individuals",
    "unidentified", "unidentified actor", "unknown",
}


def _is_generic_actor_name(name: str) -> bool:
    return name.strip().lower() in GENERIC_ACTOR_NAMES


def _is_place_name_actor(actor_name: str, geo_type: str, geo_fullname: str) -> bool:
    """GDELT occasionally extracts a bare place name mentioned in the
    article text and tags it as if it were an actor -- a US state
    ("Kansas"), or a city referenced in passing ("Kyiv" in a story
    actually located in Florida) -- not a real participant. Detected
    generically rather than from a hardcoded gazetteer: GDELT resolves
    its own geography for each actor, so if that resolution is
    sub-national (a US state/city or world city/state -- geo types 2-5,
    not a bare country) and the actor's name matches the city/region part
    of that resolution specifically, it's a place, not a person, group,
    or institution. The country part is deliberately excluded from this
    check -- a country genuinely can be a real actor ("Ukraine",
    "China"), unlike a city or state.
    """
    if not actor_name or geo_type not in ("2", "3", "4", "5") or not geo_fullname:
        return False
    parts = [p.strip() for p in geo_fullname.split(",") if p.strip()]
    if len(parts) < 2:
        return False
    name_lower = actor_name.strip().lower()
    return any(name_lower == p.lower() for p in parts[:-1])  # every part except the trailing country

# For conflict specifically: GDELT's actor extraction sometimes tags an
# incidentally-mentioned company, outlet, or institution as if it were a
# participant in the violence itself -- "Armed clash: Companies vs Europe",
# "Armed clash: Landlord (company)". A business isn't a plausible party to
# an armed clash or an assault, so these actor types are treated as if no
# actor had been extracted, the same way generic demonyms are. Not applied
# to political, where a company genuinely can be the actor (a firm signing
# a deal, taking a sanction) -- this is specific to violence.
IMPLAUSIBLE_VIOLENCE_ACTOR_TYPES = {"BUS", "MNC", "EDU", "MED", "ELI"}

# "Conflict" means real wars and armed conflicts, not any violent incident
# anywhere -- an assault in Vermont or a prison fight in Minnesota isn't a
# war, even though it's real violence. This narrows conflict events down to
# ones happening inside a currently-active conflict zone, sourced live from
# Wikipedia's crowd-sourced, actively-maintained "List of ongoing armed
# conflicts" rather than a list we'd have to hand-update ourselves as wars
# start, end, or shift. Only the two most severe tiers on that page (1,000+
# combat deaths/year -- "major wars" and "minor wars" in its own
# terminology) are used; the lower tiers mix in things like cartel/gang
# violence that read more like organized crime than a war zone.
CONFLICT_ZONE_TABLE_IDS = ("conflicts10000", "conflicts1000")

# Wikipedia's country names -> FIPS 10-4 codes, since that's what GDELT's
# own ActionGeo_CountryCode field actually uses (confirmed empirically --
# it's NOT ISO 3166; e.g. Ukraine is FIPS "UP", not ISO "UA"). Verified
# against the FIPS 10-4 reference table, not guessed. Palestine maps to
# both Gaza Strip and West Bank, since FIPS predates a unified Palestine
# code and splits it into those two historical entities.
CONFLICT_ZONE_COUNTRY_FIPS = {
    "Afghanistan": ["AF"], "Algeria": ["AG"], "Bahrain": ["BA"],
    "Bangladesh": ["BG"], "Belarus": ["BO"], "Belize": ["BH"],
    "Benin": ["BN"], "Burkina Faso": ["UV"], "Burundi": ["BY"],
    "Cameroon": ["CM"], "Central African Republic": ["CT"], "Chad": ["CD"],
    "China": ["CH"], "Colombia": ["CO"],
    "Democratic Republic of the Congo": ["CG"], "Ecuador": ["EC"],
    "Egypt": ["EG"], "El Salvador": ["ES"], "Eritrea": ["ER"],
    "Ethiopia": ["ET"], "Guatemala": ["GT"], "Haiti": ["HA"],
    "Honduras": ["HO"], "India": ["IN"], "Iran": ["IR"], "Iraq": ["IZ"],
    "Israel": ["IS"], "Ivory Coast": ["IV"], "Jordan": ["JO"],
    "Kenya": ["KE"], "Kuwait": ["KU"], "Lebanon": ["LE"], "Libya": ["LY"],
    "Mali": ["ML"], "Mauritania": ["MR"], "Mexico": ["MX"],
    "Morocco": ["MO"], "Myanmar": ["BM"], "Nicaragua": ["NU"],
    "Niger": ["NG"], "Nigeria": ["NI"], "Oman": ["MU"],
    "Pakistan": ["PK"], "Palestine": ["GZ", "WE"], "Qatar": ["QA"],
    "Russia": ["RS"], "Rwanda": ["RW"], "Saudi Arabia": ["SA"],
    "Somalia": ["SO"], "Somaliland": ["SO"], "South Sudan": ["OD"],
    "Sudan": ["SU"], "Syria": ["SY"], "Tajikistan": ["TI"],
    "Thailand": ["TH"], "Togo": ["TO"], "Tunisia": ["TS"],
    "Turkey": ["TU"], "Uganda": ["UG"], "Ukraine": ["UP"],
    "United Arab Emirates": ["AE"], "United States": ["US"],
    "Venezuela": ["VE"], "Yemen": ["YM"],
}

# A handful of Wikipedia's "major wars" entries are classified there for
# cartel/gang violence, not an organized armed conflict most people would
# picture as "a war zone". That alone might be a defensible judgment call
# to leave in -- but the real problem, confirmed by inspecting actual
# fetched headlines: these countries (the US above all) generate an
# enormous volume of ordinary daily crime and court reporting that GDELT's
# assault/armed-clash event codes can't distinguish from cartel violence,
# and that volume drowned out genuine signal -- letting completely
# unrelated stories (a decades-old criminal sentencing, a medical-investment
# PR release) through as if they were conflict-zone violence. Excluded
# rather than trusting the source's tier classification for this cluster.
CONFLICT_ZONE_EXCLUDED_COUNTRIES = {
    "United States", "Mexico", "Belize", "Guatemala", "Honduras",
    "El Salvador", "Nicaragua",
}


def fetch_conflict_zone_fips_codes() -> set:
    """Live-derive the current set of active-conflict-zone FIPS country
    codes from Wikipedia rather than hand-maintaining a static list.
    Falls back to an empty set (meaning: don't geo-filter at all, rather
    than filtering everything out) if the fetch fails or the page
    structure looks different than expected, so a Wikipedia hiccup
    degrades the site gracefully instead of hollowing out the category.
    """
    params = {
        "action": "parse",
        "page": "List_of_ongoing_armed_conflicts",
        "prop": "wikitext",
        "format": "json",
    }
    url = f"{WIKIPEDIA_API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "news-map-hobby-project/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        wikitext = payload["parse"]["wikitext"]["*"]
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, json.JSONDecodeError) as exc:
        print(f"  ! failed to fetch conflict-zone list from Wikipedia: {exc}", file=sys.stderr)
        return set()

    countries = set()
    for table_id in CONFLICT_ZONE_TABLE_IDS:
        start = wikitext.find(f'id="{table_id}"')
        if start == -1:
            continue
        end = wikitext.find("\n|}", start)
        block = wikitext[start:end if end != -1 else None]
        countries.update(m.strip() for m in re.findall(r"\{\{flag\|([^}|]+)", block))

    if len(countries) < 10:
        print(f"  ! Wikipedia conflict-zone list looked too small ({len(countries)} countries) -- page structure may have changed, skipping geo-filter", file=sys.stderr)
        return set()

    countries -= CONFLICT_ZONE_EXCLUDED_COUNTRIES

    fips_codes = set()
    unmapped = []
    for name in countries:
        codes = CONFLICT_ZONE_COUNTRY_FIPS.get(name)
        if codes:
            fips_codes.update(codes)
        else:
            unmapped.append(name)
    if unmapped:
        print(f"  ! conflict-zone countries with no FIPS mapping (add to CONFLICT_ZONE_COUNTRY_FIPS): {', '.join(sorted(unmapped))}", file=sys.stderr)

    return fips_codes

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


def balance_by_region(features: list, target: int, guaranteed_top: int = 0) -> list:
    """Cap the result at `target` while spreading it across regions instead
    of just taking the top N by count (which would skew toward wherever
    English-language coverage happens to be heaviest that day).

    `guaranteed_top` carves out that many of the single highest-count
    features first, unconditionally, before region-balancing the rest --
    so a real spike (many outlets covering the same event) always makes
    the cut instead of possibly losing out to region diversity, while the
    remaining slots still get spread across regions as before.
    """
    if len(features) <= target:
        return features

    by_count = sorted(features, key=lambda f: f.get("count", 1), reverse=True)
    guaranteed, pool = by_count[:guaranteed_top], by_count[guaranteed_top:]
    remaining_target = target - len(guaranteed)

    buckets = defaultdict(list)
    for f in pool:
        buckets[region_for(f["lat"], f["lon"])].append(f)
    for bucket in buckets.values():
        bucket.sort(key=lambda f: f.get("count", 1), reverse=True)

    order = list(buckets.keys())
    selected = []
    while len(selected) < remaining_target and any(buckets[r] for r in order):
        for r in order:
            if len(selected) >= remaining_target:
                break
            if buckets[r]:
                selected.append(buckets[r].pop(0))

    spread = ", ".join(
        f"{r}={sum(1 for f in selected if region_for(f['lat'], f['lon']) == r)}"
        for r in order
    )
    if guaranteed:
        spread += f" (+{len(guaranteed)} guaranteed top-count)"
    print("  region spread: " + spread)
    return guaranteed + selected


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

    # EONET's `closed` field is the ground truth for whether this is still
    # unfolding or already resolved -- surfaced explicitly now that the
    # feed deliberately mixes both kinds of story (see fetch_eonet_climate),
    # so a reader isn't left guessing which one they're looking at.
    closed_date = event.get("closed")
    summary.append(f"Status: Ended {_format_eonet_date(closed_date)}" if closed_date else "Status: Still active")

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
            verb = "Tracked for" if closed_date else "Tracking for"
            summary.append(f"{verb} {days} day{'s' if days != 1 else ''} (since {_format_eonet_date(first_dt)})")
        except ValueError:
            pass
    else:
        label = "Reported" if closed_date else "First reported"
        summary.append(f"{label}: {_format_eonet_date(geometry[-1].get('date', ''))}")

    # Multiple independent trackers (e.g. JTWC + NOAA both tracking the same
    # storm) is real corroboration worth showing, not just the first one.
    source_names = [s["id"] for s in (event.get("sources") or []) if s.get("id")]
    if len(source_names) > 1:
        summary.append(f"Tracked by: {', '.join(source_names)}")
    else:
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

    The rest of the budget is deliberately split between two different
    kinds of story rather than one "all, last 21 days" query: status=open
    events (no age limit -- they're still unfolding by definition, so a
    45-day-old drought that's still active is exactly as "developing" as
    one that started yesterday) for what's happening right now, and
    status=closed events over a wider 45-day window for real settled
    history -- confirmed via the live API that meaningful closed-event
    depth exists well past 21 days, it just wasn't being reached before.
    """
    features = _fetch_eonet_events("wildfires", status="open", limit=wildfire_cap)
    remaining = max(0, limit - len(features))

    open_budget = max(1, remaining // 2)
    features += _fetch_eonet_events(
        "drought,floods,severeStorms,tempExtremes", status="open", limit=open_budget
    )
    remaining = max(0, limit - len(features))
    features += _fetch_eonet_events(
        "drought,floods,severeStorms,tempExtremes", status="closed", limit=remaining, days=45
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

    Scans the entire requested window before picking winners, rather than
    stopping as soon as `limit` rows are found -- GDELT files are dense
    enough that stopping early means only ever seeing the most recent hour
    or so (files are scanned newest-first), which silently biases toward
    "most recent" instead of "most covered" and undermines sorting by
    article count later. `_SAFETY_CAP` just bounds runtime/memory if hours
    is set very high; it isn't meant to be hit in normal use.
    """
    seen_urls = set()
    features = []
    _SAFETY_CAP = 6000

    for ts in _gdelt_bulk_timestamps(hours):
        if len(features) >= _SAFETY_CAP:
            break
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

            actor1_raw = _title_case(row[6] or actor1_country)
            actor2_raw = _title_case(row[16] or actor2_country)
            if _is_generic_actor_name(actor1_raw) or _is_generic_actor_name(actor2_raw):
                continue  # a bare nationality/ethnic word isn't a real identified actor
            if _is_place_name_actor(actor1_raw, row[35], row[36]) or _is_place_name_actor(actor2_raw, row[43], row[44]):
                continue  # a stray city/state name mentioned in the article, not a real actor

            seen_urls.add(url)

            event_code = row[26]
            label = CAMEO_CODE_LABELS.get(event_code) or CAMEO_ROOT_LABELS.get(root_code, "Diplomatic/political action")
            place = row[52] or "Unknown location"
            actor1_name = _actor_label(actor1_raw, row[12])
            actor2_name = _actor_label(actor2_raw, row[22])
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
                    f"Location: {place}",
                    f"Reported: {date_str}",
                    "Source: GDELT (bulk event data)",
                ],
            })

    features.sort(key=lambda f: f["count"], reverse=True)
    return features[:limit]


def fetch_gdelt_bulk_conflict(hours: int, limit: int) -> list:
    """Conflict via the same GDELT bulk event files as political, filtered
    to real violence -- military/security force action (assault, armed
    clashes, mass violence) or a protest that turned violent (riots,
    violent repression) -- happening inside a currently-active conflict
    zone (see fetch_conflict_zone_fips_codes), refreshed every 15 minutes,
    in place of the old ACLED yearly country totals, so "conflict" means
    real wars and armed conflicts around the world, tracked in real time,
    rather than any violent incident anywhere.

    Unlike political, this doesn't require two distinct countries as
    actors -- most conflict (a government vs. a domestic rebel group, a
    protest against a country's own government) is single-country by
    nature, so that check would throw out most genuine conflict events.

    Also scans the entire requested window before picking winners rather
    than stopping at `limit` rows -- see fetch_gdelt_bulk_political for why.
    """
    conflict_zone_fips = fetch_conflict_zone_fips_codes()
    if conflict_zone_fips:
        print(f"  Active conflict zones (from Wikipedia): {len(conflict_zone_fips)} country codes")

    seen_urls = set()
    features = []
    _SAFETY_CAP = 6000

    for ts in _gdelt_bulk_timestamps(hours):
        if len(features) >= _SAFETY_CAP:
            break
        for row in _fetch_gdelt_bulk_file(ts):
            if len(row) < 61:
                continue

            root_code = row[28]
            event_code = row[26]
            if root_code not in CONFLICT_ROOT_CODES and event_code not in CONFLICT_VIOLENT_SUBCODES:
                continue

            if conflict_zone_fips and row[53] not in conflict_zone_fips:
                continue  # real violence, but not in a currently-active conflict zone

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

            label = CAMEO_CODE_LABELS.get(event_code) or CONFLICT_ROOT_LABELS.get(root_code, "Conflict event")
            place = row[52] or "Unknown location"
            actor1_type, actor2_type = row[12], row[22]
            actor1_ok = (row[6] and not _is_generic_actor_name(row[6])
                         and actor1_type not in IMPLAUSIBLE_VIOLENCE_ACTOR_TYPES
                         and not _is_place_name_actor(row[6], row[35], row[36]))
            actor2_ok = (row[16] and not _is_generic_actor_name(row[16])
                         and actor2_type not in IMPLAUSIBLE_VIOLENCE_ACTOR_TYPES
                         and not _is_place_name_actor(row[16], row[43], row[44]))
            actor1_raw = _title_case(row[6]) if actor1_ok else None
            actor2_raw = _title_case(row[16]) if actor2_ok else None
            actor1_name = _actor_label(actor1_raw, actor1_type) if actor1_raw else None
            actor2_name = _actor_label(actor2_raw, actor2_type) if actor2_raw else None

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

    features.sort(key=lambda f: f["count"], reverse=True)
    return features[:limit]


# Titles that mean the fetch hit a bot-block, paywall, or geo-restriction
# page rather than the actual article -- using one of these as a story's
# headline would be worse than the synthesized label it's meant to
# replace, since it reads as a real (if odd) headline rather than an error.
JUNK_TITLE_PATTERNS = (
    "unavailable in your location", "just a moment", "access denied",
    "are you a human", "are you a robot", "attention required",
    "enable javascript", "page not found", "403 forbidden",
    "404 not found", "subscribe to continue", "subscribe now",
)


def _fetch_page_title_and_description(url: str, timeout: int = 6):
    """Fetch a page and pull its real <title> and meta description, so a
    story can show what its source article is actually about instead of
    a label synthesized from GDELT's event classification (which is
    sometimes flatly wrong about what the article even covers). Regex-
    based on purpose -- stdlib only, and good enough for two near-
    universal, well-formed tags without needing a full HTML parser.
    Reads a bounded number of bytes since both tags always sit near the
    top of a page's <head>, keeping this fast even on large pages."""
    req = urllib.request.Request(url, headers={"User-Agent": "news-map-hobby-project/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(200_000)
    except Exception:
        return None, None

    text = raw.decode("utf-8", errors="replace")

    title = None
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    if m:
        title = html.unescape(re.sub(r"\s+", " ", m.group(1))).strip() or None
        if title and any(p in title.lower() for p in JUNK_TITLE_PATTERNS):
            title = None  # a bot-block/geo-block/error page's title, not the article's

    description = None
    m = re.search(
        r'<meta[^>]+(?:property|name)=["\'](?:og:)?description["\'][^>]*content=["\']([^"\']*)["\']',
        text, re.IGNORECASE,
    ) or re.search(
        r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\'](?:og:)?description["\']',
        text, re.IGNORECASE,
    )
    if m:
        description = html.unescape(re.sub(r"\s+", " ", m.group(1))).strip() or None

    return title, description


# A lightweight relevance check on the article's own real title/description
# (once fetched above) -- catches stories where GDELT's event classification
# and the conflict-zone geo-filter both technically matched, but the real
# article has nothing to do with armed conflict at all. Confirmed necessary
# by inspecting real output: high-news-volume conflict-zone countries
# (China, India, Pakistan, Nigeria, Colombia...) let through airline PR,
# stock-trade reports, tax disputes, and entertainment news, since GDELT's
# assault/armed-clash codes fire on all sorts of unrelated content located
# in a country that also happens to have a real internal conflict somewhere
# within it. Favors precision over recall -- specific, mostly-unambiguous
# violence/war terms, deliberately avoiding a bare "war" (too many idioms:
# "trade war", "war on drugs", "culture war" would all false-positive).
CONFLICT_RELEVANCE_KEYWORDS = (
    "armed conflict", "airstrike", "air strike", "artillery", "shelling",
    "missile strike", "drone strike", "gunfire", "gunmen", "militant",
    "insurgent", "insurgency", "rebel", "ceasefire", "cease-fire",
    "offensive", "front line", "frontline", "war zone", "warzone",
    "troops", "soldiers", "military operation", "clash", "casualties",
    "bombing", "bombed", "ambush", "siege", "invasion", "warplane",
    "rocket attack", "mortar", "combat", "skirmish", "military raid",
    "cross-border raid", "blockade",
    "martial law", "battlefield", "killed in fighting", "wounded in",
    "shot dead", "gun battle", "firefight", "warlord", "paramilitary",
    "peacekeeping", "humanitarian corridor", "coup", "uprising", "junta",
    "occupation", "displaced by", "shelled", "attacked by", "fighters",
    "extremist", "terrorist", "terrorism",
)


# Word-boundary matching, not substring containment -- "fighters" as a
# plain substring check matches inside "Firefighters", the same way "coup"
# would match inside "coupon". Compiled once at import time since this
# runs per-story.
_CONFLICT_RELEVANCE_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(kw) for kw in CONFLICT_RELEVANCE_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


def _is_conflict_relevant(title: str, description: str) -> bool:
    text = f"{title or ''} {description or ''}"
    return bool(_CONFLICT_RELEVANCE_PATTERN.search(text))


def enrich_with_real_headlines(features: list, categories=("conflict", "political"), max_workers: int = 12) -> None:
    """Conflict and political headlines are synthesized from GDELT's own
    event classification ("Armed clash: Police"), not the article's real
    headline -- and that classification is sometimes flatly wrong about
    what the linked article is actually about. This fetches each story's
    real <title> (and meta description, if present) and swaps them in,
    closing the gap between what the summary claims happened and what
    the source actually says -- and incidentally surfaces GDELT
    misclassifications instead of stating them as settled fact, since a
    wrong event-type guess becomes obvious once the real headline shows.

    For conflict specifically, a story whose real content turns out to
    have nothing to do with armed conflict (see CONFLICT_RELEVANCE_KEYWORDS)
    is dropped from `features` entirely rather than just relabeled --
    a wrong headline is one thing, but a story about airline business
    class or a tax dispute has no business in a conflict feed regardless
    of what its headline says. Political isn't filtered this way (out of
    scope for now; its own mismatches are a separate, smaller issue).

    Mutates `features` in place. Any fetch that fails, times out, or
    finds no title leaves that story's existing synthesized label
    untouched and does NOT drop it -- benefit of the doubt when we
    simply couldn't check, rather than penalizing a blocked/slow site.
    """
    targets = [f for f in features if f.get("category") in categories]
    if not targets:
        return

    def url_of(feature):
        m = re.search(r"href='([^']+)'", feature.get("html", ""))
        return m.group(1) if m else None

    to_drop = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_target = {}
        for f in targets:
            url = url_of(f)
            if url:
                future_to_target[pool.submit(_fetch_page_title_and_description, url)] = (f, url)

        fetched = 0
        for future in as_completed(future_to_target):
            feature, url = future_to_target[future]
            try:
                title, description = future.result()
            except Exception:
                title, description = None, None
            if not title:
                continue

            if feature["category"] == "conflict" and not _is_conflict_relevant(title, description):
                to_drop.append(feature)
                continue

            fetched += 1
            feature["name"] = title
            feature["html"] = f"<a href='{url}' target='_blank'>{title}</a>"
            if description:
                if len(description) > 200:
                    description = description[:197].rstrip() + "..."
                feature["summary"] = [description] + feature["summary"]

    if to_drop:
        drop_ids = {id(f) for f in to_drop}
        features[:] = [f for f in features if id(f) not in drop_ids]

    print(f"  Enriched {fetched} of {len(targets)} conflict/political stories with real article headlines"
          + (f", dropped {len(to_drop)} conflict stories whose real content had nothing to do with armed conflict" if to_drop else ""))


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
    parser.add_argument("--no-headline-fetch", action="store_true", help="Skip fetching real article headlines for conflict/political (faster for local dev iteration)")
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
    # Fetches a larger pool than the display target -- confirmed empirically
    # that a large majority of real-violence-in-a-conflict-zone matches
    # still turn out (once you read the real article) to have nothing to
    # do with armed conflict, especially from high-news-volume conflict-zone
    # countries. Filtering for relevance happens on this larger pool, before
    # capping down to the display target below, rather than after -- doing
    # it after would mean rejecting most of an already-small, already-capped
    # set and ending up with far too few conflict stories to show.
    conflict_features = fetch_gdelt_bulk_conflict(args.gdelt_bulk_hours, args.target_locations * 2)
    print(f"  -> {len(conflict_features)} locations before relevance filtering")
    if not args.no_headline_fetch:
        print("  Checking conflict stories against their real article content...")
        enrich_with_real_headlines(conflict_features, categories=("conflict",))
        print(f"  -> {len(conflict_features)} locations after relevance filtering")
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
    for category, feats in by_category.items():
        # The 10 conflict stories with the most corroborating articles are
        # always included, region diversity aside -- a real spike (many
        # outlets covering the same event) is exactly the kind of story
        # that shouldn't lose out to spreading picks across regions.
        guaranteed_top = 10 if category == "conflict" else 0
        all_features.extend(balance_by_region(feats, per_category_target, guaranteed_top))

    if not args.no_headline_fetch:
        print("\nFetching real article headlines for political stories...")
        enrich_with_real_headlines(all_features, categories=("political",))

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
