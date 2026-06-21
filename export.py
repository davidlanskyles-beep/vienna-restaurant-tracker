#!/usr/bin/env python3
"""
Read the two ranking views and write a single rankings.json.
The static frontend reads this file directly — no server needed.

Env:  DATABASE_URL
Run:  python export.py   ->   writes ./public/rankings.json
"""
import os, json, datetime as dt
import psycopg2
from psycopg2.extras import RealDictCursor

DB_URL = os.environ["DATABASE_URL"]
LIMIT = 300  # per section (higher so the cuisine/district/price filters have depth)

QUERIES = {
    "best_overall": "select * from v_best_overall limit %s",
    "trending":     "select * from v_trending     limit %s",
}

def main():
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    out = {"generated_at": dt.datetime.utcnow().isoformat() + "Z"}
    cur.execute("select week_start_date from v_latest_week")
    row = cur.fetchone()
    out["week_start_date"] = str(row["week_start_date"]) if row and row["week_start_date"] else None

    # The city-average rating that the Bayesian score uses (for transparency on the page).
    cur.execute("select round(avg(rating)::numeric, 2) as c_mean, count(*) as n from v_current")
    s = cur.fetchone()
    out["city_avg_rating"] = float(s["c_mean"]) if s and s["c_mean"] is not None else None
    out["restaurant_count"] = int(s["n"]) if s and s["n"] is not None else 0
    out["bayes_m"] = 400

    for key, sql in QUERIES.items():
        cur.execute(sql, (LIMIT,))
        rows = cur.fetchall()
        for r in rows:                       # JSON-friendly types
            if r.get("growth_rate") is not None:
                r["growth_rate"] = float(r["growth_rate"])
            if r.get("score") is not None:
                r["score"] = float(r["score"])
        out[key] = rows
    cur.close(); conn.close()

    os.makedirs("public", exist_ok=True)
    with open("public/rankings.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print(f"Wrote public/rankings.json  "
          f"(best={len(out['best_overall'])} trending={len(out['trending'])})")

if __name__ == "__main__":
    main()
