import os
import math
import json
import sqlite3
import requests
from datetime import datetime, timedelta, timezone

# =========================
# CONFIG
# =========================
API_KEY = os.getenv("FOOTBALL_DATA_API_KEY", "").strip()
if not API_KEY:
    raise SystemExit("ERROR: FOOTBALL_DATA_API_KEY no está seteada.")

HEADERS = {"X-Auth-Token": API_KEY}
BASE = "https://api.football-data.org/v4"

DB_PATH = os.getenv("AFTR_DB_PATH", "aftr.db")

# Ligas / Copas (football-data.org)
# OJO: algunas pueden tirar 404 según plan/coverage -> NO rompe, se skippea.
LEAGUES = {
    "PL": "Premier League",
    "PD": "LaLiga",
    "SA": "Serie A",
    "BL1": "Bundesliga",
    "FL1": "Ligue 1",
    "CL": "UEFA Champions League",
    "EL": "UEFA Europa League",
    "FAC": "FA Cup",
    # "CDR": "Copa del Rey",  # suele no estar -> si querés probar, descomentá y listo
    # "LPF": "Argentina LPF", # 404 seguro en football-data -> no recomendado
}

DAYS_BACK = 180
DAYS_AHEAD = 10

# Picks:
MAX_PICKS_PER_LEAGUE = 12
MIN_PROB_FOR_CANDIDATE = 0.60


