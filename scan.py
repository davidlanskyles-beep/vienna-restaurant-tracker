#!/usr/bin/env python3
"""
Vienna Restaurant Trend Tracker — monthly scan.

ONE job does both discovery and the monthly snapshot, because Nearby Search (New)
already returns rating + userRatingCount. There is no separate Place Details job.

It also captures, in the SAME call (no extra cost, because rating/userRatingCount
already put us on the top field tier):
  - district  — derived from the Vienna postal code (1XX0 -> district XX, 1..23)
  - price_level — Google's price band, 1 (€) .. 4 (€€€€)

Cost-saving cache
-----------------
Mapping the whole city (~2,268 grid cells) is the expensive part. Most cells are
empty, so we remember which cells returned restaurants and, on normal months,
only re-check those. A FULL re-map runs every few months (and always when the
cache is empty) to pick up newly-opened areas.

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

# Fields we ask for. rating + userRatingCount push this to the Enterprise SKU;
# addressComponents + priceLevel ride along at no extra per-call cost.
FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.location",
    "places.primaryType",
    "places.businessStatus",
    "places.rating",
    "places.userRatingCount",
    "places.priceLevel",
    "places.addressComponents",
])

# Vienna bounding box (rough). Trim corners later to cut empty calls.
LAT_MIN, LAT_MAX = 48.118, 48.323
LNG_MIN, LNG_MAX = 16.182, 16.578

SPACING_M = 550
RADIUS_M  = 400.0

EXCLUDED_PRIMARY = ["cafe", "bar", "bakery", "coffee_shop",
                    "meal_takeaway", "meal_delivery", "night_club",
                    "amusement_park", "comedy_club"]

# Months in which we do a FULL re-map of the whole grid (quarterly).
FULL_REMAP_MONTHS = {1, 4, 7, 10}

PRICE_MAP = {
    "PRICE_LEVEL_INEXPENSIVE": 1,
    "PRICE_LEVEL_MODERATE": 2,
    "PRICE_LEVEL_EXPENSIVE": 3,
    "PRICE_LEVEL_VERY_EXPENSIVE": 4,
}

M_PER_DEG_LAT = 111_320.0
def m_per_deg_lng(lat):
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


def parse_price(p):
    return PRICE_MAP.get(p.get("priceLevel"))


def parse_district(p):
    """Vienna postal codes are 1XX0, where XX is the district (01..23)."""
    try:
        for comp in p.get("addressComponents", []) or []:
            if "postal_code" in (comp.get("types") or []):
                pc = (comp.get("longText") or comp.get("shortText") or "").strip()
                if len(pc) == 4 and pc.isdigit() and pc[0] == "1":
                    d = int(pc[1:3])
                    if 1 <= d <= 23:
                        return d
    except Exception:
        pass
    return None


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
        if r.status_code in (429, 500, 503):
            time.sleep(2 ** attempt)
            continue
        sys.stderr.write(f"cell {lat},{lng} -> {r.status_code} {r.text[:200]}\n")
        return []
    return []


def first_of_this_month():
    return dt.date.today().replace(day=1)


def ensure_schema(conn):
    """Self-migration: make sure the district/price columns and the ranking
    views exist with the latest shape. Safe to run every time."""
    cur = conn.cursor()
    cur.execute("""
        create table if not exists productive_cells (
          lat double precision not null, lng double precision not null,
          last_seen date not null default current_date,
          primary key (lat, lng)
        );
        alter table restaurants add column if not exists district smallint;
        alter table restaurants add column if not exists price_level smallint;

        -- Rebuild the view chain so the new district/price columns and the
        -- two-metric model take effect. CREATE OR REPLACE cannot reorder or insert
        -- columns, so we drop the chain (CASCADE clears the dependent views,
        -- including the retired three-list views) and recreate it.
        drop view if exists v_current cascade;

        create view v_current as
          select s.place_id, r.name, r.primary_type, s.rating, s.review_count,
                 s.week_start_date, r.district, r.price_level
          from weekly_snapshots s
          join restaurants r using (place_id)
          join v_weeks_ranked w on w.week_start_date = s.week_start_date and w.wk_rank = 1
          where r.business_status = 'OPERATIONAL' and s.review_count is not null
            and (r.primary_type is null or
                 r.primary_type not in ('amusement_park','comedy_club'));

        create view v_growth as
          select c.*, p.review_count as review_count_prior,
                 (c.review_count - p.review_count) as growth_abs,
                 case when p.review_count > 0
                      then (c.review_count - p.review_count)::numeric / p.review_count
                      else null end as growth_rate
          from v_current c left join v_prior p using (place_id);

        -- Single source for the website: every operational restaurant, each with
        -- its Bayesian best-overall score (m = 400: rating blended with the city
        -- average, weighted by review count) AND its month-over-month review gain.
        -- The page derives the search tool, the "Beste insgesamt" list and the
        -- "Im Trend" list all from this one view.
        create view v_all as
        with stats as (select avg(rating) as c_mean from v_current)
        select g.name, g.rating, g.review_count, g.primary_type, g.district, g.price_level,
               g.growth_abs, g.growth_rate,
               round(((g.review_count * g.rating + 400 * s.c_mean)
                      / (g.review_count + 400))::numeric, 4) as score
        from v_growth g, stats s
        where g.review_count is not null
        order by score desc;
    """)
    conn.commit()
    cur.close()


def load_productive_cells(conn):
    cur = conn.cursor()
    cur.execute("select lat, lng from productive_cells")
    rows = cur.fetchall()
    cur.close()
    return [(float(la), float(ln)) for la, ln in rows]


def save_productive_cells(conn, cells, day):
    if not cells:
        return
    cur = conn.cursor()
    execute_values(cur, """
        insert into productive_cells (lat, lng, last_seen) values %s
        on conflict (lat, lng) do update set last_seen = excluded.last_seen
    """, [(la, ln, day) for (la, ln) in cells])
    conn.commit()
    cur.close()


def main():
    month_start = first_of_this_month()

    conn = psycopg2.connect(DB_URL)
    ensure_schema(conn)
    cached = load_productive_cells(conn)

    full_remap = (not cached) or (dt.date.today().month in FULL_REMAP_MONTHS)
    if full_remap:
        cells = list(grid_centers())
        why = "cache empty" if not cached else "scheduled quarterly re-map"
        print(f"FULL re-map ({why}): scanning {len(cells)} grid cells for month {month_start} …")
    else:
        cells = cached
        print(f"Incremental: scanning {len(cells)} cached productive cells for month {month_start} …")

    seen = {}
    productive = []
    calls = 0

    for i, (lat, lng) in enumerate(cells, 1):
        places = search_cell(lat, lng)
        calls += 1
        if places:
            productive.append((round(lat, 6), round(lng, 6)))
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
                "district": parse_district(p),
                "price_level": parse_price(p),
            }
        if i % 100 == 0:
            print(f"  {i}/{len(cells)} cells, {len(seen)} unique places, {calls} calls")
        time.sleep(0.05)

    print(f"Done scanning. {len(seen)} unique restaurants, {calls} API calls, "
          f"{len(productive)} productive cells.")

    write_db(month_start, list(seen.values()))

    if full_remap:
        save_productive_cells(conn, productive, month_start)
        print(f"Cache updated: {len(productive)} productive cells stored.")
    conn.close()


def write_db(week, records):
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = False
    cur = conn.cursor()

    execute_values(cur, """
        insert into restaurants
          (place_id, name, primary_type, business_status, latitude, longitude,
           district, price_level, first_seen, last_seen)
        values %s
        on conflict (place_id) do update set
          name = excluded.name,
          primary_type = excluded.primary_type,
          business_status = excluded.business_status,
          latitude = excluded.latitude,
          longitude = excluded.longitude,
          district = excluded.district,
          price_level = excluded.price_level,
          last_seen = excluded.last_seen
    """, [(r["place_id"], r["name"], r["primary_type"], r["business_status"],
           r["lat"], r["lng"], r["district"], r["price_level"], week, week) for r in records])

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
