#!/usr/bin/env python3
"""
Read the combined ranking view (v_all) and write a single rankings.json.
The static frontend reads this file and derives the explorer, the
"Beste insgesamt" list and the "Im Trend" list from it. No server needed.

Env:  DATABASE_URL
Run:  python export.py   ->   writes ./public/rankings.json
"""
import os, json, datetime as dt
import psycopg2
from psycopg2.extras import RealDictCursor

DB_URL = os.environ["DATABASE_URL"]
LIMIT = 10000  # effectively "all" — the explorer/search needs every restaurant

def main():
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    out = {"generated_at": dt.datetime.utcnow().isoformat() + "Z"}
    cur.execute("select week_start_date from v_latest_week")
    row = cur.fetchone()
    out["week_start_date"] = str(row["week_start_date"]) if row and row["week_start_date"] else None

    # City-average rating + count that the Bayesian score uses (shown on the page).
    cur.execute("select round(avg(rating)::numeric, 2) as c_mean, count(*) as n from v_current")
    s = cur.fetchone()
    out["city_avg_rating"] = float(s["c_mean"]) if s and s["c_mean"] is not None else None
    out["restaurant_count"] = int(s["n"]) if s and s["n"] is not None else 0
    out["bayes_m"] = 400

    cur.execute("select * from v_all limit %s", (LIMIT,))
    rows = cur.fetchall()
    for r in rows:                       # JSON-friendly types
        if r.get("growth_rate") is not None:
            r["growth_rate"] = float(r["growth_rate"])
        if r.get("score") is not None:
            r["score"] = float(r["score"])
    out["restaurants"] = rows

    cur.close(); conn.close()

    os.makedirs("public", exist_ok=True)
    with open("public/rankings.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"), default=str)
    print(f"Wrote public/rankings.json  ({len(rows)} restaurants)")

if __name__ == "__main__":
    main()
