"""
Basketball/NBA picks builder only. Markets: Moneyline, Total Points, Spread.
Produces pick dicts compatible with the shared dashboard (same shape as football picks).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


NBA_DEFAULT_TOTAL_LINE = 220.5


def _parse_utcdate(m: dict) -> datetime:
    s = (m or {}).get("utcDate") or ""
    try:
        if isinstance(s, str) and s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        return datetime.fromisoformat(s)
    except Exception:
        return datetime.now(timezone.utc)


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return float(default)
        return float(x)
    except Exception:
        return float(default)


def build_basketball_picks(matches: list[dict]) -> list[dict]:
    """
    Build picks for basketball matches. Markets: Moneyline Home/Away, Over/Under total, Spread (placeholder).
    Same overall pick shape as football so dashboard can render. No football assumptions.
    """
    if not matches:
        return []
    sorted_matches = sorted(matches, key=_parse_utcdate)
    picks: list[dict] = []
    for m in sorted_matches:
        ml_home_prob = 0.52
        ml_away_prob = 0.48
        total_line = NBA_DEFAULT_TOTAL_LINE
        over_prob = 0.50
        under_prob = 0.50
        candidates = [
            {"market": "Moneyline Home", "prob": round(ml_home_prob, 4), "fair": round(1.0 / ml_home_prob, 2)},
            {"market": "Moneyline Away", "prob": round(ml_away_prob, 4), "fair": round(1.0 / ml_away_prob, 2)},
            {"market": f"Over {total_line}", "prob": round(over_prob, 4), "fair": 2.0},
            {"market": f"Under {total_line}", "prob": round(under_prob, 4), "fair": 2.0},
            {"market": "Spread Home", "prob": 0.50, "fair": 2.0},
        ]
        ordered = sorted(candidates, key=lambda c: _safe_float(c.get("prob")), reverse=True)
        best = ordered[0] if ordered else None
        second = ordered[1] if len(ordered) > 1 else None
        best_market = best.get("market") if best else None
        best_prob = _safe_float(best.get("prob")) if best else None
        best_fair = best.get("fair") if best else None
        second_market = second.get("market") if second else None
        second_prob = _safe_float(second.get("prob")) if second else None
        edge_val = (best_prob or 0) - (second_prob or 0)
        conf = max(1, min(10, int(round((best_prob or 0) * 10))))
        picks.append({
            "match_id": m.get("match_id"),
            "utcDate": m.get("utcDate", ""),
            "home": m.get("home", ""),
            "away": m.get("away", ""),
            "home_crest": m.get("home_crest"),
            "away_crest": m.get("away_crest"),
            "home_team_id": m.get("home_team_id"),
            "away_team_id": m.get("away_team_id"),
            "xg_home": 0,
            "xg_away": 0,
            "xg_total": 0,
            "model": "Basketball",
            "probs": {},
            "candidates": candidates,
            "best_market": best_market,
            "best_prob": best_prob,
            "best_fair": best_fair,
            "second_market": second_market,
            "second_prob": second_prob,
            "edge": round(edge_val, 4),
            "confidence": conf,
            "result": "PENDING",
            "score_home": None,
            "score_away": None,
            "stats_home": {},
            "stats_away": {},
        })
    return picks
