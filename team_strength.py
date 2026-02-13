import os
import json
import math
from datetime import datetime, timezone, timedelta

import requests

API_KEY = os.getenv("FOOTBALL_DATA_API_KEY", "3c139115487a45faa9ed84c633120c21")
HEADERS = {"X-Auth-Token": API_KEY}

BASE = "https://api.football-data.org/v4"

LEAGUES = {
    "PL": "Premier League",
    "PD": "LaLiga",
    "SA": "Serie A",
    "BL1": "Bundesliga",
    "FL1": "Ligue 1",
    "LPF": "Argentina LPF",  # ojo: puede no estar en tu plan; si falla, se saltea
}

# Ventana de partidos futuros que vamos a analizar (próximos N días)
LOOKAHEAD_DAYS = 10

# Umbrales (ajustables)
THRESH_AWAY_WIN = 0.62
THRESH_HOME_WIN = 0.62
THRESH_UNDER25 = 0.65
THRESH_OVER25 = 0.55
THRESH_BTTS_NO = 0.65
THRESH_BTTS_YES = 0.58

MAX_GOALS = 10  # para Poisson


def parse_utc(dt_str: str) -> datetime:
    # Ej: "2026-02-12T20:00:00Z"
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00")).astimezone(timezone.utc)


def poisson_pmf(k: int, lam: float) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def probs_1x2(lh: float, la: float) -> dict:
    # matriz goles
    home_win = 0.0
    draw = 0.0
    away_win = 0.0

    ph = [poisson_pmf(i, lh) for i in range(MAX_GOALS + 1)]
    pa = [poisson_pmf(j, la) for j in range(MAX_GOALS + 1)]

    for i in range(MAX_GOALS + 1):
        for j in range(MAX_GOALS + 1):
            p = ph[i] * pa[j]
            if i > j:
                home_win += p
            elif i == j:
                draw += p
            else:
                away_win += p

    # normalización leve por truncamiento
    s = home_win + draw + away_win
    if s > 0:
        home_win /= s
        draw /= s
        away_win /= s

    return {"home": home_win, "draw": draw, "away": away_win}


def prob_over_under_25(lh: float, la: float) -> dict:
    # Total goals = i+j. Over 2.5 => total >= 3
    ph = [poisson_pmf(i, lh) for i in range(MAX_GOALS + 1)]
    pa = [poisson_pmf(j, la) for j in range(MAX_GOALS + 1)]

    under = 0.0
    over = 0.0
    for i in range(MAX_GOALS + 1):
        for j in range(MAX_GOALS + 1):
            p = ph[i] * pa[j]
            if (i + j) <= 2:
                under += p
            else:
                over += p

    s = under + over
    if s > 0:
        under /= s
        over /= s
    return {"under_25": under, "over_25": over}


def prob_btts(lh: float, la: float) -> dict:
    ph = [poisson_pmf(i, lh) for i in range(MAX_GOALS + 1)]
    pa = [poisson_pmf(j, la) for j in range(MAX_GOALS + 1)]

    yes = 0.0
    no = 0.0
    for i in range(MAX_GOALS + 1):
        for j in range(MAX_GOALS + 1):
            p = ph[i] * pa[j]
            if i >= 1 and j >= 1:
                yes += p
            else:
                no += p

    s = yes + no
    if s > 0:
        yes /= s
        no /= s
    return {"btts_yes": yes, "btts_no": no}


def fair_odds(p: float) -> float | None:
    if p <= 0:
        return None
    return round(1.0 / p, 2)


def fetch_matches(league_code: str) -> list[dict]:
    url = f"{BASE}/competitions/{league_code}/matches"
    r = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"{league_code}: HTTP {r.status_code} -> {r.text[:200]}")
    data = r.json()
    return data.get("matches", [])


