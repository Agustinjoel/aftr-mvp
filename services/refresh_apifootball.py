"""
AFTR API-Football Refresh — refresco de ligas usando API-Football v3 (RapidAPI).

Produce el formato de caché estándar (daily_matches_*.json, daily_picks_*.json)
para todas las ligas configuradas en APIF_LEAGUE_MAP.

Uso interno (llamado desde services.tiered_refresh):
    from services.refresh_apifootball import apif_refresh_league
    matches_count, picks_count = apif_refresh_league("PL")
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("aftr.refresh_apifootball")


def _build_finished_lookup(finished: list[dict]) -> dict[int, tuple[int, int]]:
    """Construye lookup {match_id: (home_goals, away_goals)} para resultado definitivo."""
    lu: dict[int, tuple[int, int]] = {}
    for m in finished or []:
        if not isinstance(m, dict):
            continue
        mid = m.get("match_id")
        hg  = m.get("home_goals")
        ag  = m.get("away_goals")
        if mid is None or hg is None or ag is None:
            continue
        try:
            lu[int(mid)] = (int(hg), int(ag))
        except (TypeError, ValueError):
            continue
    return lu


def apif_refresh_league(
    league_code: str,
    *,
    days_upcoming: int = 7,
    days_finished: int = 7,
    fetch_odds: bool = False,
    metrics: object | None = None,
) -> tuple[int, int]:
    """
    Refresco completo de una liga usando API-Football v3.

    1. Fetch próximos (next days_upcoming días) y finalizados (last days_finished días)
    2. Normaliza a formato AFTR
    3. Merge con caché existente
    4. Construye picks (Poisson model)
    5. Aplica resultados a picks finalizados
    6. Escribe caché (daily_matches_*, daily_picks_*)
    7. Actualiza historial y team names

    Devuelve (n_upcoming, n_picks).
    """
    from config.settings import settings
    from data.providers.api_football import fetch_fixtures_by_league, _api_key
    from services.refresh_teams import (
        _load_team_names_cache,
        _save_team_names_cache,
        _update_team_names_from_matches,
    )
    from services.refresh_utils import _read_json_list, _normalize_match
    from services.refresh_picks import _build_picks_from_matches
    from services.refresh_results import (
        _build_finished_lookup_by_id,
        _build_finished_lookup_from_cache,
        _apply_results_by_match_id,
        _merge_by_match_id,
        _restore_settled_picks,
        _save_history,
        _window_daily,
        _write_league_cache,
    )
    from services.aftr_score import enrich_pick_with_aftr_score, filter_premium_picks

    # ── 0. Verificar key y league ID ────────────────────────────────────────
    if not _api_key():
        logger.debug("apif_refresh_league %s: no API key, skip", league_code)
        return 0, 0

    league_id = settings.get_apif_league_id(league_code)
    if not league_id:
        logger.debug("apif_refresh_league %s: no APIF ID mapped, skip", league_code)
        return 0, 0

    season = settings.get_apif_season(league_code)

    # ── 1. Fetch próximos + finalizados ─────────────────────────────────────
    try:
        upcoming_raw, finished_raw = fetch_fixtures_by_league(
            league_id,
            season,
            league_code=league_code,
            days_upcoming=days_upcoming,
            days_finished=days_finished,
        )
    except Exception as e:
        logger.warning("apif_refresh_league %s: fetch error: %s", league_code, e)
        return 0, 0

    if not upcoming_raw and not finished_raw:
        logger.info("apif_refresh_league %s: no fixtures returned (season=%s)", league_code, season)
        # Intentar temporada anterior si no hay datos
        prev_season = season - 1
        try:
            upcoming_raw, finished_raw = fetch_fixtures_by_league(
                league_id,
                prev_season,
                league_code=league_code,
                days_upcoming=days_upcoming,
                days_finished=days_finished,
            )
            if upcoming_raw or finished_raw:
                season = prev_season
                logger.info(
                    "apif_refresh_league %s: found data in season %s", league_code, season
                )
        except Exception as e2:
            logger.debug("apif_refresh_league %s: prev season fetch failed: %s", league_code, e2)

    # ── 2. Normalizar a formato AFTR ─────────────────────────────────────────
    # _normalize_match asegura que score/status estén en el formato canónico AFTR
    upcoming_matches  = [_normalize_match(m) for m in upcoming_raw  if isinstance(m, dict)]
    finished_matches  = [_normalize_match(m) for m in finished_raw  if isinstance(m, dict)]

    # ── 3. Team names cache ──────────────────────────────────────────────────
    team_names = _load_team_names_cache()
    _update_team_names_from_matches(team_names, upcoming_matches)
    _update_team_names_from_matches(team_names, finished_matches)

    # ── 4. Construir picks ────────────────────────────────────────────────────
    upcoming_picks = _build_picks_from_matches(upcoming_matches, team_names)
    finished_picks = _build_picks_from_matches(finished_matches, team_names)

    # ── 5. Merge con existentes ───────────────────────────────────────────────
    existing_picks = _read_json_list(f"daily_picks_{league_code}.json")
    merged = _merge_by_match_id(existing_picks, upcoming_picks)
    merged = _merge_by_match_id(finished_picks, merged)

    # ── 6. Aplicar resultados ─────────────────────────────────────────────────
    finished_by_id = _build_finished_lookup_by_id(finished_raw)
    picks_all      = _apply_results_by_match_id(merged, finished_by_id)

    # ── 6b. Backfill: resolver picks PENDING contra caché local ───────────────
    # Partidos más viejos que days_finished no aparecen en la respuesta de la API,
    # pero daily_matches ya tiene su score final. Los resolvemos sin llamadas extra.
    # Los dicts son mutables — los cambios en pending_picks reflejan en picks_all.
    all_matches = _merge_by_match_id(upcoming_matches, finished_matches)
    existing_matches = _read_json_list(f"daily_matches_{league_code}.json")
    final_matches = _merge_by_match_id(existing_matches, all_matches)

    # PUSH también se incluye: evaluate_market solo devuelve PUSH cuando best_market
    # es vacío/irreconocible — nunca es un resultado legítimo para fútbol.
    needs_resolve = [
        p for p in picks_all
        if (p.get("result") or "PENDING").upper() in ("PENDING", "PUSH")
    ]
    if needs_resolve:
        cached_finished = _build_finished_lookup_from_cache(final_matches)
        _apply_results_by_match_id(needs_resolve, cached_finished)

    # ── 6c. Restore settled picks that got wiped by fresh computation ─────────
    # Safety net: if any WIN/LOSS from existing_picks was overwritten by a freshly
    # computed PENDING pick (e.g. because the match appeared in upcoming_raw or
    # finished_raw) and backfill didn't re-settle it, restore from existing.
    _restore_settled_picks(picks_all, existing_picks)

    # ── 7. AFTR score ─────────────────────────────────────────────────────────

    for p in picks_all:
        try:
            enrich_pick_with_aftr_score(p)
        except Exception as _e:
            logger.debug("apif_refresh_league %s: aftr_score error: %s", league_code, _e)

    # ── 8. Odds (opcional) ────────────────────────────────────────────────────
    if fetch_odds:
        try:
            from services.refresh_odds import _enrich_football_picks_with_odds
            picks_all = _enrich_football_picks_with_odds(league_code, picks_all, final_matches)
        except Exception as _e:
            logger.debug("apif_refresh_league %s: odds error: %s", league_code, _e)

    # ── 8b. Team stats (córners y tarjetas por equipo) ────────────────────────
    try:
        from services.team_stats import update_team_stats_from_fixtures, get_team_averages
        team_stats = update_team_stats_from_fixtures(league_code, finished_raw)
        for p in picks_all:
            home_id = p.get("home_team_id") or p.get("home_id") or p.get("homeTeam", {}).get("id")
            away_id = p.get("away_team_id") or p.get("away_id") or p.get("awayTeam", {}).get("id")
            if home_id:
                avg = get_team_averages(home_id, team_stats)
                if avg:
                    p["home_team_stats"] = avg
            if away_id:
                avg = get_team_averages(away_id, team_stats)
                if avg:
                    p["away_team_stats"] = avg
    except Exception as _e:
        logger.debug("apif_refresh_league %s: team_stats error: %s", league_code, _e)

    # ── 9. Guardar caché ─────────────────────────────────────────────────────
    keep_days = getattr(settings, "daily_keep_days", None)
    picks_daily = _window_daily(picks_all, keep_days)
    _write_league_cache(league_code, final_matches, picks_daily)
    _save_history(league_code, picks_all)
    _save_team_names_cache(team_names)

    # ── 10. Standings (best-effort) ───────────────────────────────────────────
    try:
        from data.providers.api_football import fetch_standings
        from data.cache import write_json
        standings = fetch_standings(league_id, season)
        if standings:
            write_json(f"standings_{league_code}.json", standings)
            logger.debug("standings %s: %d rows", league_code, len(standings))
    except Exception as _e:
        logger.debug("apif_refresh_league %s: standings error: %s", league_code, _e)

    premium_picks = filter_premium_picks(picks_all)
    n_upcoming = len([m for m in upcoming_matches if isinstance(m, dict)])
    n_picks    = len(picks_daily)
    logger.info(
        "apif_refresh_league %s: upcoming=%d | picks=%d (premium=%d) | season=%s",
        league_code, n_upcoming, n_picks, len(premium_picks), season,
    )
    return n_upcoming, n_picks
