"""
Construcción de picks a partir de matches.
Aplica modelo A (Poisson + xG estático) y modelo B (xG dinámico por forma)
a los top-N picks, generando candidatos, confianza y métricas.
"""
from __future__ import annotations

import logging

from config.settings import settings
from core.poisson import build_candidates, estimate_xg, match_probs, select_best_candidate
from core.model_b import estimate_xg_dynamic_split
from data.providers.team_form import get_team_recent_matches
from services.refresh_utils import _safe_float, _best_prob, _parse_utcdate
from services.refresh_teams import (
    _calc_team_stats_from_recent,
    _build_recent_compact,
)

logger = logging.getLogger(__name__)


# -------------------------
# Candidatos + confidence
# -------------------------

def _top2_from_candidates(
    candidates: list[dict],
) -> tuple[str | None, float | None, str | None, float | None]:
    """Devuelve (best_market, best_prob, second_market, second_prob) del ranking de candidatos."""
    if not candidates:
        return None, None, None, None

    ordered = sorted(
        [c for c in candidates if isinstance(c, dict)],
        key=lambda c: _safe_float(c.get("prob")),
        reverse=True,
    )

    best = ordered[0] if len(ordered) >= 1 else None
    second = ordered[1] if len(ordered) >= 2 else None

    best_m = best.get("market") if best else None
    best_p = _safe_float(best.get("prob")) if best and best.get("prob") is not None else None

    sec_m = second.get("market") if second else None
    sec_p = _safe_float(second.get("prob")) if second and second.get("prob") is not None else None

    return best_m, best_p, sec_m, sec_p


def _confidence_score(
    best_prob: float | None,
    second_prob: float | None,
    xg_total: float,
    model: str,
) -> int:
    """
    Score de confianza 1-10 basado en probabilidad del mejor mercado,
    separación respecto al segundo, xG total y modelo usado.
    """
    bp = float(best_prob or 0.0)
    sp = float(second_prob or 0.0)
    edge = max(0.0, bp - sp)

    score = 0.0
    score += bp * 7.0
    score += edge * 12.0

    if xg_total < 1.8:
        score -= 1.0
    elif xg_total > 3.2:
        score += 0.5

    if (model or "").strip().upper() == "B":
        score += 0.6

    score_i = int(round(score))
    return max(1, min(10, score_i))


def _build_pick_entry(m: dict, xg_h: float, xg_a: float, model: str, stats_home: dict, stats_away: dict) -> dict:
    """Construye el dict de pick a partir del match y los xG calculados."""
    probs = match_probs(xg_h, xg_a, max_goals=settings.max_goals_poisson)
    candidates = build_candidates(probs, min_prob=settings.min_prob_for_candidate)
    best = select_best_candidate(candidates)

    best_market, best_prob, second_market, second_prob = _top2_from_candidates(candidates)
    if best_market is None and best:
        best_market = best.get("market")
    if best_prob is None and best and best.get("prob") is not None:
        best_prob = _safe_float(best.get("prob"))

    best_fair = best.get("fair") if best and best.get("prob") else None
    if best_fair is None and best_prob and best_prob > 0:
        best_fair = round(1.0 / float(best_prob), 2)

    xg_total = float(xg_h + xg_a)

    edge_val: float | None = None
    if best_prob is not None and second_prob is not None:
        edge_val = float(best_prob) - float(second_prob)

    conf = _confidence_score(best_prob, second_prob, xg_total, model)

    return {
        "match_id": m.get("match_id"),
        "utcDate": m.get("utcDate", ""),
        "home": m.get("home", ""),
        "away": m.get("away", ""),
        "home_crest": m.get("home_crest"),
        "away_crest": m.get("away_crest"),
        "home_team_id": m.get("home_team_id"),
        "away_team_id": m.get("away_team_id"),
        "xg_home": round(float(xg_h), 2),
        "xg_away": round(float(xg_a), 2),
        "xg_total": round(float(xg_total), 2),
        "model": model,
        "probs": probs,
        "candidates": candidates,
        "best_market": best_market,
        "best_prob": best_prob,
        "best_fair": best_fair,
        "second_market": second_market,
        "second_prob": second_prob,
        "edge": round(float(edge_val), 4) if edge_val is not None else None,
        "confidence": conf,
        "result": "PENDING",
        "score_home": None,
        "score_away": None,
        "stats_home": stats_home,
        "stats_away": stats_away,
    }


# -------------------------
# Builder principal
# -------------------------

