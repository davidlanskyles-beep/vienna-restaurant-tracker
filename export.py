#!/usr/bin/env python3
"""
Read the three ranking views and write a single rankings.json.
The static frontend reads this file directly — no server needed.

Env:  DATABASE_URL
Run:  python export.py   ->   writes ./public/rankings.json
"""
import os, json, datetime as dt
import psycopg2
from psycopg2.extras import RealDictCursor

DB_URL = os.environ["DATABASE_URL"]
LIMIT = 50  # per section

QUERIES = {
    "blue_chips":   "select * from v_blue_chips   limit %s",
    "rising_stars": "select * from v_rising_stars limit %s",
    "breakouts":    "select * from v_breakouts    limit %s",
}

def main():
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    out = {"generated_at": dt.datetime.utcnow().isoformat() + "Z"}
    cur.execute("select week_start_date from v_latest_week")
    row = cur.fetchone()
    out["week_start_date"] = str(row["week_start_date"]) if row and row["week_start_date"] else None
    for key, sql in QUERIES.items():
        cur.execute(sql, (LIMIT,))
        rows = cur.fetchall()
        for r in rows:                       # JSON-friendly types
            if r.get("growth_rate") is not None:
                r["growth_rate"] = float(r["growth_rate"])
        out[key] = rows
    cur.close(); conn.close()

    os.makedirs("public", exist_ok=True)
    with open("public/rankings.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print(f"Wrote public/rankings.json  "
          f"(blue={len(out['blue_chips'])} rising={len(out['rising_stars'])} "
          f"break={len(out['breakouts'])})")

if __name__ == "__main__":
    main()
