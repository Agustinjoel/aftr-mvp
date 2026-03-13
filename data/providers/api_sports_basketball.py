"""
Cliente API-Sports Basketball API v1.
Usa API_SPORTS_KEY desde config/env.
Endpoint: https://v1.basketball.api-sports.io
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import requests

from config.settings import API_SPORTS_KEY, NBA_DATE_FROM, NBA_DATE_TO, NBA_SEASON

logger = logging.getLogger(__name__)

BASE = "https://v1.basketball.api-sports.io"

# League code -> API-Sports league id (NBA = 12 from GET /leagues)
LEAGUES = {
    "NBA": 12,
}


def _nba_season_string() -> str:
    """Current NBA season as 'YYYY-YYYY' (e.g. '2024-2025'). API-Sports expects this format."""
    now = datetime.now(timezone.utc)
    if now.month >= 10:
        start_year = now.year
    else:
        start_year = now.year - 1
    return f"{start_year}-{start_year + 1}"


def _is_valid_season_format(s: str) -> bool:
    """True if s looks like YYYY-YYYY with second = first + 1."""
    if not s or "-" not in s:
        return False
    parts = s.strip().split("-")
    if len(parts) != 2:
        return False
    try:
        a, b = int(parts[0]), int(parts[1])
        return b == a + 1 and 1990 <= a <= 2100
    except ValueError:
        return False


def _previous_season(season: str) -> str:
    """'2025-2026' -> '2024-2025'."""
    parts = season.split("-", 1)
    if len(parts) != 2:
        return season
    try:
        start = int(parts[0])
        return f"{start - 1}-{start}"
    except ValueError:
        return season


def _resolve_nba_season() -> tuple[str, bool]:
    """
    Returns (season_string, is_override).
    If NBA_SEASON is set and valid, use it (is_override=True, no fallback).
    Else use automatic _nba_season_string() (is_override=False, fallback allowed).
    """
    if NBA_SEASON and _is_valid_season_format(NBA_SEASON):
        return NBA_SEASON, True
    return _nba_season_string(), False


def _headers() -> dict[str, str]:
    if not API_SPORTS_KEY:
        raise RuntimeError("API_SPORTS_KEY no está configurada (config o .env)")
    return {"x-apisports-key": API_SPORTS_KEY}


def _get(path: str, params: dict | None = None) -> dict:
    url = f"{BASE}{path}"
    r = requests.get(url, headers=_headers(), params=params or {}, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"API-Sports Basketball Error {r.status_code}: {r.text[:500]}")
    return r.json()


def _get_games_response(league_id: int, season: str) -> list:
    """Fetch /games for league and season; return the list of game objects (response or games)."""
    data = _get("/games", params={"league": league_id, "season": season})
    response = data.get("response")
    if not isinstance(response, list):
        response = data.get("games") or []
    return response


def _safe_int(val, default=None):
    """Return int(val) or default on failure."""
    if val is None:
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _parse_game_date(date_str: str):
    """Parse game date from API value (e.g. '2025-02-01' or '2025-02-01T00:00:00+00:00'). Returns date or None."""
    if not date_str:
        return None
    s = (date_str or "").strip().replace("Z", "+00:00")
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        pass
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _extract_scores(g: dict) -> tuple[int | None, int | None]:
    """
    Extract home and away final points from API-Sports Basketball game object.
    Tries: scores.home/away (dict with points/score or number), teams.home/away, homeCompetitor/awayCompetitor.
    Returns (home_pts, away_pts) or (None, None).
    """
    def _one_score(v) -> int | None:
        if v is None:
            return None
        if isinstance(v, dict):
            v = v.get("points") or v.get("score") or v.get("total")
        return _safe_int(v)

    home_pts, away_pts = None, None

    scores = g.get("scores")
    if isinstance(scores, dict):
        home_pts = _one_score(scores.get("home"))
        away_pts = _one_score(scores.get("away"))
    if home_pts is not None and away_pts is not None:
        return home_pts, away_pts

    teams = g.get("teams")
    if isinstance(teams, dict):
        if home_pts is None:
            home_pts = _one_score(teams.get("home"))
        if away_pts is None:
            away_pts = _one_score(teams.get("away"))
    if home_pts is not None and away_pts is not None:
        return home_pts, away_pts

    home_c = g.get("homeCompetitor") or {}
    away_c = g.get("awayCompetitor") or {}
    if isinstance(home_c, dict) and home_pts is None:
        home_pts = _safe_int(home_c.get("score") or home_c.get("points"))
    if isinstance(away_c, dict) and away_pts is None:
        away_pts = _safe_int(away_c.get("score") or away_c.get("points"))

    return home_pts, away_pts


def _crest_from_team_id(team_id: int | None) -> str | None:
    """API-Sports basketball team logo URL by team id. Returns None if no id."""
    if team_id is None:
        return None
    try:
        return f"https://media.api-sports.io/basketball/teams/{int(team_id)}.png"
    except (TypeError, ValueError):
        return None


def _game_to_match(g: dict, league_code: str, home_goals: int | None = None, away_goals: int | None = None) -> dict:
    """Normalize one game to the same shape as football provider (match_id, utcDate, home, away, league, ...)."""
    # Support both "response" style (teams.home/away) and "games" style (homeCompetitor/awayCompetitor)
    home_id = None
    away_id = None
    home_name = ""
    away_name = ""

    teams = g.get("teams") or {}
    if teams:
        home_info = teams.get("home") or {}
        away_info = teams.get("away") or {}
        home_id = home_info.get("id")
        away_id = away_info.get("id")
        home_name = (home_info.get("name") or "").strip()
        away_name = (away_info.get("name") or "").strip()

    if not home_name or not away_name:
        home_c = g.get("homeCompetitor") or {}
        away_c = g.get("awayCompetitor") or {}
        home_id = home_id or home_c.get("id")
        away_id = away_id or away_c.get("id")
        home_name = home_name or (home_c.get("name") or "").strip()
        away_name = away_name or (away_c.get("name") or "").strip()

    home_crest = _crest_from_team_id(home_id) or (teams.get("home") or {}).get("logo") if isinstance(teams.get("home"), dict) else None
    away_crest = _crest_from_team_id(away_id) or (teams.get("away") or {}).get("logo") if isinstance(teams.get("away"), dict) else None

    # Date: API often has "date" (YYYY-MM-DD) + "time" (HH:MM) or "timestamp"
    date_str = g.get("date") or ""
    time_str = (g.get("time") or "").strip()
    timestamp = g.get("timestamp")
    utc_date = ""
    if timestamp is not None:
        try:
            dt = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
            utc_date = dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        except Exception:
            pass
    if not utc_date and date_str:
        if time_str:
            utc_date = f"{date_str}T{time_str}:00+00:00"
        else:
            utc_date = f"{date_str}T00:00:00+00:00"

    return {
        "match_id": g.get("id"),
        "utcDate": utc_date,
        "home": home_name or "Home",
        "away": away_name or "Away",
        "league": league_code,
        "home_team_id": home_id,
        "away_team_id": away_id,
        "home_crest": home_crest,
        "away_crest": away_crest,
        "sport": "basketball",
    } | ({"home_goals": home_goals, "away_goals": away_goals} if home_goals is not None and away_goals is not None else {})


def get_upcoming_games(league_code: str, days: int = 7) -> list[dict]:
    """
    Games programados (próximos). Devuelve lista con sport="basketball".
    Formato compatible con pipeline: match_id, utcDate, home, away, league, home_team_id, away_team_id, home_crest, away_crest, sport.
    """
    league_id = LEAGUES.get(league_code)
    if league_id is None:
        logger.warning("Liga de basketball no configurada: %s", league_code)
        return []

    now = datetime.now(timezone.utc)
    end = now + timedelta(days=max(1, days))
    window_start = now.date()
    window_end = end.date()
    if league_code == "NBA" and NBA_DATE_FROM and NBA_DATE_TO:
        try:
            ws = datetime.strptime(NBA_DATE_FROM.strip(), "%Y-%m-%d").date()
            we = datetime.strptime(NBA_DATE_TO.strip(), "%Y-%m-%d").date()
            window_start = min(ws, we)
            window_end = max(ws, we)
        except ValueError:
            pass
    season, is_override = _resolve_nba_season()
    used_fallback = False
    actual_season = season

    try:
        response = _get_games_response(league_id, season)
        if not is_override and len(response) == 0:
            prev_season = _previous_season(season)
            response = _get_games_response(league_id, prev_season)
            if response:
                used_fallback = True
                actual_season = prev_season
                logger.info("NBA upcoming: season %s had 0 games; using available season %s", season, prev_season)
    except Exception as e:
        logger.warning("API-Sports Basketball get_upcoming_games failed: %s", e)
        return []

    # temporary debug (reports the season that actually produced the response)
    print("[NBA get_upcoming_games] actual season: %s | raw game count: %d" % (actual_season, len(response)))

    upcoming_status = {"NS", "TBD", "LIVE", "HT", "C1", "C2", "C3", "C4", "1Q", "2Q", "3Q", "4Q"}

    if league_code == "NBA" and used_fallback and response and not (NBA_DATE_FROM and NBA_DATE_TO):
        dates_in_response = []
        for g in response:
            if not isinstance(g, dict):
                continue
            status = (g.get("status") or {}).get("short") if isinstance(g.get("status"), dict) else g.get("status")
            status_key = (status or "").strip().upper()
            if status_key and status_key not in upcoming_status:
                continue
            date_str = (g.get("date") or "").strip().replace("Z", "+00:00")
            game_date = _parse_game_date(date_str)
            if game_date is not None:
                dates_in_response.append(game_date)
        if dates_in_response:
            window_start = min(dates_in_response)
            window_end = max(dates_in_response)

    out: list[dict] = []
    for g in response:
        if not isinstance(g, dict):
            continue
        status = (g.get("status") or {}).get("short") if isinstance(g.get("status"), dict) else g.get("status")
        status_key = (status or "").strip().upper()
        if status_key and status_key not in upcoming_status:
            continue
        date_str = (g.get("date") or "").strip().replace("Z", "+00:00")
        game_date = _parse_game_date(date_str)
        if game_date is None:
            continue
        if game_date < window_start or game_date > window_end:
            continue
        out.append(_game_to_match(g, league_code))
    out.sort(key=lambda x: (x.get("utcDate") or ""))
    return out[:80]


def get_finished_games(league_code: str, days_back: int = 7) -> list[dict]:
    """
    Games finalizados con resultado (home_goals/away_goals = points).
    Incluye sport="basketball".
    """
    league_id = LEAGUES.get(league_code)
    if league_id is None:
        logger.warning("Liga de basketball no configurada: %s", league_code)
        return []

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=max(1, days_back))
    window_start = start.date()
    window_end = now.date()
    if league_code == "NBA" and NBA_DATE_FROM and NBA_DATE_TO:
        try:
            ws = datetime.strptime(NBA_DATE_FROM.strip(), "%Y-%m-%d").date()
            we = datetime.strptime(NBA_DATE_TO.strip(), "%Y-%m-%d").date()
            window_start = min(ws, we)
            window_end = max(ws, we)
        except ValueError:
            pass
    season, is_override = _resolve_nba_season()

    try:
        response = _get_games_response(league_id, season)
        if not is_override and len(response) == 0:
            prev_season = _previous_season(season)
            response = _get_games_response(league_id, prev_season)
            if response:
                logger.info("NBA: current season %s returned 0 games; using previous season %s", season, prev_season)
    except Exception as e:
        logger.warning("API-Sports Basketball get_finished_games failed: %s", e)
        return []

    FINISHED_STATUSES = ("FT", "FINISHED", "3OT", "2OT", "1OT", "OT", "AOT")
    out: list[dict] = []
    for g in response:
        if not isinstance(g, dict):
            continue
        status = (g.get("status") or {}).get("short") if isinstance(g.get("status"), dict) else g.get("status")
        if not status or str(status).upper() not in FINISHED_STATUSES:
            continue
        date_str = (g.get("date") or "").strip().replace("Z", "+00:00")
        game_date = _parse_game_date(date_str)
        if game_date is None:
            continue
        if game_date < window_start or game_date > window_end:
            continue
        home_pts, away_pts = _extract_scores(g)
        if home_pts is not None and away_pts is not None:
            out.append(_game_to_match(g, league_code, home_goals=home_pts, away_goals=away_pts))
    out.sort(key=lambda x: (x.get("utcDate") or ""), reverse=True)
    return out
