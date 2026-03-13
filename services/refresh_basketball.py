"""
Basketball/NBA-only refresh flow. Separate from football.
Provider: api_sports_basketball. Picks: core.basketball_picks. Evaluation: core.basketball_evaluation.
Writes daily_matches_{league_code}.json and daily_picks_{league_code}.json (e.g. NBA).
"""
from __future__ import annotations

import logging
from typing import Any

from config.settings import settings
from core.basketball_evaluation import evaluate_basketball_market
from core.basketball_picks import build_basketball_picks
from data.cache import read_json, write_json
from data.providers.api_sports_basketball import get_finished_games, get_upcoming_games

from services.refresh import (
    _build_finished_lookup_by_id,
    _load_team_names_cache,
    _merge_by_match_id,
    _normalize_match,
    _read_json_list,
    _save_history,
    _save_team_names_cache,
    _update_team_names_from_matches,
    _window_daily,
)

logger = logging.getLogger(__name__)


def _apply_results_basketball(
    picks: list[dict], finished_by_id: dict[int, tuple[int, int]]
) -> list[dict]:
    """Apply results to basketball picks using basketball evaluator (points = home_goals/away_goals)."""
    for p in picks or []:
        if not isinstance(p, dict):
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
        home_pts, away_pts = finished_by_id[mid_i]
        p["score_home"] = int(home_pts)
        p["score_away"] = int(away_pts)
        res = (p.get("result") or "").strip().upper()
        if res in ("", "PENDING", "NONE"):
            market = (p.get("best_market") or "").strip()
            result, _reason = evaluate_basketball_market(market, home_pts, away_pts)
            p["result"] = result
    return picks


def refresh_league_basketball(league_code: str) -> tuple[int, int]:
    """
    Full NBA/basketball refresh: upcoming + finished from basketball provider,
    build picks with basketball_picks, apply results with basketball_evaluation,
    persist daily_matches_{league_code}.json and daily_picks_{league_code}.json.
    """
    if league_code not in settings.leagues:
        logger.warning("Unknown league: %s", league_code)
        return 0, 0

    # 1) Upcoming
    raw_upcoming = get_upcoming_games(league_code, days=7)
    team_names = _load_team_names_cache()
    upcoming_matches = [_normalize_match(m) for m in (raw_upcoming or [])]
    _update_team_names_from_matches(team_names, upcoming_matches)
    upcoming_picks = build_basketball_picks(upcoming_matches)

    # 2) Existing cache
    existing_picks = _read_json_list(f"daily_picks_{league_code}.json")

    # 3) Finished
    finished_by_id: dict[int, tuple[int, int]] = {}
    finished_picks: list[dict] = []
    finished_matches_norm: list[dict] = []
    try:
        finished_matches = get_finished_games(league_code, days_back=7)
        finished_by_id = _build_finished_lookup_by_id(finished_matches or [])
        finished_matches_norm = [_normalize_match(m) for m in (finished_matches or [])]
        _update_team_names_from_matches(team_names, finished_matches_norm)
        finished_picks = build_basketball_picks(finished_matches_norm)
    except Exception as e:
        logger.warning("Could not fetch finished games for %s: %s", league_code, e)

    # 4) Merge picks
    merged = _merge_by_match_id(existing_picks, upcoming_picks)
    merged = _merge_by_match_id(merged, finished_picks)

    # 5) Apply results (basketball evaluator only)
    picks_all = _apply_results_basketball(merged, finished_by_id)

    # 6) Daily window
    keep_days = getattr(settings, "daily_keep_days", None)
    picks_daily = _window_daily(picks_all, keep_days)

    # 7) Matches (align with picks)
    merged_matches = _merge_by_match_id(upcoming_matches, finished_matches_norm)
    existing_matches = _read_json_list(f"daily_matches_{league_code}.json")
    merged_matches = _merge_by_match_id(merged_matches, existing_matches)
    write_json(f"daily_matches_{league_code}.json", merged_matches)

    write_json(f"daily_picks_{league_code}.json", picks_daily)

    # 8) History + team names
    _save_history(league_code, picks_all)
    _save_team_names_cache(team_names)

    settled = sum(1 for p in picks_daily if (p.get("result") or "").upper() in ("WIN", "LOSS", "PUSH"))
    pending = sum(1 for p in picks_daily if (p.get("result") or "").upper() == "PENDING")
    logger.info(
        "Basketball %s: upcoming=%d | daily picks=%d (settled=%d pending=%d)",
        league_code,
        len(upcoming_matches),
        len(picks_daily),
        settled,
        pending,
    )
    return len(upcoming_matches), len(picks_daily)
