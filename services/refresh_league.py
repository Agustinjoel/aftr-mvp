"""
Orquestación del refresco por liga: modos live, upcoming, results y full.
Función pública: refresh_league().
"""
from __future__ import annotations

import logging
import os

from config.settings import settings
from data.providers.football_data import (
    UnsupportedCompetitionError,
    get_finished_matches,
    get_live_matches,
    get_upcoming_matches,
)
from services.aftr_score import enrich_pick_with_aftr_score, filter_premium_picks
from services.refresh_utils import _normalize_match, _read_json_list
from services.refresh_teams import (
    _load_team_names_cache,
    _save_team_names_cache,
    _update_team_names_from_matches,
)
from services.refresh_picks import _build_picks_from_matches
from services.refresh_odds import (
    _enrich_football_picks_with_odds,
    _build_debug_watch_keys,
    _log_odds_debug_saved,
    _is_odds_debug_enabled,
)
from services.refresh_results import (
    _build_finished_lookup_by_id,
    _scores_lookup_from_match_list,
    _apply_results_by_match_id,
    _merge_by_match_id,
    _save_history,
    _window_daily,
    _write_league_cache,
)

logger = logging.getLogger(__name__)


def _get_odds_debug_samples() -> int:
    return int(os.getenv("AFTR_ODDS_DEBUG_SAMPLES", "3"))


def _enrich_and_score(
    league_code: str,
    picks: list[dict],
    matches: list[dict],
    *,
    fetch_odds: bool,
) -> list[dict]:
    """Enriquece picks con odds (opcional) y calcula AFTR score."""
    if fetch_odds:
        debug_samples = _get_odds_debug_samples()
        watch_keys = _build_debug_watch_keys(picks, debug_samples) if _is_odds_debug_enabled() else None
        picks = _enrich_football_picks_with_odds(
            league_code, matches, picks, debug_watch_keys=watch_keys
        )
        if watch_keys:
            saved = _read_json_list(f"daily_picks_{league_code}.json")
            _log_odds_debug_saved(league_code, watch_keys, saved)

    for p in picks:
        if isinstance(p, dict):
            enrich_pick_with_aftr_score(p)

    return picks


# -------------------------
# Modos de refresco parcial
# -------------------------

def _refresh_league_live_only(
    league_code: str,
    metrics: object | None,
) -> tuple[int, int]:
    """Solo actualiza partidos en vivo y re-evalúa resultados parciales."""
    try:
        raw_live = get_live_matches(league_code)
    except UnsupportedCompetitionError:
        return 0, 0
    except Exception as e:
        logger.warning("live refresh %s: %s", league_code, e)
        return 0, 0

    if not raw_live:
        return 0, 0

    team_names = _load_team_names_cache()
    live_matches = [_normalize_match(m) for m in raw_live]
    _update_team_names_from_matches(team_names, live_matches)

    existing_matches = _read_json_list(f"daily_matches_{league_code}.json")
    merged_matches = _merge_by_match_id(existing_matches, live_matches)

    partial_by_id = _scores_lookup_from_match_list(live_matches)
    if partial_by_id:
        existing_picks = _read_json_list(f"daily_picks_{league_code}.json")
        picks_all = _apply_results_by_match_id(existing_picks, partial_by_id)
        for p in picks_all:
            if isinstance(p, dict):
                enrich_pick_with_aftr_score(p)
        keep_days = getattr(settings, "daily_keep_days", None)
        picks_daily = _window_daily(picks_all, keep_days)
        _write_league_cache(league_code, merged_matches, picks_daily)
        _save_history(league_code, picks_all)
    else:
        from data.cache import backup_current_to_prev
        from data.cache import write_json
        backup_current_to_prev(f"daily_matches_{league_code}.json")
        write_json(f"daily_matches_{league_code}.json", merged_matches)

    _save_team_names_cache(team_names)
    if metrics is not None:
        metrics.matches_updated += len(merged_matches)
    return len(raw_live), len(merged_matches)


