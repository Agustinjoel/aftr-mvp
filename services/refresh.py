"""
Pipeline de refresco único: obtener partidos → calcular picks (Poisson) → guardar en cache.
Punto de entrada para cron/run_daily.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any

from config.settings import settings
from core.evaluation import evaluate_market
from core.poisson import build_candidates, estimate_xg, match_probs, select_best_candidate
from data.cache import read_json, write_json
from data.providers.football_data import get_finished_matches, get_upcoming_matches

from core.model_b import estimate_xg_dynamic_split
from data.providers.team_form import get_team_recent_matches

logger = logging.getLogger(__name__)


# -------------------------
# Helpers generales
# -------------------------
def _parse_utcdate(m: dict) -> datetime:
    s = m.get("utcDate") or ""
    try:
        if isinstance(s, str) and s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        return datetime.fromisoformat(s)
    except Exception:
        return datetime.now(timezone.utc)


def _parse_utcdate_str(s: Any) -> datetime:
    """Parse utcDate robusto desde string ISO (soporta 'Z')."""
    try:
        if isinstance(s, str) and s:
            if s.endswith("Z"):
                return datetime.fromisoformat(s.replace("Z", "+00:00"))
            return datetime.fromisoformat(s)
    except Exception:
        pass
    return datetime.now(timezone.utc)


def _best_prob(p: dict) -> float:
    try:
        return float(p.get("best_prob") or 0.0)
    except Exception:
        return 0.0


def _normalize_match(m: dict) -> dict:
    out = dict(m)
    out.setdefault("home_crest", None)
    out.setdefault("away_crest", None)
    return out


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return float(default)
        return float(x)
    except Exception:
        return float(default)


def _read_json_list(filename: str) -> list[dict]:
    data = read_json(filename)
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]


# -------------------------
# Candidates: top2 + confidence
# -------------------------
def _top2_from_candidates(
    candidates: list[dict],
) -> tuple[str | None, float | None, str | None, float | None]:
    """
    Devuelve (best_market, best_prob, second_market, second_prob)
    basado en candidates ordenados por prob desc.
    """
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
    Score 1–10. Heurística simple:
    - best_prob alta => más confianza
    - edge (best-second) => más confianza
    - xG total muy bajo => más volatilidad (baja un poco)
    - modelo B => pequeño bonus
    """
    bp = float(best_prob or 0.0)
    sp = float(second_prob or 0.0)
    edge = max(0.0, bp - sp)

    score = 0.0
    score += bp * 10.0          # 0.80 -> +8
    score += edge * 20.0        # 0.05 -> +1

    if xg_total < 1.8:
        score -= 1.0
    elif xg_total > 3.2:
        score += 0.5

    if (model or "").strip().upper() == "B":
        score += 0.6

    score_i = int(round(score))
    if score_i < 1:
        score_i = 1
    if score_i > 10:
        score_i = 10
    return score_i


# -------------------------
# Picks builder (A + topN B)
# -------------------------
def _build_picks_from_matches(matches: list[dict]) -> list[dict]:
    """Genera picks híbrido: A para todos + B solo para Top N por prob."""
    if not matches:
        return []

    sorted_matches = sorted(matches, key=_parse_utcdate)

    topn_b = int(getattr(settings, "refresh_topn_model_b", 10) or 10)
    days_back = int(getattr(settings, "team_form_days_back", 30) or 30)
    limit = int(getattr(settings, "team_form_limit", 10) or 10)

    team_recent_cache: dict[int, list[dict]] = {}

    def _tm(team_id: int) -> list[dict]:
        if team_id not in team_recent_cache:
            team_recent_cache[team_id] = get_team_recent_matches(
                team_id, days_back=days_back, limit=limit
            )
        return team_recent_cache[team_id]

    picks: list[dict] = []

    # 1) Todos con modelo A
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
        except Exception:
            pass

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

        conf = _confidence_score(best_prob, second_prob, xg_total, "A")

        picks.append(
            {
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
                "model": "A",
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
            }
        )

    if topn_b <= 0:
        return picks

    # 2) Top N por best_prob para recalcular con B
    ranked = sorted(enumerate(picks), key=lambda t: _best_prob(t[1]), reverse=True)
    top_idxs = [idx for idx, _p in ranked[:topn_b]]

    # 3) Recalcular esos con modelo B
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


# -------------------------
# Finished => result
# -------------------------
def _build_finished_lookup_by_id(finished_matches: list[dict]) -> dict[int, tuple[int, int]]:
    """Mapa match_id -> (home_goals, away_goals)."""
    lookup: dict[int, tuple[int, int]] = {}
    for m in finished_matches:
        mid = m.get("match_id")
        hg = m.get("home_goals")
        ag = m.get("away_goals")
        if mid is None or hg is None or ag is None:
            continue
        try:
            lookup[int(mid)] = (int(hg), int(ag))
        except Exception:
            continue
    return lookup


