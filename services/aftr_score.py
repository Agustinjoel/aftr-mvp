"""
AFTR Score (0-100) for each pick.
Combines: model probability, value edge vs bookmaker odds, team form, xG difference.
"""
from __future__ import annotations


def clamp(value: float, low: float, high: float) -> float:
    """Clamp value to [low, high]."""
    return max(low, min(high, value))


def normalize_model_score(prob: float) -> float:
    """
    Model probability -> 0-100.
    prob in [0, 1]; 0.45 -> low, 0.55+ -> high. Linear: 0 -> 0, 1 -> 100.
    """
    p = clamp(prob, 0.0, 1.0)
    return round(p * 100.0, 2)


def normalize_value_score(prob: float, odds: float) -> float:
    """
    Value edge vs bookmaker -> 0-100.
    implied_prob = 1/odds, edge = prob - implied_prob.
    edge <= 0 => 0; edge >= 0.08 => 100; linear in between.
    Then if edge > 0: value_score *= 1.2, clamp to 0-100.
    """
    if odds is None or odds <= 0:
        return 0.0
    try:
        implied_prob = 1.0 / float(odds)
    except (TypeError, ValueError):
        return 0.0
    p = float(prob) if prob is not None else 0.0
    edge = p - implied_prob
    if edge <= 0:
        return 0.0
    if edge >= 0.08:
        raw = 100.0
    else:
        raw = (edge / 0.08) * 100.0
    if edge > 0:
        raw *= 1.2
    return round(clamp(raw, 0.0, 100.0), 2)


def normalize_form_score(form_diff: float, pick_side: str) -> float:
    """
    form_diff in [-2.0, 2.0] -> 0-100.
    pick_side: "home" | "away" | "draw" | "neutral".
    For "home": positive form_diff = good; for "away": negative form_diff = good.
    """
    fd = clamp(form_diff, -2.0, 2.0)
    if pick_side == "draw" or pick_side == "neutral":
        # Neutral: 0 diff -> 50
        return round(50.0 + fd * 25.0, 2)  # -2 -> 0, 0 -> 50, 2 -> 100
    if pick_side == "home":
        # -2 -> 0, 0 -> 50, 2 -> 100
        return round(50.0 + fd * 25.0, 2)
    if pick_side == "away":
        # For away we want negative form_diff (home worse) to be good -> invert
        return round(50.0 - fd * 25.0, 2)
    return round(50.0 + fd * 25.0, 2)


def normalize_xg_score(xg_diff: float, pick_side: str) -> float:
    """
    xg_diff in [-1.5, 1.5] -> 0-100.
    pick_side: "home" | "away" | "draw" | "neutral".
    For "home": positive xg_diff = good; for "away": negative xg_diff = good.
    """
    xd = clamp(xg_diff, -1.5, 1.5)
    if pick_side == "draw" or pick_side == "neutral":
        return round(50.0 + xd * (50.0 / 1.5), 2)  # -1.5 -> 0, 0 -> 50, 1.5 -> 100
    if pick_side == "home":
        # -1.5 -> 0, 0 -> 50, 1.5 -> 100
        return round(50.0 + xd * (50.0 / 1.5), 2)
    if pick_side == "away":
        return round(50.0 - xd * (50.0 / 1.5), 2)
    return round(50.0 + xd * (50.0 / 1.5), 2)


# Weights
WEIGHT_MODEL = 0.35
WEIGHT_VALUE = 0.35
WEIGHT_FORM = 0.15
WEIGHT_XG = 0.15

# Tier thresholds
TIER_ELITE = 85
TIER_STRONG = 70
TIER_RISKY = 55


def _tier_from_score(score: float) -> str:
    if score >= TIER_ELITE:
        return "elite"
    if score >= TIER_STRONG:
        return "strong"
    if score >= TIER_RISKY:
        return "risky"
    return "pass"


