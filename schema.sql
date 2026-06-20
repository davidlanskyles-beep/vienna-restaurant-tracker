-- Vienna Restaurant Trend Tracker — schema + ranking views
-- Target: Supabase / Postgres 14+
-- Run once: psql "$DATABASE_URL" -f schema.sql

create extension if not exists "pgcrypto";

-- ── Core tables ─────────────────────────────────────────────────────────────

create table if not exists restaurants (
  id            uuid primary key default gen_random_uuid(),
  place_id      text unique not null,
  name          text not null,
  primary_type  text,                       -- Google primaryType, e.g. italian_restaurant
  business_status text default 'OPERATIONAL',
  latitude      double precision,
  longitude     double precision,
  first_seen    date not null default current_date,
  last_seen     date not null default current_date
);

create table if not exists weekly_snapshots (
  id              uuid primary key default gen_random_uuid(),
  place_id        text not null references restaurants(place_id) on delete cascade,
  week_start_date date not null,
  rating          double precision,
  review_count    integer,
  -- one row per restaurant per week → makes the weekly job safely re-runnable
  unique (place_id, week_start_date)
);

create index if not exists idx_snap_place  on weekly_snapshots (place_id);
create index if not exists idx_snap_week   on weekly_snapshots (week_start_date);
create index if not exists idx_resto_status on restaurants (business_status);

-- ── Config knobs ─────────────────────────────────────────────────────────────
-- This build runs MONTHLY. Each restaurant gets one snapshot per month, stored
-- in week_start_date (the column name is historical; it holds the 1st of the
-- month). Momentum = this month vs last month. v_prior uses wk_rank = 2 = the
-- immediately previous month.

-- ── Helper: the most-recent and comparison snapshot per restaurant ───────────
-- Latest week present in the data:
create or replace view v_latest_week as
  select max(week_start_date) as week_start_date from weekly_snapshots;

-- Distinct weeks, newest first, numbered (1 = latest):
create or replace view v_weeks_ranked as
  select week_start_date,
         row_number() over (order by week_start_date desc) as wk_rank
  from (select distinct week_start_date from weekly_snapshots) d;

-- Current snapshot (latest week), operational restaurants only:
create or replace view v_current as
  select s.place_id, r.name, r.primary_type, s.rating, s.review_count,
         s.week_start_date
  from weekly_snapshots s
  join restaurants r using (place_id)
  join v_weeks_ranked w on w.week_start_date = s.week_start_date and w.wk_rank = 1
  where r.business_status = 'OPERATIONAL'
    and s.review_count is not null;

-- Comparison snapshot = previous month (wk_rank = 2).
create or replace view v_prior as
  select s.place_id, s.rating, s.review_count, s.week_start_date
  from weekly_snapshots s
  join v_weeks_ranked w on w.week_start_date = s.week_start_date and w.wk_rank = 2;

-- Current + growth, joined. growth can be negative (Google prunes spam reviews).
create or replace view v_growth as
  select c.*,
         p.review_count as review_count_prior,
         (c.review_count - p.review_count) as growth_abs,
         case when p.review_count > 0
              then (c.review_count - p.review_count)::numeric / p.review_count
              else null end as growth_rate
  from v_current c
  left join v_prior p using (place_id);

-- ── 6.1 Blue Chips — top 15% on BOTH rating and review_count ─────────────────
create or replace view v_blue_chips as
with thresholds as (
  select percentile_cont(0.85) within group (order by rating)       as rating_thr,
         percentile_cont(0.85) within group (order by review_count) as review_thr
  from v_current
)
select g.name, g.rating, g.review_count, g.primary_type,
       g.growth_abs, g.growth_rate
from v_growth g, thresholds t
where g.rating >= t.rating_thr
  and g.review_count >= t.review_thr
order by g.review_count desc;

-- ── 6.2 Rising Stars — quality + fastest absolute review growth ──────────────
create or replace view v_rising_stars as
with thr as (
  select percentile_cont(0.60) within group (order by rating) as rating_thr
  from v_current
)
select g.name, g.rating, g.review_count, g.primary_type,
       g.growth_abs, g.growth_rate
from v_growth g, thr
where g.rating >= thr.rating_thr
  and g.growth_abs is not null
  and g.growth_abs > 0
order by g.growth_abs desc;

-- ── 6.3 Breakouts — small/mid places accelerating fastest ────────────────────
-- Floor of +10 absolute reviews kills divide-by-small-number noise.
create or replace view v_breakouts as
select g.name, g.rating, g.review_count, g.primary_type,
       g.growth_abs, g.growth_rate
from v_growth g
where g.review_count between 100 and 1500
  and g.growth_abs is not null
  and g.growth_abs >= 10
  and g.growth_rate is not null
order by g.growth_rate desc;
