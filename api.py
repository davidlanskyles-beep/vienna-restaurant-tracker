#!/usr/bin/env python3
"""
Optional live API (the spec's GET /blue-chips, /rising-stars, /breakouts).
You don't need this if you use the static rankings.json path — it's here only
if you want live endpoints.

Env:  DATABASE_URL
Run:  uvicorn api:app --reload
"""
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

DB_URL = os.environ["DATABASE_URL"]
app = FastAPI(title="Vienna Restaurant Trend Tracker")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"])

def query(view, limit=50):
    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(f"select * from {view} limit %s", (limit,))
    rows = cur.fetchall()
    for r in rows:
        if r.get("growth_rate") is not None:
            r["growth_rate"] = float(r["growth_rate"])
    cur.close(); conn.close()
    return rows

@app.get("/blue-chips")
def blue_chips(limit: int = 50):   return query("v_blue_chips", limit)

@app.get("/rising-stars")
def rising_stars(limit: int = 50): return query("v_rising_stars", limit)

@app.get("/breakouts")
def breakouts(limit: int = 50):    return query("v_breakouts", limit)