def _apply_results_by_match_id(
    picks: list[dict], finished_by_id: dict[int, tuple[int, int]]
) -> list[dict]:
    """Actualiza result por match_id. Solo pisa si está PENDING/None."""
    for p in picks:
        res = (p.get("result") or "").strip().upper()
        if res not in ("", "PENDING", "NONE"):
            continue

        mid = p.get("match_id")
        if mid is None:
            continue

        try:
            mid_i = int(mid)
        except Exception:
            continue

        if mid_i not in finished_by_id:
            continue

        hg, ag = finished_by_id[mid_i]
        market = (p.get("best_market") or "").strip()
        result, _reason = evaluate_market(market, hg, ag)
        p["result"] = result

    return picks


# -------------------------
# Merge + History
# -------------------------
def _merge_by_match_id(existing: list[dict], new: list[dict]) -> list[dict]:
    """Merge por match_id (new pisa existing)."""
    by_id: dict[int, dict] = {}

    for p in existing or []:
        mid = p.get("match_id")
        if mid is None:
            continue
        try:
            by_id[int(mid)] = p
        except Exception:
            continue

    for p in new or []:
        mid = p.get("match_id")
        if mid is None:
            continue
        try:
            by_id[int(mid)] = p
        except Exception:
            continue

    return list(by_id.values())


def _save_history(league_code: str, picks: list[dict]) -> None:
    """Guarda historial eterno: nunca se pierde nada."""
    hist_file = f"picks_history_{league_code}.json"
    history = _read_json_list(hist_file)
    merged = _merge_by_match_id(history, picks)
    write_json(hist_file, merged)


def _window_daily(picks: list[dict], keep_days: int | None) -> list[dict]:
    """
    Mantiene daily liviano:
    - siempre incluye PENDING
    - incluye SETTLED solo dentro de keep_days hacia atrás
    Si keep_days es None o <=0 => no recorta.
    """
    try:
        kd = int(keep_days) if keep_days is not None else None
    except Exception:
        kd = None

    if not kd or kd <= 0:
        return picks

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=kd)

    out: list[dict] = []
    for p in picks:
        r = (p.get("result") or "").strip().upper()
        if r == "PENDING":
            out.append(p)
            continue
        dt = _parse_utcdate_str(p.get("utcDate"))
        if dt >= cutoff:
            out.append(p)
    return out


# -------------------------
# Public API
# -------------------------
def refresh_league(league_code: str) -> tuple[int, int]:
    if league_code not in settings.leagues:
        logger.warning("Liga desconocida: %s", league_code)
        return 0, 0

    # 1) Upcoming
    raw_upcoming = get_upcoming_matches(league_code)
    upcoming_matches = [_normalize_match(m) for m in (raw_upcoming or [])]
    upcoming_picks = _build_picks_from_matches(upcoming_matches)

    # 2) Existing cache
    existing_picks = _read_json_list(f"daily_picks_{league_code}.json")

    # 3) Finished + generar picks para finished (así aparecen aunque refresques tarde)
    finished_by_id: dict[int, tuple[int, int]] = {}
    finished_picks: list[dict] = []

    try:
        finished_matches = get_finished_matches(league_code, days_back=7)
        finished_by_id = _build_finished_lookup_by_id(finished_matches or [])

        finished_matches_norm = [_normalize_match(m) for m in (finished_matches or [])]
        finished_picks = _build_picks_from_matches(finished_matches_norm)

    except Exception as e:
        logger.warning("No pude traer FINISHED para %s (sigo sin evaluar): %s", league_code, e)

    # 4) Merge
    merged = _merge_by_match_id(existing_picks, upcoming_picks)
    merged = _merge_by_match_id(merged, finished_picks)

    # 5) Apply results
    picks_all = _apply_results_by_match_id(merged, finished_by_id)

    # 6) Save daily (ventana opcional)
    keep_days = getattr(settings, "daily_keep_days", None)  # si no existe => None
    picks_daily = _window_daily(picks_all, keep_days)

    write_json(f"daily_matches_{league_code}.json", upcoming_matches)
    write_json(f"daily_picks_{league_code}.json", picks_daily)

    # 7) Save history (eterno)
    _save_history(league_code, picks_all)

    settled = sum(1 for p in picks_daily if (p.get("result") or "").upper() in ("WIN", "LOSS", "PUSH"))
    pending = sum(1 for p in picks_daily if (p.get("result") or "").upper() == "PENDING")

    logger.info(
        "Liga %s: upcoming=%d | daily picks=%d (settled=%d pending=%d) | history updated",
        league_code,
        len(upcoming_matches),
        len(picks_daily),
        settled,
        pending,
    )
    return len(upcoming_matches), len(picks_daily)


def refresh_all() -> None:
    logger.info("Iniciando refresco para %d ligas", len(settings.league_codes()))
    for code in settings.league_codes():
        try:
            refresh_league(code)
        except Exception as e:
            logger.exception("Error refrescando liga %s: %s", code, e)
    logger.info("Refresco finalizado")