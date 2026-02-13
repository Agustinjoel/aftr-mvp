import math
import json
import datetime as dt
import requests
import os

# =========================
# CONFIG
# =========================
API_KEY = os.getenv("FOOTBALL_DATA_API_KEY", "")  
HEADERS = {"X-Auth-Token": API_KEY}

COMP_CODE = "PL"  # Premier League
MATCHES_URL = f"https://api.football-data.org/v4/competitions/{COMP_CODE}/matches"

# Ventana de próximos partidos (días)
NEXT_DAYS = 10

# Candidatos (umbral simple para MVP)
MIN_PROB_PICK = 0.62

# Límite de goles en Poisson para aproximar (más alto = más exacto, más lento)
MAX_GOALS = 10


# =========================
# HELPERS
# =========================
def safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


def poisson_pmf(k, lam):
    # P(k;λ) = e^-λ λ^k / k!
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def compute_1x2_probs(lam_home, lam_away, max_goals=MAX_GOALS):
    home_win = 0.0
    draw = 0.0
    away_win = 0.0

    ph = [poisson_pmf(i, lam_home) for i in range(max_goals + 1)]
    pa = [poisson_pmf(j, lam_away) for j in range(max_goals + 1)]

    for i in range(max_goals + 1):
        for j in range(max_goals + 1):
            p = ph[i] * pa[j]
            if i > j:
                home_win += p
            elif i == j:
                draw += p
            else:
                away_win += p

    # Normalización suave (por truncamiento)
    s = home_win + draw + away_win
    if s > 0:
        home_win /= s
        draw /= s
        away_win /= s

    return home_win, draw, away_win


def compute_over_under_25(lam_home, lam_away, max_goals=MAX_GOALS):
    ph = [poisson_pmf(i, lam_home) for i in range(max_goals + 1)]
    pa = [poisson_pmf(j, lam_away) for j in range(max_goals + 1)]

    under = 0.0
    over = 0.0

    for i in range(max_goals + 1):
        for j in range(max_goals + 1):
            p = ph[i] * pa[j]
            if (i + j) <= 2:
                under += p
            else:
                over += p

    s = under + over
    if s > 0:
        under /= s
        over /= s

    return over, under


def compute_btts(lam_home, lam_away, max_goals=MAX_GOALS):
    ph0 = poisson_pmf(0, lam_home)
    pa0 = poisson_pmf(0, lam_away)
    # P(BTTS No) = P(home=0 OR away=0) = P(home=0)+P(away=0)-P(both=0)
    btts_no = ph0 + pa0 - (ph0 * pa0)
    btts_yes = 1.0 - btts_no
    return btts_yes, btts_no


def fair_odds(prob):
    return round(1.0 / prob, 2) if prob > 0 else None


