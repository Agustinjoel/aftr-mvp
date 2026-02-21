"""
Pipeline de refresco único: obtener partidos → calcular picks (Poisson) → guardar en cache.
Punto de entrada para cron/run_daily.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from config.settings import settings
from core.evaluation import evaluate_market
from core.poisson import build_candidates, estimate_xg, match_probs, select_best_candidate
from data.cache import read_json, write_json
from data.providers.football_data import get_finished_matches, get_upcoming_matches

logger = logging.getLogger(__name__)


def _match_key(home: str, away: str, utc_date: str) -> tuple[str, str, str]:
    """Clave para identificar partido: (home_norm, away_norm, YYYY-MM-DD)."""
    date_part = (utc_date or "")[:10] if isinstance(utc_date, str) else ""
    return ((home or "").strip().lower(), (away or "").strip().lower(), date_part)


def _parse_utcdate(m: dict) -> datetime:
    s = m.get("utcDate") or ""
    try:
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        return datetime.fromisoformat(s)
    except Exception:
        return datetime.now(timezone.utc)


def _build_picks_from_matches(matches: list[dict]) -> list[dict]:
    """Genera picks con Poisson a partir de la lista de partidos."""
    if not matches:
        return []
    sorted_matches = sorted(matches, key=_parse_utcdate)
    picks = []
    for m in sorted_matches:
        xg_h, xg_a = estimate_xg(
            m,
            default_home=settings.default_xg_home,
            default_away=settings.default_xg_away,
        )
        probs = match_probs(xg_h, xg_a, max_goals=settings.max_goals_poisson)
        candidates = build_candidates(probs, min_prob=settings.min_prob_for_candidate)
        best = select_best_candidate(candidates)
        best_market = best.get("market") if best else None
        best_prob = best.get("prob") if best else None
        best_fair = best.get("fair") if best and best.get("prob") else None
        if best_fair is None and best_prob and best_prob > 0:
            best_fair = round(1.0 / best_prob, 2)

        picks.append({
            "utcDate": m.get("utcDate", ""),
            "home": m.get("home", ""),
            "away": m.get("away", ""),
            "home_crest": m.get("home_crest"),
            "away_crest": m.get("away_crest"),
            "xg_home": round(xg_h, 2),
            "xg_away": round(xg_a, 2),
            "xg_total": round(xg_h + xg_a, 2),
            "probs": probs,
            "candidates": candidates,
            "best_market": best_market,
            "best_prob": best_prob,
            "best_fair": best_fair,
            "result": "PENDING",
        })
    return picks


def _normalize_match(m: dict) -> dict:
    """Asegura que cada partido tenga home_crest y away_crest (None si faltan)."""
    out = dict(m)
    if "home_crest" not in out:
        out["home_crest"] = None
    if "away_crest" not in out:
        out["away_crest"] = None
    return out


def _build_finished_lookup(finished_matches: list[dict]) -> dict[tuple[str, str, str], tuple[int, int]]:
    """Mapa (home_norm, away_norm, date) -> (home_goals, away_goals)."""
    lookup: dict[tuple[str, str, str], tuple[int, int]] = {}
    for m in finished_matches:
        key = _match_key(m.get("home", ""), m.get("away", ""), m.get("utcDate", ""))
        hg = m.get("home_goals", 0)
        ag = m.get("away_goals", 0)
        lookup[key] = (hg, ag)
    return lookup


def _apply_finished_results(
    new_picks: list[dict],
    existing_picks: list[dict],
    finished_lookup: dict[tuple[str, str, str], tuple[int, int]],
) -> list[dict]:
    """
    new_picks: picks de partidos programados (todos result PENDING).
    existing_picks: picks previos del cache.
    finished_lookup: partidos finalizados con resultado.
    Devuelve new_picks + picks históricos de partidos ya finalizados con result WIN/LOSS/PUSH.
    """
    new_keys = {_match_key(p.get("home", ""), p.get("away", ""), p.get("utcDate", "")) for p in new_picks}
    finished_picks: list[dict] = []
    for op in existing_picks:
        if not isinstance(op, dict):
            continue
        key = _match_key(op.get("home", ""), op.get("away", ""), op.get("utcDate", ""))
        if key not in finished_lookup or key in new_keys:
            continue
        hg, ag = finished_lookup[key]
        market = op.get("best_market") or ""
        result, _ = evaluate_market(market, hg, ag)
        pick_with_result = dict(op)
        pick_with_result["result"] = result
        finished_picks.append(pick_with_result)
    return new_picks + finished_picks


def refresh_league(league_code: str) -> tuple[int, int]:
    """
    Refresca una liga: fetch matches → build picks → evaluar partidos finalizados → write cache.
    Los picks incluyen siempre "result": "WIN" | "LOSS" | "PUSH" | "PENDING".
    Returns (num_matches, num_picks).
    """
    if league_code not in settings.leagues:
        logger.warning("Liga desconocida: %s", league_code)
        return 0, 0
    raw_matches = get_upcoming_matches(league_code)
    matches = [_normalize_match(m) for m in raw_matches]
    new_picks = _build_picks_from_matches(matches)

    existing_picks = read_json(f"daily_picks_{league_code}.json")
    if not isinstance(existing_picks, list):
        existing_picks = []

    finished_matches = get_finished_matches(league_code, days_back=5)
    finished_lookup = _build_finished_lookup(finished_matches)
    picks = _apply_finished_results(new_picks, existing_picks, finished_lookup)

    write_json(f"daily_matches_{league_code}.json", matches)
    write_json(f"daily_picks_{league_code}.json", picks)
    logger.info("Liga %s: %d partidos, %d picks", league_code, len(matches), len(picks))
    return len(matches), len(picks)


def refresh_all() -> None:
    """Refresca todas las ligas configuradas y escribe en data/cache."""
    logger.info("Iniciando refresco para %d ligas", len(settings.league_codes()))
    for code in settings.league_codes():
        try:
            refresh_league(code)
        except Exception as e:
            logger.exception("Error refrescando liga %s: %s", code, e)
    logger.info("Refresco finalizado")