def split_matches(matches: list[dict]):
    finished = []
    upcoming = []

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=LOOKAHEAD_DAYS)

    for m in matches:
        status = m.get("status")
        utc = m.get("utcDate")
        if not utc:
            continue

        try:
            dt = parse_utc(utc)
        except Exception:
            continue

        if status == "FINISHED":
            finished.append(m)
        else:
            # TIMED / SCHEDULED / POSTPONED etc -> nos quedamos con futuros cercanos
            if now <= dt <= horizon:
                upcoming.append(m)

    return finished, upcoming


def league_averages(finished_matches: list[dict]) -> tuple[float, float]:
    home_goals = 0
    away_goals = 0
    n = 0

    for m in finished_matches:
        sc = m.get("score", {}).get("fullTime", {})
        hg = sc.get("home")
        ag = sc.get("away")
        if hg is None or ag is None:
            continue
        home_goals += hg
        away_goals += ag
        n += 1

    # fallback si no hay datos
    if n == 0:
        return (1.45, 1.20)

    return (home_goals / n, away_goals / n)


def team_strengths(finished_matches: list[dict], avg_home: float, avg_away: float) -> dict:
    # Calcula promedios home/away por equipo y los transforma en factores (attack/defense)
    stats = {}
    for m in finished_matches:
        home = m["homeTeam"]["name"]
        away = m["awayTeam"]["name"]
        sc = m.get("score", {}).get("fullTime", {})
        hg = sc.get("home")
        ag = sc.get("away")
        if hg is None or ag is None:
            continue

        stats.setdefault(home, {"hs": 0, "hc": 0, "hg": 0, "as": 0, "ac": 0, "ag": 0})
        stats.setdefault(away, {"hs": 0, "hc": 0, "hg": 0, "as": 0, "ac": 0, "ag": 0})

        # home team
        stats[home]["hs"] += hg
        stats[home]["hc"] += ag
        stats[home]["hg"] += 1

        # away team
        stats[away]["as"] += ag
        stats[away]["ac"] += hg
        stats[away]["ag"] += 1

    strengths = {}
    for team, s in stats.items():
        hg = s["hg"]
        ag = s["ag"]

        # evitar div/0
        if hg == 0:
            # casi no debería pasar, pero por las dudas
            attack_home = 1.0
            defense_home = 1.0
        else:
            attack_home = (s["hs"] / hg) / avg_home
            defense_home = (s["hc"] / hg) / avg_away

        if ag == 0:
            attack_away = 1.0
            defense_away = 1.0
        else:
            attack_away = (s["as"] / ag) / avg_away
            defense_away = (s["ac"] / ag) / avg_home

        strengths[team] = {
            "attack_home": attack_home,
            "defense_home": defense_home,
            "attack_away": attack_away,
            "defense_away": defense_away,
        }

    return strengths


def expected_goals(home: str, away: str, strengths: dict, avg_home: float, avg_away: float) -> tuple[float, float]:
    # fallback neutral si faltan datos
    h = strengths.get(home, {"attack_home": 1, "defense_home": 1})
    a = strengths.get(away, {"attack_away": 1, "defense_away": 1})

    attack_home = h.get("attack_home", 1.0)
    defense_home = h.get("defense_home", 1.0)

    attack_away = a.get("attack_away", 1.0)
    defense_away = a.get("defense_away", 1.0)

    # modelo clásico: λ_home = avg_home * attack_home * defense_away
    lh = avg_home * attack_home * defense_away
    la = avg_away * attack_away * defense_home

    # clamps suaves para evitar locuras por pocos partidos
    lh = max(0.2, min(4.5, lh))
    la = max(0.2, min(4.5, la))

    return lh, la