# =========================
# DATA FETCH
# =========================
def fetch_all_matches():
    r = requests.get(MATCHES_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("matches", [])


def split_matches(matches):
    finished = []
    upcoming = []

    today = dt.datetime.utcnow().date()
    end_date = today + dt.timedelta(days=NEXT_DAYS)

    for m in matches:
        status = m.get("status")
        utc_date = m.get("utcDate")

        # Parse fecha
        match_date = None
        if utc_date:
            try:
                match_date = dt.datetime.fromisoformat(utc_date.replace("Z", "+00:00")).date()
            except Exception:
                match_date = None

        if status == "FINISHED":
            finished.append(m)

        # upcoming: scheduled en ventana
        if status in ("SCHEDULED", "TIMED") and match_date:
            if today <= match_date <= end_date:
                upcoming.append(m)

    return finished, upcoming


# =========================
# LEAGUE AVERAGES
# =========================
def compute_league_avgs(finished_matches):
    home_goals_total = 0
    away_goals_total = 0
    games = 0

    for m in finished_matches:
        score = (m.get("score") or {}).get("fullTime") or {}
        hg = score.get("home")
        ag = score.get("away")
        if hg is None or ag is None:
            continue
        home_goals_total += hg
        away_goals_total += ag
        games += 1

    # fallback si no hay data
    if games == 0:
        return 1.50, 1.20

    return home_goals_total / games, away_goals_total / games


# =========================
# TEAM STRENGTHS
# =========================
def compute_team_strengths(finished_matches, league_avg_home, league_avg_away):
    """
    Devuelve:
    strengths[team] = {
      attack_home, defense_home, attack_away, defense_away,
      home_scored_avg, home_conceded_avg, away_scored_avg, away_conceded_avg,
      home_games, away_games
    }
    """
    teams = set()
    for m in finished_matches:
        teams.add(m["homeTeam"]["name"])
        teams.add(m["awayTeam"]["name"])

    # acumuladores
    stats = {}
    for t in teams:
        stats[t] = dict(
            home_scored=0, home_conceded=0, away_scored=0, away_conceded=0,
            home_games=0, away_games=0
        )

    for m in finished_matches:
        home = m["homeTeam"]["name"]
        away = m["awayTeam"]["name"]
        score = (m.get("score") or {}).get("fullTime") or {}
        hg = score.get("home")
        ag = score.get("away")
        if hg is None or ag is None:
            continue

        # home team
        stats[home]["home_scored"] += hg
        stats[home]["home_conceded"] += ag
        stats[home]["home_games"] += 1

        # away team
        stats[away]["away_scored"] += ag
        stats[away]["away_conceded"] += hg
        stats[away]["away_games"] += 1

    strengths = {}
    for team, s in stats.items():
        hg = s["home_games"]
        ag = s["away_games"]

        # si le faltan juegos, saltamos (o podrías fallback a 1.0)
        if hg == 0 or ag == 0:
            continue

        home_scored_avg = s["home_scored"] / hg
        home_conceded_avg = s["home_conceded"] / hg
        away_scored_avg = s["away_scored"] / ag
        away_conceded_avg = s["away_conceded"] / ag

        attack_home = (home_scored_avg / league_avg_home) if league_avg_home > 0 else 1.0
        defense_home = (home_conceded_avg / league_avg_away) if league_avg_away > 0 else 1.0

        attack_away = (away_scored_avg / league_avg_away) if league_avg_away > 0 else 1.0
        defense_away = (away_conceded_avg / league_avg_home) if league_avg_home > 0 else 1.0

        strengths[team] = {
            "attack_home": round(attack_home, 3),
            "defense_home": round(defense_home, 3),
            "attack_away": round(attack_away, 3),
            "defense_away": round(defense_away, 3),
            "home_scored_avg": round(home_scored_avg, 3),
            "home_conceded_avg": round(home_conceded_avg, 3),
            "away_scored_avg": round(away_scored_avg, 3),
            "away_conceded_avg": round(away_conceded_avg, 3),
            "home_games": hg,
            "away_games": ag,
        }

    return strengths


# =========================
# PREDICTIONS + DRIVERS
# =========================
def expected_goals(home, away, strengths, league_avg_home, league_avg_away):
    """
    λ_home = avg_home * attack_home(home) * defense_away(away)
    λ_away = avg_away * attack_away(away) * defense_home(home)
    """
    hs = strengths.get(home)
    as_ = strengths.get(away)

    # fallback suave si faltan equipos
    if not hs or not as_:
        return league_avg_home, league_avg_away

    lam_home = league_avg_home * hs["attack_home"] * as_["defense_away"]
    lam_away = league_avg_away * as_["attack_away"] * hs["defense_home"]
    return lam_home, lam_away


def build_candidates(probs):
    """
    Genera picks candidatos simples (MVP).
    """
    candidates = []

    # 1X2
    for market_key, market_name in [
        ("home", "Home Win"),
        ("draw", "Draw"),
        ("away", "Away Win"),
    ]:
        pr = probs.get(market_key, 0)
        if pr >= MIN_PROB_PICK:
            candidates.append({
                "market": market_name,
                "prob": round(pr, 3),
                "fair": fair_odds(pr),
            })

    # OU 2.5
    if probs.get("under_25", 0) >= MIN_PROB_PICK:
        pr = probs["under_25"]
        candidates.append({
            "market": "Under 2.5",
            "prob": round(pr, 3),
            "fair": fair_odds(pr),
        })

    if probs.get("over_25", 0) >= MIN_PROB_PICK:
        pr = probs["over_25"]
        candidates.append({
            "market": "Over 2.5",
            "prob": round(pr, 3),
            "fair": fair_odds(pr),
        })

    # BTTS
    if probs.get("btts_no", 0) >= MIN_PROB_PICK:
        pr = probs["btts_no"]
        candidates.append({
            "market": "BTTS No",
            "prob": round(pr, 3),
            "fair": fair_odds(pr),
        })

    if probs.get("btts_yes", 0) >= MIN_PROB_PICK:
        pr = probs["btts_yes"]
        candidates.append({
            "market": "BTTS Yes",
            "prob": round(pr, 3),
            "fair": fair_odds(pr),
        })

    # ordenar por prob desc
    candidates.sort(key=lambda c: c["prob"], reverse=True)
    return candidates


def match_drivers(home, away, xg_home, xg_away, probs, strengths):
    """
    Exporta drivers numéricos + strengths para UI.
    """
    xg_total = xg_home + xg_away
    xg_edge = xg_away - xg_home  # positivo favorece al away
    imbalance = abs(xg_edge)

    hs = strengths.get(home, {})
    as_ = strengths.get(away, {})

    return {
        "xg_total": round(xg_total, 3),
        "xg_edge": round(xg_edge, 3),
        "imbalance": round(imbalance, 3),
        "home_strength": hs,
        "away_strength": as_,
        "signals": {
            "fav": "home" if probs["home"] > probs["away"] else "away",
            "fav_prob": round(max(probs["home"], probs["away"]), 3),
            "under25_prob": round(probs["under_25"], 3),
            "btts_no_prob": round(probs["btts_no"], 3),
        }
    }


def predict_upcoming(upcoming_matches, strengths, league_avg_home, league_avg_away):
    all_matches = []
    picks = []

    for m in upcoming_matches:
        home = m["homeTeam"]["name"]
        away = m["awayTeam"]["name"]

        lam_home, lam_away = expected_goals(home, away, strengths, league_avg_home, league_avg_away)

        p_home, p_draw, p_away = compute_1x2_probs(lam_home, lam_away)
        p_over, p_under = compute_over_under_25(lam_home, lam_away)
        p_btts_yes, p_btts_no = compute_btts(lam_home, lam_away)

        probs = {
            "home": round(p_home, 3),
            "draw": round(p_draw, 3),
            "away": round(p_away, 3),
            "over_25": round(p_over, 3),
            "under_25": round(p_under, 3),
            "btts_yes": round(p_btts_yes, 3),
            "btts_no": round(p_btts_no, 3),
        }

        candidates = build_candidates(probs)

        item = {
            "home": home,
            "away": away,
            "utcDate": m.get("utcDate"),

            # xG
            "xg_home": round(lam_home, 3),
            "xg_away": round(lam_away, 3),
            "xg_total": round(lam_home + lam_away, 3),

            # probabilities
            "probs": probs,

            # recommendations (premium)
            "candidates": candidates,

            # drivers + strengths (NUEVO)
            "drivers": match_drivers(home, away, lam_home, lam_away, probs, strengths),
        }

        all_matches.append(item)

        if candidates:
            picks.append(item)

    # ordenar por fecha
    def parse_date(x):
        try:
            return dt.datetime.fromisoformat(x["utcDate"].replace("Z", "+00:00"))
        except Exception:
            return dt.datetime(2100, 1, 1)

    all_matches.sort(key=parse_date)
    picks.sort(key=parse_date)

    return all_matches, picks


# =========================
# MAIN
# =========================
if __name__ == "__main__":
    matches = fetch_all_matches()
    finished, upcoming = split_matches(matches)

    league_avg_home, league_avg_away = compute_league_avgs(finished)

    strengths = compute_team_strengths(finished, league_avg_home, league_avg_away)

    all_matches, picks = predict_upcoming(upcoming, strengths, league_avg_home, league_avg_away)

    # Guardar
    with open("daily_matches.json", "w", encoding="utf-8") as f:
        json.dump(all_matches, f, ensure_ascii=False, indent=2)

    with open("daily_picks.json", "w", encoding="utf-8") as f:
        json.dump(picks, f, ensure_ascii=False, indent=2)

    print("\n=== DAILY MATCHES (ALL) ===\n")
    print(f"League avg home goals: {league_avg_home:.2f}")
    print(f"League avg away goals: {league_avg_away:.2f}")
    print(f"Finished matches used: {len(finished)}")
    print(f"Upcoming in next {NEXT_DAYS} days: {len(upcoming)}\n")

    print(f"Saved ALL matches to daily_matches.json ✅ ({len(all_matches)})")
    print(f"Saved PICKS to daily_picks.json ✅ ({len(picks)})")