def _refresh_league_upcoming_only(
    league_code: str,
    fetch_odds: bool,
    metrics: object | None,
) -> tuple[int, int]:
    """Refresca solo partidos próximos y sus picks."""
    try:
        raw_upcoming = get_upcoming_matches(league_code)
        for m in raw_upcoming:
            m["sport"] = "football"
    except UnsupportedCompetitionError:
        return 0, 0

    team_names = _load_team_names_cache()
    upcoming_matches = [_normalize_match(m) for m in (raw_upcoming or [])]
    _update_team_names_from_matches(team_names, upcoming_matches)
    upcoming_picks = _build_picks_from_matches(upcoming_matches, team_names)

    existing_picks = _read_json_list(f"daily_picks_{league_code}.json")
    existing_matches = _read_json_list(f"daily_matches_{league_code}.json")
    merged_matches = _merge_by_match_id(existing_matches, upcoming_matches)
    finished_by_id = _scores_lookup_from_match_list(merged_matches)
    merged_picks = _merge_by_match_id(existing_picks, upcoming_picks)
    picks_all = _apply_results_by_match_id(merged_picks, finished_by_id)

    picks_all = _enrich_and_score(league_code, picks_all, merged_matches, fetch_odds=fetch_odds)

    keep_days = getattr(settings, "daily_keep_days", None)
    picks_daily = _window_daily(picks_all, keep_days)
    _write_league_cache(league_code, merged_matches, picks_daily)
    _save_history(league_code, picks_all)
    _save_team_names_cache(team_names)

    if metrics is not None:
        metrics.matches_updated += len(merged_matches)
    return len(upcoming_matches), len(picks_daily)


def _refresh_league_results_only(
    league_code: str,
    finished_days_back: int,
    fetch_odds: bool,
    metrics: object | None,
) -> tuple[int, int]:
    """Refresca solo resultados de partidos terminados y evalúa picks."""
    existing_picks = _read_json_list(f"daily_picks_{league_code}.json")
    existing_matches = _read_json_list(f"daily_matches_{league_code}.json")
    team_names = _load_team_names_cache()

    try:
        finished_matches = get_finished_matches(league_code, days_back=finished_days_back)
        for m in finished_matches or []:
            m["sport"] = "football"
    except UnsupportedCompetitionError:
        return 0, 0
    except Exception as e:
        logger.warning("results refresh %s: %s", league_code, e)
        return 0, 0

    finished_by_id = _build_finished_lookup_by_id(finished_matches or [])
    finished_matches_norm = [_normalize_match(m) for m in (finished_matches or [])]
    _update_team_names_from_matches(team_names, finished_matches_norm)
    finished_picks = _build_picks_from_matches(finished_matches_norm, team_names)

    merged_matches = _merge_by_match_id(existing_matches, finished_matches_norm)
    merged_picks = _merge_by_match_id(existing_picks, finished_picks)
    picks_all = _apply_results_by_match_id(merged_picks, finished_by_id)

    picks_all = _enrich_and_score(league_code, picks_all, merged_matches, fetch_odds=fetch_odds)

    keep_days = getattr(settings, "daily_keep_days", None)
    picks_daily = _window_daily(picks_all, keep_days)
    _write_league_cache(league_code, merged_matches, picks_daily)
    _save_history(league_code, picks_all)
    _save_team_names_cache(team_names)

    if metrics is not None:
        metrics.matches_updated += len(merged_matches)
    return len(finished_matches_norm), len(picks_daily)


# -------------------------
# Refresco completo por liga (mode=full)
# -------------------------

