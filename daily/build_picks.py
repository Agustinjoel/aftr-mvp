import json
import math
import os
from datetime import datetime, timezone

# Ligas top (coinciden con tus archivos)
LEAGUES = ["PL", "PD", "SA", "BL1", "FL1", "CL"]

BASE_DIR = os.path.dirname(os.path.dirname(__file__))  # engine/
DAILY_DIR = os.path.join(BASE_DIR, "daily")


# -----------------------------
# Utils
# -----------------------------
def _safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


def read_json(path: str):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: str, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def poisson_pmf(lmbda: float, k: int) -> float:
    # PMF(k; λ) = e^-λ * λ^k / k!
    return (math.exp(-lmbda) * (lmbda ** k)) / math.factorial(k)


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


# -----------------------------
# Probabilidades por Poisson
# -----------------------------
def match_probs(xg_home: float, xg_away: float, max_goals: int = 8):
    p_home = 0.0
    p_draw = 0.0
    p_away = 0.0
    p_under25 = 0.0
    p_over25 = 0.0
    p_over15 = 0.0
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

            if total >= 2:
                p_over15 += p

            if h >= 1 and a >= 1:
                p_btts_yes += p
            else:
                p_btts_no += p

    # normalizar (por si falta masa por truncamiento)
    s = p_home + p_draw + p_away
    if s > 0:
        p_home /= s
        p_draw /= s
        p_away /= s

        # estos ya están en el mismo espacio, pero los re-normalizamos suave
        # (no es obligatorio, pero queda prolijo)
        # under/over:
        so = p_under25 + p_over25
        if so > 0:
            p_under25 /= so
            p_over25 /= so

        sb = p_btts_yes + p_btts_no
        if sb > 0:
            p_btts_yes /= sb
            p_btts_no /= sb

    return {
        "home": round(p_home, 4),
        "draw": round(p_draw, 4),
        "away": round(p_away, 4),
        "under_25": round(p_under25, 4),
        "over_25": round(p_over25, 4),
        "over_15": round(p_over15, 4),
        "btts_yes": round(p_btts_yes, 4),
        "btts_no": round(p_btts_no, 4),
    }


# -----------------------------
# Heurística simple para xG
# (sin históricos aún)
# -----------------------------
def estimate_xg(match: dict):
    """
    Como todavía no metimos strength por equipo:
    - default home 1.45
    - default away 1.15
    - si querés, podés tunear por liga más adelante
    """
    xg_home = 1.45
    xg_away = 1.15

    # si en tu JSON ya vienen xg_home/xg_away, los usa
    if "xg_home" in match and "xg_away" in match:
        xg_home = _safe_float(match.get("xg_home"), xg_home)
        xg_away = _safe_float(match.get("xg_away"), xg_away)

    # clamp por seguridad
    xg_home = clamp(xg_home, 0.2, 4.0)
    xg_away = clamp(xg_away, 0.2, 4.0)
    return xg_home, xg_away


def build_candidates(probs: dict, min_prob: float = 0.50):
    cand = [
        ("Home Win", probs["home"]),
        ("Draw", probs["draw"]),
        ("Away Win", probs["away"]),
        ("Over 1.5", probs["over_15"]),
        ("Over 2.5", probs["over_25"]),
        ("Under 2.5", probs["under_25"]),
        ("BTTS Yes", probs["btts_yes"]),
        ("BTTS No", probs["btts_no"]),
    ]

    out = []
    for market, p in cand:
        if p >= min_prob:
            out.append({"market": market, "prob": round(float(p), 4)})

    out.sort(key=lambda x: x["prob"], reverse=True)
    return out


def parse_utcdate(m: dict):
    s = m.get("utcDate") or ""
    try:
        # ejemplo: 2026-02-18T19:00:00Z
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        return datetime.fromisoformat(s)
    except Exception:
        return datetime.now(timezone.utc)


def build_for_league(code: str):
    matches_path = os.path.join(DAILY_DIR, f"daily_matches_{code}.json")
    picks_path = os.path.join(DAILY_DIR, f"daily_picks_{code}.json")

    matches = read_json(matches_path)
    if not matches:
        write_json(picks_path, [])
        print(f"SKIP {code}: no matches")
        return

    # orden por fecha
    matches.sort(key=parse_utcdate)

    picks = []
    for m in matches:
        home = m.get("home") or ""
        away = m.get("away") or ""
        utcDate = m.get("utcDate") or ""

        xg_h, xg_a = estimate_xg(m)
        probs = match_probs(xg_h, xg_a, max_goals=8)

        candidates = build_candidates(probs, min_prob=0.50)

        picks.append({
            "utcDate": utcDate,
            "home": home,
            "away": away,
            "xg_home": round(xg_h, 2),
            "xg_away": round(xg_a, 2),
            "xg_total": round(xg_h + xg_a, 2),
            "probs": probs,
            "candidates": candidates,
        })

    write_json(picks_path, picks)
    print(f"OK {code}: {len(picks)} picks")


def main():
    for code in LEAGUES:
        build_for_league(code)


if __name__ == "__main__":
    main()