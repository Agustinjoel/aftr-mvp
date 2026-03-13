# AFTR production data: what the home/dashboard needs

## 1. Data sources used by the home/dashboard

| What you see | Source | How it’s loaded |
|--------------|--------|------------------|
| **ROI global** | Settled + pending picks | `_load_all_leagues_data()` → `daily_picks_*.json` → computed |
| **Profit neto (net)** | Settled picks only | Same picks → `_unit_delta(p)` (profit_units / net_units) |
| **Winrate** | Settled picks (W/L) | Same picks → count WIN/LOSS, then % |
| **Picks totales** | All picks | `len(all_settled) + len(all_upcoming)` from same picks |
| **Chart history (last 7 days)** | Settled picks by day | `group_picks_recent_by_day_desc(all_settled, days=7)` → `_roi_spark_points()` |
| **Combos (Safe / Medium / Aggressive)** | Built in memory | `_build_combos_by_tier(all_upcoming, match_by_key)` from picks + matches |
| **Top picks** | Pending picks (today + 2 days) | Same `all_upcoming` + `match_by_key` |
| **Big matches today** | Matches + picks | `daily_matches_*.json` + `daily_picks_*.json` |
| **Featured league cards** | Per-league picks | Same `picks_by_league` from `daily_picks_*.json` |

All of the above come from **JSON cache only**. The SQLite DB is used for **users, auth, subscriptions** (login, premium, Stripe). It is **not** used for ROI, winrate, combos, or chart.

---

## 2. Where values come from (code path)

- **ROI / winrate / net / total picks**  
  `app/ui.py` → `home_page()` → `_load_all_leagues_data()` → `data.cache.read_json("daily_picks_{code}.json")` for each league in `settings.leagues`. Then filtered into `all_settled` / `all_upcoming` and aggregated.

- **Chart**  
  Same `all_settled` → `group_picks_recent_by_day_desc(..., days=7)` → `_roi_spark_points()`.

- **Combos (home)**  
  Same `all_upcoming` and `match_by_key` (from `daily_matches_*.json`) → `_build_combos_by_tier()` in memory.  
  The API `GET /api/combos` uses `daily_combos.json`; the **home page** does not use that file.

- **Top picks / big matches / featured**  
  Same `all_upcoming`, `match_by_key`, `picks_by_league`, `matches_by_league` from the same JSON files.

- **Refresh pipeline**  
  `services/refresh.py` → `refresh_league(code)` and (for basketball) `services/refresh_basketball.py` → write:
  - `data/cache/daily_matches_{code}.json`
  - `data/cache/daily_picks_{code}.json`  
  Optional: `daily_combos.json` (only if `_build_and_save_combos()` is run after refresh).

---

## 3. Exact files/tables required for production to look like local

**JSON (required for rich home/dashboard):**

- `data/cache/daily_matches_{code}.json` — one per league you use (e.g. PL, PD, SA, CL, NBA, BL1, FL1, ELC, DED, PPL, BSA, EC, WC, CLI).
- `data/cache/daily_picks_{code}.json` — same leagues.

League list comes from `config.settings` → `LEAGUES` (and thus `settings.league_codes()`). Featured leagues on the home are a subset: PL, CL, PD, SA, NBA.

**Optional:**

- `data/cache/daily_combos.json` — only if something calls the combos API; the **home combos section** does not depend on it.

**SQLite:**

- Used only for users/subscriptions (e.g. `aftr.db` or `AFTR_DB_PATH`). Not required for ROI, winrate, chart, or combos on the home page.

---

## 4. Why Render looks empty

- **`data/cache/` is in `.gitignore`**, so it is not in the repo. On Render the app runs from a clean clone with **no** `daily_picks_*.json` or `daily_matches_*.json`.
- `read_json(...)` then returns `[]` for every league, so:
  - `all_settled` = [], `all_upcoming` = []
  - ROI = 0%, winrate missing, total picks = 0, net = 0
  - Chart has no points, combos empty, top picks empty, big matches empty, featured empty