def _build_picks_from_matches(matches: list[dict], team_names: dict[int, str]) -> list[dict]:
    """
    Genera picks para todos los matches:
    1) Todos con modelo A (Poisson + xG estático)
    2) Top-N por best_prob se recalculan con modelo B (xG dinámico)
    """
    if not matches:
        return []

    sorted_matches = sorted(matches, key=_parse_utcdate)

    topn_b = int(getattr(settings, "refresh_topn_model_b", 10) or 10)
    days_back = int(getattr(settings, "team_form_days_back", 30) or 30)
    limit = int(getattr(settings, "team_form_limit", 10) or 10)

    # Cache de partidos recientes por equipo (evita llamadas repetidas)
    team_recent_cache: dict[int, list[dict]] = {}

    def _tm(team_id: int) -> list[dict]:
        if team_id not in team_recent_cache:
            team_recent_cache[team_id] = get_team_recent_matches(
                team_id, days_back=days_back, limit=limit
            )
        return team_recent_cache[team_id]

    picks: list[dict] = []

    # 1) Modelo A para todos
    for m in sorted_matches:
        xg_h = float(settings.default_xg_home)
        xg_a = float(settings.default_xg_away)

        try:
            axg_h, axg_a = estimate_xg(
                m,
                default_home=settings.default_xg_home,
                default_away=settings.default_xg_away,
            )
            xg_h, xg_a = float(axg_h), float(axg_a)
        except Exception as _err:
            logger.warning("unexpected exception (non-fatal): %s", _err)

        stats_home: dict = {}
        stats_away: dict = {}
        hid = m.get("home_team_id")
        aid = m.get("away_team_id")

        try:
            if hid:
                hm = _tm(int(hid))
                base = _calc_team_stats_from_recent(int(hid), hm)
                base["recent"] = _build_recent_compact(int(hid), hm, team_names, n=3)
                stats_home = base
            if aid:
                am = _tm(int(aid))
                base = _calc_team_stats_from_recent(int(aid), am)
                base["recent"] = _build_recent_compact(int(aid), am, team_names, n=3)
                stats_away = base
        except Exception as e:
            logger.warning("Team stats error (%s vs %s): %s", hid, aid, e)

        picks.append(_build_pick_entry(m, xg_h, xg_a, "A", stats_home, stats_away))

    if topn_b <= 0:
        return picks

    # 2) Top-N recalculados con modelo B
    ranked = sorted(enumerate(picks), key=lambda t: _best_prob(t[1]), reverse=True)
    top_idxs = [idx for idx, _p in ranked[:topn_b]]

    for idx in top_idxs:
        p = picks[idx]
        hid = p.get("home_team_id")
        aid = p.get("away_team_id")
        if not hid or not aid:
            continue

        try:
            hm = _tm(int(hid))
            am = _tm(int(aid))
            xg_h, xg_a = estimate_xg_dynamic_split(int(hid), int(aid), hm, am)

            probs = match_probs(xg_h, xg_a, max_goals=settings.max_goals_poisson)
            candidates = build_candidates(probs, min_prob=settings.min_prob_for_candidate)
            best = select_best_candidate(candidates)

            best_market, best_prob, second_market, second_prob = _top2_from_candidates(candidates)
            if best_market is None and best:
                best_market = best.get("market")
            if best_prob is None and best and best.get("prob") is not None:
                best_prob = _safe_float(best.get("prob"))

            best_fair = best.get("fair") if best and best.get("prob") else None
            if best_fair is None and best_prob and best_prob > 0:
                best_fair = round(1.0 / float(best_prob), 2)

            xg_total = float(xg_h + xg_a)
            edge_val: float | None = None
            if best_prob is not None and second_prob is not None:
                edge_val = float(best_prob) - float(second_prob)

            conf = _confidence_score(best_prob, second_prob, xg_total, "B")

            p.update(
                {
                    "xg_home": round(float(xg_h), 2),
                    "xg_away": round(float(xg_a), 2),
                    "xg_total": round(float(xg_total), 2),
                    "model": "B",
                    "probs": probs,
                    "candidates": candidates,
                    "best_market": best_market,
                    "best_prob": best_prob,
                    "best_fair": best_fair,
                    "second_market": second_market,
                    "second_prob": second_prob,
                    "edge": round(float(edge_val), 4) if edge_val is not None else None,
                    "confidence": conf,
                }
            )

        except Exception as e:
            logger.warning("Modelo B fallback a A (%s vs %s): %s", p.get("home"), p.get("away"), e)

    return picks