# =========================
# DB
# =========================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS meta (
      key TEXT PRIMARY KEY,
      value TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS matches (
      league TEXT,
      match_id TEXT,
      utcDate TEXT,
      status TEXT,
      home TEXT,
      away TEXT,
      home_goals INTEGER,
      away_goals INTEGER,
      crest_home TEXT,
      crest_away TEXT,
      xg_home REAL,
      xg_away REAL,
      xg_total REAL,
      probs_json TEXT,
      updated_at TEXT,
      PRIMARY KEY (league, match_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS picks (
      league TEXT,
      match_id TEXT,
      market TEXT,
      prob REAL,
      fair REAL,
      rationale TEXT,
      status TEXT,         -- PENDING / WIN / LOSS
      updated_at TEXT,
      PRIMARY KEY (league, match_id, market)
    )
    """)

    conn.commit()
    conn.close()


def set_meta(key, value):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()
    conn.close()


def upsert_match(row):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
    INSERT INTO matches(
      league, match_id, utcDate, status, home, away,
      home_goals, away_goals, crest_home, crest_away,
      xg_home, xg_away, xg_total, probs_json, updated_at
    )
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(league, match_id) DO UPDATE SET
      utcDate=excluded.utcDate,
      status=excluded.status,
      home=excluded.home,
      away=excluded.away,
      home_goals=excluded.home_goals,
      away_goals=excluded.away_goals,
      crest_home=excluded.crest_home,
      crest_away=excluded.crest_away,
      xg_home=excluded.xg_home,
      xg_away=excluded.xg_away,
      xg_total=excluded.xg_total,
      probs_json=excluded.probs_json,
      updated_at=excluded.updated_at
    """,
        (
            row["league"],
            row["match_id"],
            row["utcDate"],
            row["status"],
            row["home"],
            row["away"],
            row["home_goals"],
            row["away_goals"],
            row["crest_home"],
            row["crest_away"],
            row["xg_home"],
            row["xg_away"],
            row["xg_total"],
            row["probs_json"],
            row["updated_at"],
        ),
    )
    conn.commit()
    conn.close()


def upsert_pick(row):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
    INSERT INTO picks(
      league, match_id, market, prob, fair, rationale, status, updated_at
    )
    VALUES(?,?,?,?,?,?,?,?)
    ON CONFLICT(league, match_id, market) DO UPDATE SET
      prob=excluded.prob,
      fair=excluded.fair,
      rationale=excluded.rationale,
      status=excluded.status,
      updated_at=excluded.updated_at
    """,
        (
            row["league"],
            row["match_id"],
            row["market"],
            row["prob"],
            row["fair"],
            row["rationale"],
            row["status"],
            row["updated_at"],
        ),
    )
    conn.commit()
    conn.close()


# =========================
# API fetch
# =========================
def fetch_matches_range(competition, date_from, date_to):
    url = f"{BASE}/competitions/{competition}/matches"
    params = {"dateFrom": date_from, "dateTo": date_to}
    r = requests.get(url, headers=HEADERS, params=params, timeout=25)
    if not r.ok:
        try:
            j = r.json()
        except Exception:
            j = r.text
        print(f"⚠️  {competition}: HTTP {r.status_code} -> {j}")
        r.raise_for_status()
    return (r.json() or {}).get("matches", [])


def now_utc():
    return datetime.now(timezone.utc)


def iso_date(d: datetime):
    return d.date().isoformat()


# =========================
# Model (simple Poisson xG)
# =========================
def poisson_pmf(lmbda, k):
    return math.exp(-lmbda) * (lmbda**k) / math.factorial(k)


def match_probs(xg_home, xg_away, max_goals=8):
    # 1X2
    p_home = 0.0
    p_draw = 0.0
    p_away = 0.0
    # Totals / BTTS
    p_under25 = 0.0
    p_over25 = 0.0
    p_btts_yes = 0.0
    p_btts_no = 0.0

    for h in range(max_goals + 1):
        ph = poisson_pmf(xg_home, h)
        for a in range(max_goals + 1):
            pa = poisson_pmf(xg_away, a)
            p = ph * pa

            if h > a:
                p_home += p
            elif h == a:
                p_draw += p
            else:
                p_away += p

            total = h + a
            if total <= 2:
                p_under25 += p
            else:
                p_over25 += p

            if h > 0 and a > 0:
                p_btts_yes += p
            else:
                p_btts_no += p

    # normalize small numeric drift
    s = p_home + p_draw + p_away
    if s > 0:
        p_home /= s
        p_draw /= s
        p_away /= s

    return {
        "home": round(p_home, 3),
        "draw": round(p_draw, 3),
        "away": round(p_away, 3),
        "under_25": round(p_under25, 3),
        "over_25": round(p_over25, 3),
        "btts_yes": round(p_btts_yes, 3),
        "btts_no": round(p_btts_no, 3),
    }


def fair_odds(prob):
    if prob <= 0:
        return None
    return round(1.0 / prob, 2)


def safe_team_name(t):
    return (t or "").strip()


def extract_score(match):
    sc = (match.get("score") or {}).get("fullTime") or {}
    hg = sc.get("home")
    ag = sc.get("away")
    return hg, ag


def extract_crests(match):
    # football-data suele tener crest dentro de homeTeam/awayTeam
    ht = match.get("homeTeam") or {}
    at = match.get("awayTeam") or {}
    return ht.get("crest"), at.get("crest")


# =========================
# Strength calc
# =========================
def league_finished_stats(matches):
    home_goals = 0
    away_goals = 0
    games = 0

    for m in matches:
        if m.get("status") != "FINISHED":
            continue
        hg, ag = extract_score(m)
        if hg is None or ag is None:
            continue
        home_goals += int(hg)
        away_goals += int(ag)
        games += 1

    if games == 0:
        return 1.4, 1.1, 0

    return (home_goals / games), (away_goals / games), games


def team_strengths(matches, league_avg_home, league_avg_away):
    # totals per team
    stats = {}

    def ensure(team):
        if team not in stats:
            stats[team] = {
                "home_scored": 0,
                "home_conceded": 0,
                "home_games": 0,
                "away_scored": 0,
                "away_conceded": 0,
                "away_games": 0,
            }

    for m in matches:
        if m.get("status") != "FINISHED":
            continue
        hg, ag = extract_score(m)
        if hg is None or ag is None:
            continue

        home = safe_team_name((m.get("homeTeam") or {}).get("name"))
        away = safe_team_name((m.get("awayTeam") or {}).get("name"))
        if not home or not away:
            continue

        ensure(home)
        ensure(away)

        stats[home]["home_scored"] += int(hg)
        stats[home]["home_conceded"] += int(ag)
        stats[home]["home_games"] += 1

        stats[away]["away_scored"] += int(ag)
        stats[away]["away_conceded"] += int(hg)
        stats[away]["away_games"] += 1

    # normalize into attack/defense factors
    strength = {}
    for team, s in stats.items():
        if s["home_games"] == 0 or s["away_games"] == 0:
            continue
        attack_home = (s["home_scored"] / s["home_games"]) / max(league_avg_home, 0.01)
        defense_home = (s["home_conceded"] / s["home_games"]) / max(
            league_avg_away, 0.01
        )
        attack_away = (s["away_scored"] / s["away_games"]) / max(league_avg_away, 0.01)
        defense_away = (s["away_conceded"] / s["away_games"]) / max(
            league_avg_home, 0.01
        )

        strength[team] = {
            "attack_home": attack_home,
            "defense_home": defense_home,
            "attack_away": attack_away,
            "defense_away": defense_away,
        }

    return strength


def expected_goals(home, away, strength, league_avg_home, league_avg_away):
    # fallback if missing
    sh = strength.get(home)
    sa = strength.get(away)
    if not sh or not sa:
        return league_avg_home, league_avg_away

    xg_h = league_avg_home * sh["attack_home"] * sa["defense_away"]
    xg_a = league_avg_away * sa["attack_away"] * sh["defense_home"]

    # sanity clamp
    xg_h = max(0.1, min(float(xg_h), 4.0))
    xg_a = max(0.1, min(float(xg_a), 4.0))
    return xg_h, xg_a


# =========================
# Picks selection + evaluation
# =========================
def pick_candidates(probs):
    cand = []
    # choose strongest markets only
    cand.append(("Home Win", probs["home"]))
    cand.append(("Draw", probs["draw"]))
    cand.append(("Away Win", probs["away"]))
    cand.append(("Under 2.5", probs["under_25"]))
    cand.append(("Over 2.5", probs["over_25"]))
    cand.append(("BTTS No", probs["btts_no"]))
    cand.append(("BTTS Yes", probs["btts_yes"]))

    # filter by prob threshold
    out = []
    for mkt, p in cand:
        if p >= MIN_PROB_FOR_CANDIDATE:
            out.append((mkt, p, fair_odds(p)))
    # sort by prob desc
    out.sort(key=lambda x: x[1], reverse=True)
    return out


def rationale_from_probs(probs, xg_home, xg_away):
    # short transparent drivers
    total = xg_home + xg_away
    return f"xG {xg_home:.2f}-{xg_away:.2f} (total {total:.2f}) | 1X2 H {probs['home']:.3f} D {probs['draw']:.3f} A {probs['away']:.3f} | U2.5 {probs['under_25']:.3f} | BTTS No {probs['btts_no']:.3f}"


def eval_pick(market, hg, ag):
    # return "WIN"/"LOSS"
    if hg is None or ag is None:
        return "PENDING"
    hg = int(hg)
    ag = int(ag)

    if market == "Home Win":
        return "WIN" if hg > ag else "LOSS"
    if market == "Away Win":
        return "WIN" if ag > hg else "LOSS"
    if market == "Draw":
        return "WIN" if hg == ag else "LOSS"
    if market == "Under 2.5":
        return "WIN" if (hg + ag) <= 2 else "LOSS"
    if market == "Over 2.5":
        return "WIN" if (hg + ag) >= 3 else "LOSS"
    if market == "BTTS Yes":
        return "WIN" if (hg > 0 and ag > 0) else "LOSS"
    if market == "BTTS No":
        return "WIN" if (hg == 0 or ag == 0) else "LOSS"
    return "PENDING"


def evaluate_finished_picks():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
      SELECT p.league, p.match_id, p.market,
             m.home_goals, m.away_goals, m.status
      FROM picks p
      JOIN matches m ON m.league=p.league AND m.match_id=p.match_id
      WHERE m.status='FINISHED'
    """)
    rows = cur.fetchall()
    conn.close()

    for r in rows:
        status = eval_pick(r["market"], r["home_goals"], r["away_goals"])
        upsert_pick(
            {
                "league": r["league"],
                "match_id": r["match_id"],
                "market": r["market"],
                "prob": None,
                "fair": None,
                "rationale": None,
                "status": status,
                "updated_at": now_utc().isoformat(),
            }
        )