def _refresh_league_full(
    league_code: str,
    finished_days_back: int,
    fetch_odds: bool,
    metrics: object | None,
) -> tuple[int, int]:
    """Refresco completo: upcoming + finished + live overlay + odds + AFTR score."""

    # 1) Upcoming
    try:
        raw_upcoming = get_upcoming_matches(league_code)
        for m in raw_upcoming:
            m["sport"] = "football"
    except UnsupportedCompetitionError as e:
        logger.warning("Liga no disponible con la API actual (403): %s", e.league_code)
        return 0, 0

    team_names = _load_team_names_cache()
    upcoming_matches = [_normalize_match(m) for m in (raw_upcoming or [])]
    _update_team_names_from_matches(team_names, upcoming_matches)
    upcoming_picks = _build_picks_from_matches(upcoming_matches, team_names)

    # 2) Cache existente
    existing_picks = _read_json_list(f"daily_picks_{league_code}.json")

    # 3) Partidos finalizados
    finished_by_id: dict[int, tuple[int, int]] = {}
    finished_picks: list[dict] = []
    finished_matches_norm: list[dict] = []
    try:
        finished_matches = get_finished_matches(league_code, days_back=finished_days_back)
        for m in finished_matches or []:
            m["sport"] = "football"
        finished_by_id = _build_finished_lookup_by_id(finished_matches or [])
        finished_matches_norm = [_normalize_match(m) for m in (finished_matches or [])]
        _update_team_names_from_matches(team_names, finished_matches_norm)
        finished_picks = _build_picks_from_matches(finished_matches_norm, team_names)
    except Exception as e:
        logger.warning("No pude traer FINISHED para %s (sigo sin evaluar): %s", league_code, e)

    # 4) Merge picks
    merged = _merge_by_match_id(existing_picks, upcoming_picks)
    merged = _merge_by_match_id(merged, finished_picks)

    # 5) Aplicar resultados
    picks_all = _apply_results_by_match_id(merged, finished_by_id)

    # 6) Overlay de partidos en vivo (para status IN_PLAY)
    merged_matches = _merge_by_match_id(upcoming_matches, finished_matches_norm)
    try:
        raw_live = get_live_matches(league_code)
        if raw_live:
            for m in raw_live:
                m["sport"] = "football"
            live_norm = [_normalize_match(m) for m in raw_live]
            merged_matches = _merge_by_match_id(merged_matches, live_norm)
    except UnsupportedCompetitionError:
        pass
    except Exception as e:
        logger.debug("full refresh live overlay %s: %s", league_code, e)

    # 7) Odds + AFTR score
    picks_all = _enrich_and_score(league_code, picks_all, merged_matches, fetch_odds=fetch_odds)

    premium_picks = filter_premium_picks(picks_all)
    logger.info("AFTR premium picks: %s / %s", len(premium_picks), len(picks_all))
    if picks_all:
        sample = next((p for p in picks_all if isinstance(p, dict)), None)
        if sample:
            logger.info(
                "AFTR SAMPLE PICK: %s",
                {k: sample.get(k) for k in (
                    "aftr_score", "tier", "edge", "confidence",
                    "confidence_level", "home", "away", "best_market"
                )},
            )

    # 8) Guardar
    keep_days = getattr(settings, "daily_keep_days", None)
    picks_daily = _window_daily(picks_all, keep_days)
    existing_matches = _read_json_list(f"daily_matches_{league_code}.json")
    final_matches = _merge_by_match_id(existing_matches, merged_matches)
    _write_league_cache(league_code, final_matches, picks_daily)

    _save_history(league_code, picks_all)
    _save_team_names_cache(team_names)

    # 9) Standings (best-effort, una vez por full refresh)
    try:
        from data.providers.football_data import get_standings
        from data.cache import write_json
        standings = get_standings(league_code)
        if standings:
            write_json(f"standings_{league_code}.json", standings)
            logger.debug("standings %s: %d rows", league_code, len(standings))
    except Exception as e:
        logger.debug("standings fetch %s: %s", league_code, e)

    settled = sum(1 for p in picks_daily if (p.get("result") or "").upper() in ("WIN", "LOSS", "PUSH"))
    pending = sum(1 for p in picks_daily if (p.get("result") or "").upper() == "PENDING")
    logger.info(
        "Liga %s: upcoming=%d | daily picks=%d (settled=%d pending=%d) | history updated",
        league_code, len(upcoming_matches), len(picks_daily), settled, pending,
    )
    if metrics is not None:
        metrics.matches_updated += len(final_matches)
    return len(upcoming_matches), len(picks_daily)


# -------------------------
# API pública
# -------------------------

def refresh_league(
    league_code: str,
    *,
    mode: str = "full",
    finished_days_back: int = 7,
    fetch_odds: bool = True,
    metrics: object | None = None,
) -> tuple[int, int]:
    """
    Refresca una liga. Modos:
    - "full": upcoming + finished + live overlay + odds + AFTR (default)
    - "live": solo partidos en vivo
    - "upcoming": solo próximos
    - "results": solo resultados finalizados
    Para basketball, delega a refresh_basketball.
    """
    if league_code not in settings.leagues:
        logger.warning("Liga desconocida: %s", league_code)
        return 0, 0

    mode_norm = (mode or "full").strip().lower()

    sport = getattr(settings, "league_sport", {}).get(league_code, "football")
    if sport == "basketball":
        if mode_norm != "full":
            return 0, 0
        from services import refresh_basketball
        return refresh_basketball.refresh_league_basketball(
            league_code,
            finished_days_back=finished_days_back,
            metrics=metrics,
        )

    if mode_norm == "live":
        return _refresh_league_live_only(league_code, metrics)
    if mode_norm == "upcoming":
        return _refresh_league_upcoming_only(league_code, fetch_odds, metrics)
    if mode_norm == "results":
        return _refresh_league_results_only(league_code, finished_days_back, fetch_odds, metrics)
    if mode_norm != "full":
        logger.warning("Modo refresh desconocido %s (liga %s)", mode_norm, league_code)
        return 0, 0

    return _refresh_league_full(league_code, finished_days_back, fetch_odds, metrics)