def compute_aftr_score(
    prob: float | None,
    odds: float | None,
    form_diff: float | None,
    xg_diff: float | None,
    pick_side: str,
) -> dict:
    """
    Compute all component scores and final AFTR score (0-100) and tier.
    Returns dict with: model_score, value_score, form_score, xg_score, aftr_score, tier, edge.
    """
    pick_side = (pick_side or "neutral").strip().lower() or "neutral"
    prob = float(prob) if prob is not None else 0.0
    odds = float(odds) if odds is not None else None

    # Value edge for output
    edge: float | None = None
    if prob is not None and odds is not None and odds > 0:
        implied = 1.0 / odds
        edge = round(prob - implied, 4)

    model_score = normalize_model_score(prob)
    value_score = normalize_value_score(prob, odds) if odds else 0.0
    form_diff_val = clamp(float(form_diff) if form_diff is not None else 0.0, -2.0, 2.0)
    xg_diff_val = clamp(float(xg_diff) if xg_diff is not None else 0.0, -1.5, 1.5)
    form_score = normalize_form_score(form_diff_val, pick_side)
    xg_score = normalize_xg_score(xg_diff_val, pick_side)

    aftr_score = round(
        WEIGHT_MODEL * model_score
        + WEIGHT_VALUE * value_score
        + WEIGHT_FORM * form_score
        + WEIGHT_XG * xg_score,
        2,
    )
    aftr_score = clamp(aftr_score, 0.0, 100.0)

    # B) Penalize no value
    if edge is not None and edge <= 0:
        aftr_score = round(aftr_score * 0.7, 2)
        aftr_score = clamp(aftr_score, 0.0, 100.0)

    # C) Penalize low-value favorites
    if prob > 0.65 and (edge is None or edge < 0.02):
        aftr_score = round(aftr_score - 10.0, 2)
        aftr_score = clamp(aftr_score, 0.0, 100.0)

    tier = _tier_from_score(aftr_score)

    # Guardrails: if edge <= 0, tier cannot exceed risky
    if edge is not None and edge <= 0:
        if tier in ("elite", "strong"):
            tier = "risky"
    # if prob < 0.45, tier cannot be elite
    if prob < 0.45 and tier == "elite":
        tier = "strong" if aftr_score >= TIER_STRONG else "risky"

    # D) Confidence metric
    confidence = round((model_score + form_score + xg_score) / 3.0, 2)
    if confidence >= 75:
        confidence_level = "high"
    elif confidence >= 60:
        confidence_level = "medium"
    else:
        confidence_level = "low"

    return {
        "model_score": round(model_score, 2),
        "value_score": round(value_score, 2),
        "form_score": round(form_score, 2),
        "xg_score": round(xg_score, 2),
        "aftr_score": round(aftr_score, 2),
        "tier": tier,
        "edge": edge,
        "confidence": round(confidence, 2),
        "confidence_level": confidence_level,
    }


def pick_side_from_market(best_market: str) -> str:
    """Map best_market string to pick_side: home | away | draw | neutral."""
    m = (best_market or "").strip().lower()
    if m in ("home", "home win", "1"):
        return "home"
    if m in ("away", "away win", "2"):
        return "away"
    if m in ("draw", "x"):
        return "draw"
    return "neutral"


def form_diff_from_stats(stats_home: dict, stats_away: dict) -> float:
    """
    Derive form_diff in [-2, 2] from stats_home/stats_away (gf, ga, n).
    Per-game goal diff difference, clamped.
    """
    def gd(s: dict) -> float:
        if not s or not isinstance(s, dict):
            return 0.0
        try:
            gf = float(s.get("gf")) if s.get("gf") != "—" else 0.0
        except (TypeError, ValueError):
            gf = 0.0
        try:
            ga = float(s.get("ga")) if s.get("ga") != "—" else 0.0
        except (TypeError, ValueError):
            ga = 0.0
        return gf - ga

    # gf/ga in stats are already per-game averages — diff is direct, no extra division needed
    home_gd = gd(stats_home or {})
    away_gd = gd(stats_away or {})
    diff = home_gd - away_gd
    return clamp(diff, -2.0, 2.0)


def xg_diff_for_pick(xg_home: float, xg_away: float, pick_side: str) -> float:
    """xg_diff in [-1.5, 1.5] for the picked side."""
    xh = float(xg_home) if xg_home is not None else 0.0
    xa = float(xg_away) if xg_away is not None else 0.0
    raw = xh - xa
    if pick_side == "away":
        raw = xa - xh
    elif pick_side in ("draw", "neutral"):
        raw = 0.0
    return clamp(raw, -1.5, 1.5)


def enrich_pick_with_aftr_score(p: dict) -> dict:
    """
    Add model_score, value_score, form_score, xg_score, aftr_score, tier, edge to a pick dict.
    Uses best_prob, best_fair/odds_decimal, stats_home/stats_away, xg_home/xg_away, best_market.
    """
    if not p or not isinstance(p, dict):
        return p
    prob = p.get("best_prob")
    odds = p.get("odds_decimal") or p.get("best_fair")
    if odds is not None:
        try:
            odds = float(odds)
        except (TypeError, ValueError):
            odds = None
    pick_side = pick_side_from_market(p.get("best_market") or "")
    form_diff = form_diff_from_stats(p.get("stats_home") or {}, p.get("stats_away") or {})
    xg_diff = xg_diff_for_pick(
        p.get("xg_home"), p.get("xg_away"), pick_side
    )
    result = compute_aftr_score(prob, odds, form_diff, xg_diff, pick_side)
    # Use value edge from result; if pick already had edge (from odds enrichment), keep for display
    edge = p.get("edge")
    if result.get("edge") is not None:
        edge = result["edge"]
    p["model_score"] = result["model_score"]
    p["value_score"] = result["value_score"]
    p["form_score"] = result["form_score"]
    p["xg_score"] = result["xg_score"]
    p["aftr_score"] = result["aftr_score"]
    p["tier"] = result["tier"]
    p["edge"] = edge
    p["confidence"] = result.get("confidence")
    p["confidence_level"] = result.get("confidence_level")
    return p


def filter_premium_picks(picks: list[dict]) -> list[dict]:
    """Return picks that meet premium bar: aftr_score >= 70 and edge > 0."""
    return [
        p for p in (picks or [])
        if isinstance(p, dict)
        and p.get("aftr_score", 0) >= 70
        and (p.get("edge") or 0) > 0
    ]
