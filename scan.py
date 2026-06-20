#!/usr/bin/env python3
"""
Vienna Restaurant Trend Tracker — weekly scan.

ONE job does both discovery and the weekly snapshot, because Nearby Search (New)
already returns rating + userRatingCount. There is no separate Place Details job.

Env vars required:
  GOOGLE_MAPS_API_KEY   Places API (New) enabled, billing on
  DATABASE_URL          Supabase/Postgres connection string

Run:  python scan.py
"""
import os
import sys
import time
import datetime as dt
import requests
import psycopg2
from psycopg2.extras import execute_values

API_KEY = os.environ["GOOGLE_MAPS_API_KEY"]
DB_URL  = os.environ["DATABASE_URL"]

NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"

# Fields we ask for. rating + userRatingCount push this to the Enterprise SKU.
FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.location",
    "places.primaryType",
    "places.businessStatus",
    "places.rating",
    "places.userRatingCount",
])

# Vienna bounding box (rough). Trim corners later to cut empty calls.
LAT_MIN, LAT_MAX = 48.118, 48.323
LNG_MIN, LNG_MAX = 16.182, 16.578

# Grid: ~550 m spacing, 400 m search radius. Inner districts are dense and
# Nearby Search (New) caps at 20 results with NO pagination — if you see cells
# returning exactly 20, densify the grid there (drop SPACING_M to ~300).
SPACING_M = 550
RADIUS_M  = 400.0

# Keep the genuine restaurant set; drop the obvious non-restaurants at the API.
EXCLUDED_PRIMARY = ["cafe", "bar", "bakery", "coffee_shop",
                    "meal_takeaway", "meal_delivery", "night_club"]

M_PER_DEG_LAT = 111_320.0
def m_per_deg_lng(lat):  # longitude shrinks with latitude
    import math
    return 111_320.0 * math.cos(math.radians(lat))


def grid_centers():
    lat = LAT_MIN
    dlat = SPACING_M / M_PER_DEG_LAT
    while lat <= LAT_MAX:
        dlng = SPACING_M / m_per_deg_lng(lat)
        lng = LNG_MIN
        while lng <= LNG_MAX:
            yield round(lat, 6), round(lng, 6)
            lng += dlng
        lat += dlat


def search_cell(lat, lng):
    body = {
        "includedTypes": ["restaurant"],
        "excludedPrimaryTypes": EXCLUDED_PRIMARY,
        "maxResultCount": 20,
        "locationRestriction": {
            "circle": {"center": {"latitude": lat, "longitude": lng},
                       "radius": RADIUS_M}
        },
    }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    for attempt in range(4):
        r = requests.post(NEARBY_URL, json=body, headers=headers, timeout=30)
        if r.status_code == 200:
            return r.json().get("places", [])
        if r.status_code in (429, 500, 503):       # backoff & retry
            time.sleep(2 ** attempt)
            continue
        # Anything else is a real error — surface it once and skip the cell.
        sys.stderr.write(f"cell {lat},{lng} -> {r.status_code} {r.text[:200]}\n")
        return []
    return []


def first_of_this_month():
    today = dt.date.today()
    return today.replace(day=1)


def main():
    week = first_of_this_month()   # one snapshot per month; stored in week_start_date
    seen = {}          # place_id -> record (dedup across overlapping cells)
    calls = 0
    cells = list(grid_centers())
    print(f"Scanning {len(cells)} grid cells for week {week} …")

    for i, (lat, lng) in enumerate(cells, 1):
        places = search_cell(lat, lng)
        calls += 1
        for p in places:
            pid = p.get("id")
            if not pid or pid in seen:
                continue
            loc = p.get("location", {})
            seen[pid] = {
                "place_id": pid,
                "name": (p.get("displayName") or {}).get("text", ""),
                "primary_type": p.get("primaryType"),
                "business_status": p.get("businessStatus", "OPERATIONAL"),
                "lat": loc.get("latitude"),
                "lng": loc.get("longitude"),
                "rating": p.get("rating"),
                "review_count": p.get("userRatingCount"),
            }
        if i % 100 == 0:
            print(f"  {i}/{len(cells)} cells, {len(seen)} unique places, {calls} calls")
        time.sleep(0.05)   # be gentle on the per-minute quota

    print(f"Done scanning. {len(seen)} unique restaurants, {calls} API calls.")
    write_db(week, list(seen.values()))


def write_db(week, records):
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = False
    cur = conn.cursor()

    # Upsert restaurants (refresh last_seen, name, status, location).
    execute_values(cur, """
        insert into restaurants
          (place_id, name, primary_type, business_status, latitude, longitude,
           first_seen, last_seen)
        values %s
        on conflict (place_id) do update set
          name = excluded.name,
          primary_type = excluded.primary_type,
          business_status = excluded.business_status,
          latitude = excluded.latitude,
          longitude = excluded.longitude,
          last_seen = excluded.last_seen
    """, [(r["place_id"], r["name"], r["primary_type"], r["business_status"],
           r["lat"], r["lng"], week, week) for r in records])

    # Insert this week's snapshots (idempotent on re-run).
    execute_values(cur, """
        insert into weekly_snapshots (place_id, week_start_date, rating, review_count)
        values %s
        on conflict (place_id, week_start_date) do update set
          rating = excluded.rating,
          review_count = excluded.review_count
    """, [(r["place_id"], week, r["rating"], r["review_count"]) for r in records])

    conn.commit()
    cur.close()
    conn.close()
    print(f"Wrote {len(records)} restaurants + snapshots for {week}.")


if __name__ == "__main__":
    main()