def build_candidates(probs: dict) -> list[dict]:
    cands = []

    ph = probs["home"]
    pd = probs["draw"]
    pa = probs["away"]

    under = probs["under_25"]
    over = probs["over_25"]

    by = probs["btts_yes"]
    bn = probs["btts_no"]

    if pa >= THRESH_AWAY_WIN:
        cands.append({"market": "Away Win", "prob": round(pa, 3), "fair": fair_odds(pa)})
    if ph >= THRESH_HOME_WIN:
        cands.append({"market": "Home Win", "prob": round(ph, 3), "fair": fair_odds(ph)})

    if under >= THRESH_UNDER25:
        cands.append({"market": "Under 2.5", "prob": round(under, 3), "fair": fair_odds(under)})
    if over >= THRESH_OVER25:
        cands.append({"market": "Over 2.5", "prob": round(over, 3), "fair": fair_odds(over)})

    if bn >= THRESH_BTTS_NO:
        cands.append({"market": "BTTS No", "prob": round(bn, 3), "fair": fair_odds(bn)})
    if by >= THRESH_BTTS_YES:
        cands.append({"market": "BTTS Yes", "prob": round(by, 3), "fair": fair_odds(by)})

    # orden por prob desc
    cands.sort(key=lambda x: x["prob"], reverse=True)
    return cands


def run_league(league_code: str):
    print(f"\n=== {league_code} • {LEAGUES.get(league_code,'League')} ===")

    matches = fetch_matches(league_code)
    finished, upcoming = split_matches(matches)

    avg_home, avg_away = league_averages(finished)
    strengths = team_strengths(finished, avg_home, avg_away)

    daily_matches = []
    daily_picks = []

    for m in sorted(upcoming, key=lambda x: x.get("utcDate", "")):
        home = m["homeTeam"]["name"]
        away = m["awayTeam"]["name"]
        utc = m.get("utcDate")

        lh, la = expected_goals(home, away, strengths, avg_home, avg_away)

        p1x2 = probs_1x2(lh, la)
        pou = prob_over_under_25(lh, la)
        pbtts = prob_btts(lh, la)

        probs = {
            "home": round(p1x2["home"], 3),
            "draw": round(p1x2["draw"], 3),
            "away": round(p1x2["away"], 3),
            "under_25": round(pou["under_25"], 3),
            "over_25": round(pou["over_25"], 3),
            "btts_yes": round(pbtts["btts_yes"], 3),
            "btts_no": round(pbtts["btts_no"], 3),
        }

        item = {
            "league": league_code,
            "home": home,
            "away": away,
            "utcDate": utc,
            "xg_home": round(lh, 2),
            "xg_away": round(la, 2),
            "xg_total": round(lh + la, 2),
            "probs": probs,
        }

        daily_matches.append(item)

        candidates = build_candidates({
            "home": p1x2["home"],
            "draw": p1x2["draw"],
            "away": p1x2["away"],
            "under_25": pou["under_25"],
            "over_25": pou["over_25"],
            "btts_yes": pbtts["btts_yes"],
            "btts_no": pbtts["btts_no"],
        })

        if candidates:
            pick_item = dict(item)
            pick_item["candidates"] = candidates
            daily_picks.append(pick_item)

    # guardar por liga
    matches_path = f"daily_matches_{league_code}.json"
    picks_path = f"daily_picks_{league_code}.json"

    with open(matches_path, "w", encoding="utf-8") as f:
        json.dump(daily_matches, f, ensure_ascii=False, indent=2)

    with open(picks_path, "w", encoding="utf-8") as f:
        json.dump(daily_picks, f, ensure_ascii=False, indent=2)

    print(f"Finished matches used: {len(finished)} | Upcoming in {LOOKAHEAD_DAYS}d: {len(daily_matches)}")
    print(f"League avg home: {avg_home:.2f} | away: {avg_away:.2f}")
    print(f"Saved: {matches_path} ({len(daily_matches)})")
    print(f"Saved: {picks_path} ({len(daily_picks)}) ✅")


def main():
    if not API_KEY:
        print("ERROR: FOOTBALL_DATA_API_KEY no está seteada.")
        print("En Windows PowerShell (local):  set FOOTBALL_DATA_API_KEY=TU_KEY")
        return

    for code in LEAGUES.keys():
        try:
            run_league(code)
        except Exception as e:
            print(f"⚠️  {code} skipped -> {e}")


if __name__ == "__main__":
    main()