# =========================
# MAIN
# =========================
def run_competition(code):
    print(f"\n=== {code} • {LEAGUES.get(code, code)} ===")
    d0 = now_utc()
    date_from = iso_date(d0 - timedelta(days=DAYS_BACK))
    date_to = iso_date(d0 + timedelta(days=DAYS_AHEAD))

    try:
        matches = fetch_matches_range(code, date_from, date_to)
    except Exception:
        print(f"⚠️  {code} sin datos (skip).")
        return

    finished = [m for m in matches if m.get("status") == "FINISHED"]
    upcoming = [
        m
        for m in matches
        if m.get("status") in ("SCHEDULED", "TIMED", "IN_PLAY", "PAUSED")
    ]

    league_avg_home, league_avg_away, used = league_finished_stats(matches)
    print(f"Finished matches used: {used} | Upcoming in {DAYS_AHEAD}d: {len(upcoming)}")
    print(f"League avg home: {league_avg_home:.2f} | away: {league_avg_away:.2f}")

    strength = team_strengths(matches, league_avg_home, league_avg_away)

    updated_at = now_utc().isoformat()
    saved_matches = 0
    saved_picks = 0

    for m in upcoming + finished:
        mid = m.get("id")
        if mid is None:
            continue
        mid = str(mid)

        utcDate = m.get("utcDate")
        status = m.get("status")

        home = safe_team_name((m.get("homeTeam") or {}).get("name"))
        away = safe_team_name((m.get("awayTeam") or {}).get("name"))
        crest_home, crest_away = extract_crests(m)

        hg, ag = extract_score(m)
        hg = int(hg) if hg is not None else None
        ag = int(ag) if ag is not None else None

        xg_h, xg_a = expected_goals(
            home, away, strength, league_avg_home, league_avg_away
        )
        probs = match_probs(xg_h, xg_a)
        probs_json = json.dumps(probs, ensure_ascii=False)

        upsert_match(
            {
                "league": code,
                "match_id": mid,
                "utcDate": utcDate,
                "status": status,
                "home": home,
                "away": away,
                "home_goals": hg,
                "away_goals": ag,
                "crest_home": crest_home,
                "crest_away": crest_away,
                "xg_home": float(round(xg_h, 2)),
                "xg_away": float(round(xg_a, 2)),
                "xg_total": float(round(xg_h + xg_a, 2)),
                "probs_json": probs_json,
                "updated_at": updated_at,
            }
        )
        saved_matches += 1

        # Candidates only for upcoming (predict)
        if status in ("SCHEDULED", "TIMED"):
            cands = pick_candidates(probs)
            if cands:
                # pick top 1 per match (clean MVP)
                market, p, fair = cands[0]
                rationale = rationale_from_probs(probs, xg_h, xg_a)
                upsert_pick(
                    {
                        "league": code,
                        "match_id": mid,
                        "market": market,
                        "prob": float(p),
                        "fair": float(fair) if fair else None,
                        "rationale": rationale,
                        "status": "PENDING",
                        "updated_at": updated_at,
                    }
                )
                saved_picks += 1

    set_meta(f"last_update_{code}", updated_at)
    print(f"Saved matches -> DB: {saved_matches} | Saved picks -> DB: {saved_picks}")


def main():
    init_db()
    for code in LEAGUES.keys():
        run_competition(code)

    evaluate_finished_picks()
    set_meta("last_update_all", now_utc().isoformat())
    print("\n✅ Picks evaluados (WIN/LOSS) donde haya FT.")
    print("✅ SQLite actualizado.")


if __name__ == "__main__":
    main()