- **SQLite**: If you don’t set `AFTR_DB_PATH`, the app may still create a new DB on the ephemeral filesystem; that only affects users/auth, not the dashboard numbers above.

So production is missing **all JSON cache files** that feed the home/dashboard.

---

## 5. Fastest safe fix (options)

**Option A – Run refresh on every startup (good for ephemeral disk)**  
Populate `data/cache/` by running the existing refresh before starting the web server. No new business logic.

- **Render start command:**
  ```bash
  python -m app.cli refresh && uvicorn app.main:app --host 0.0.0.0 --port $PORT
  ```
- **Requires** env vars: `FOOTBALL_DATA_API_KEY`, `API_SPORTS_KEY` (for NBA), and optionally `ODDS_API_KEY`. Without them, some leagues may be skipped or have no odds.
- **Trade-off:** Startup can take 1–3+ minutes (many leagues/APIs). Render may show “Deploying…” until refresh finishes. After restart/redeploy, cache is lost and refresh runs again.

**Option B – Persistent disk + one-time or scheduled refresh**  
Attach a Render persistent disk, point cache (and optionally DB) there, run refresh once (or on a cron):

- Set `AFTR_DB_PATH` to a path on the disk if you want the DB to persist.
- Either:
  - Add an env var for cache directory (e.g. `AFTR_CACHE_DIR`) and use it in `config.settings` / `data/cache.py`, then run `python -m app.cli refresh` once after deploy, or
  - Mount the disk so that the project’s `data/cache` lives on the disk (e.g. mount at `./data` and ensure `data/cache` exists).
- Then run refresh once manually or from a background worker/cron so `daily_picks_*.json` and `daily_matches_*.json` are created. After that, the dashboard will look like local until the next refresh.

**Option C – Seed from local**  
Copy your local `data/cache/daily_picks_*.json` and `daily_matches_*.json` into the repo (e.g. in a `seed/` folder) or upload them to S3/storage and add a deploy step that copies them into `data/cache/`. No API keys needed for that step; data will age until you run refresh or re-seed.

---

## 6. Exact commands to reproduce local state on Render

**If using Option A (startup refresh):**

1. In Render dashboard, set **Start Command** to:
   ```bash
   python -m app.cli refresh && uvicorn app.main:app --host 0.0.0.0 --port $PORT
   ```
2. Set env vars: `FOOTBALL_DATA_API_KEY`, `API_SPORTS_KEY`, and optionally `ODDS_API_KEY`, `AFTR_DB_PATH`, etc.
3. Deploy. First boot will run refresh then start the server; the home page will then show ROI, winrate, combos, chart, top picks, and featured leagues like local (for the data that the APIs return).

**If using Option B (persistent disk):**

1. Attach a persistent disk and set `AFTR_DB_PATH` (and optionally `AFTR_CACHE_DIR` if you add support for it) to paths on that disk.
2. Run refresh once (e.g. via shell or a one-off job):
   ```bash
   python -m app.cli refresh
   ```
3. Start the app as usual:
   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port $PORT
   ```

**Reproduce local state locally (for reference):**

```bash
# From project root
python -m app.cli refresh
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 7. Summary

| Missing in production | Exact source | Fix |
|------------------------|--------------|-----|
| ROI / winrate / net / total picks | `data/cache/daily_picks_*.json` (all leagues) | Run refresh at startup or populate cache (disk/seed) |
| Chart history | Same picks (last 7 days settled) | Same |
| Combos (home) | Same picks + `daily_matches_*.json` | Same |
| Top picks / big matches / featured | Same | Same |
| Users/auth (optional) | SQLite at `AFTR_DB_PATH` | Set `AFTR_DB_PATH` and use persistent disk if you want DB to survive restarts |

No UI or business logic changes are required; only ensuring the same JSON cache (and optionally DB path) is available in production.
