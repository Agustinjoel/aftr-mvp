"""
Modelo Poisson para probabilidades de partido (1X2, Over/Under, BTTS).
"""
from __future__ import annotations

import math
from typing import Any


def poisson_pmf(lmbda: float, k: int) -> float:
    """PMF(k; λ) = e^-λ * λ^k / k!"""
    return (math.exp(-lmbda) * (lmbda**k)) / math.factorial(k)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def match_probs(
    xg_home: float,
    xg_away: float,
    max_goals: int = 8,
) -> dict[str, float]:
    """
    Probabilidades por scorelines (hasta max_goals):
    - p_home_win, p_draw, p_away_win (1X2)
    - Derivados: 1X = home+draw, X2 = away+draw, 12 = home+away
    - Over/Under 2.5, Over 1.5, BTTS.
    """
    p_home_win = p_draw = p_away_win = 0.0
    p_under25 = p_over25 = p_over15 = 0.0
    p_btts_yes = p_btts_no = 0.0

    for h in range(max_goals + 1):
        ph = poisson_pmf(xg_home, h)
        for a in range(max_goals + 1):
            pa = poisson_pmf(xg_away, a)
            p = ph * pa

            if h > a:
                p_home_win += p
            elif h == a:
                p_draw += p
            else:
                p_away_win += p

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

    s = p_home_win + p_draw + p_away_win
    if s > 0:
        p_home_win /= s
        p_draw /= s
        p_away_win /= s
    so = p_under25 + p_over25
    if so > 0:
        p_under25 /= so
        p_over25 /= so
    sb = p_btts_yes + p_btts_no
    if sb > 0:
        p_btts_yes /= sb
        p_btts_no /= sb

    # Mercados derivados 1X, X2, 12
    p_1x = p_home_win + p_draw
    p_x2 = p_away_win + p_draw
    p_12 = p_home_win + p_away_win

    return {
        "home": round(p_home_win, 4),
        "draw": round(p_draw, 4),
        "away": round(p_away_win, 4),
        "1x": round(p_1x, 4),
        "x2": round(p_x2, 4),
        "12": round(p_12, 4),
        "under_25": round(p_under25, 4),
        "over_25": round(p_over25, 4),
        "over_15": round(p_over15, 4),
        "btts_yes": round(p_btts_yes, 4),
        "btts_no": round(p_btts_no, 4),
    }


def estimate_xg(
    match: dict[str, Any],
    default_home: float = 1.45,
    default_away: float = 1.15,
) -> tuple[float, float]:
    """
    xG esperados para el partido. Si el match trae xg_home/xg_away los usa; si no, defaults.
    """
    xg_home = default_home
    xg_away = default_away
    if "xg_home" in match and "xg_away" in match:
        try:
            xg_home = float(match.get("xg_home", default_home))
        except (TypeError, ValueError):
            pass
        try:
            xg_away = float(match.get("xg_away", default_away))
        except (TypeError, ValueError):
            pass
    return (
        _clamp(xg_home, 0.2, 4.0),
        _clamp(xg_away, 0.2, 4.0),
    )


# Mercados para candidatos: (nombre display, key en probs)
# 1X2, derivados 1X/X2/12, Over/Under, BTTS
CANDIDATE_MARKETS = [
    ("HOME WIN", "home"),
    ("DRAW", "draw"),
    ("AWAY WIN", "away"),
    ("1X", "1x"),
    ("X2", "x2"),
    ("12", "12"),
    ("Over 1.5", "over_15"),
    ("Over 2.5", "over_25"),
    ("Under 2.5", "under_25"),
    ("BTTS Yes", "btts_yes"),
    ("BTTS No", "btts_no"),
]

# Prioridad para elegir best_market cuando las probabilidades son similares (menor = preferido).
# DRAW solo se elige si es claramente mejor por probabilidad.
MARKET_PRIORITY: dict[str, int] = {
    "1X": 0,
    "X2": 0,
    "HOME WIN": 1,
    "AWAY WIN": 1,
    "12": 1,
    "OVER 2.5": 2,
    "UNDER 2.5": 2,
    "OVER 1.5": 2,
    "BTTS YES": 3,
    "BTTS NO": 3,
    "DRAW": 4,
}


def market_priority(market_name: str) -> int:
    """Prioridad del mercado (menor = más preferido). Desconocidos al final."""
    key = (market_name or "").strip().upper()
    return MARKET_PRIORITY.get(key, 5)


def select_best_candidate(
    candidates: list[dict[str, Any]],
    similar_threshold: float = 0.03,
    draw_edge_threshold: float = 0.04,
) -> dict[str, Any] | None:
    """
    Elige el mejor candidato para best_market.
    Cuando las probabilidades son similares (dentro de similar_threshold), prioriza:
    1X/X2 > HOME WIN/AWAY WIN > Over/Under 2.5 > BTTS > DRAW.
    DRAW solo se elige si su prob es al menos draw_edge_threshold mayor que el mejor no-DRAW.
    """
    if not candidates:
        return None
    max_prob = max(c.get("prob", 0) or 0 for c in candidates)
    non_draw = [c for c in candidates if market_priority(c.get("market") or "") != 4]
    best_draw = next((c for c in candidates if (c.get("market") or "").strip().upper() == "DRAW"), None)
    max_non_draw = max((c.get("prob", 0) or 0 for c in non_draw), default=0)
    draw_prob = (best_draw.get("prob", 0) or 0) if best_draw else 0

    if best_draw and draw_prob >= max_non_draw + draw_edge_threshold:
        return best_draw
    similar = [c for c in candidates if (c.get("prob") or 0) >= max_prob - similar_threshold]
    similar_sorted = sorted(
        similar,
        key=lambda c: (market_priority(c.get("market")), -(c.get("prob") or 0)),
    )
    return similar_sorted[0] if similar_sorted else candidates[0]


def build_candidates(
    probs: dict[str, float],
    min_prob: float = 0.50,
) -> list[dict[str, Any]]:
    """
    Candidatos con prob >= min_prob: { market, prob, fair }.
    fair = 1/prob si prob > 0. Ordenados por prob desc.
    """
    out = []
    for market_name, key in CANDIDATE_MARKETS:
        p = probs.get(key, 0.0)
        if p >= min_prob:
            cand: dict[str, Any] = {"market": market_name, "prob": round(float(p), 4)}
            if p > 0:
                cand["fair"] = round(1.0 / p, 2)
            out.append(cand)
    out.sort(key=lambda x: x["prob"], reverse=True)
    return out
