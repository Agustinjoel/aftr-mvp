"""
Team stats cache — promedios de córners y tarjetas por equipo.
Se construye a partir de los partidos finalizados fetcheados por apif_refresh_league.
Cache: team_stats_{league_code}.json
  { "team_id": { "name": "...", "corners_for": [...], "yellow": [...], "red": [...] } }
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("aftr.team_stats")

_STATS_FILE = "team_stats_{}.json"
_MAX_HISTORY = 10  # últimos N partidos para el promedio


def _stats_file(league_code: str) -> str:
    return _STATS_FILE.format(league_code)


def load_team_stats(league_code: str) -> dict:
    from data.cache import read_json
    raw = read_json(_stats_file(league_code))
    return raw if isinstance(raw, dict) else {}


def save_team_stats(league_code: str, stats: dict) -> None:
    from data.cache import write_json
    write_json(_stats_file(league_code), stats)


def _avg(lst: list) -> float | None:
    vals = [v for v in lst if v is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def get_team_averages(team_id: int | str, stats: dict) -> dict | None:
    """Devuelve promedios {corners_avg, yellow_avg, red_avg} o None si no hay datos."""
    key = str(team_id)
    entry = stats.get(key)
    if not entry:
        return None
    corners = _avg(entry.get("corners_for", []))
    yellow = _avg(entry.get("yellow", []))
    red = _avg(entry.get("red", []))
    if corners is None and yellow is None:
        return None
    return {
        "name": entry.get("name", ""),
        "corners_avg": corners,
        "yellow_avg": yellow,
        "red_avg": red,
        "games": len(entry.get("corners_for", [])),
    }


def update_team_stats_from_fixtures(
    league_code: str,
    finished_raw: list[dict],
    *,
    max_new_fetches: int = 8,
) -> dict:
    """
    Para cada partido finalizado en finished_raw que aún no tiene stats cacheadas,
    fetchea /fixtures/statistics y actualiza el cache de la liga.
    Limita a max_new_fetches llamadas nuevas por invocación (para no saturar la API).
    Devuelve el dict de stats actualizado.
    """
    from data.providers.api_football import fetch_fixture_statistics

    stats = load_team_stats(league_code)
    fetched_fixture_ids: set = set(stats.get("__fetched_ids__", []))
    new_fetches = 0

    for match in finished_raw:
        if not isinstance(match, dict):
            continue
        fixture_id = match.get("match_id")
        if not fixture_id or fixture_id in fetched_fixture_ids:
            continue
        if new_fetches >= max_new_fetches:
            break

        try:
            fix_stats = fetch_fixture_statistics(int(fixture_id))
        except Exception as e:
            logger.debug("team_stats: error fetching fixture %s: %s", fixture_id, e)
            continue

        new_fetches += 1
        fetched_fixture_ids.add(fixture_id)

        home_id = match.get("home_id")
        away_id = match.get("away_id")

        for team_id, team_key, team_name_key in [
            (home_id, str(home_id) if home_id else None, "home"),
            (away_id, str(away_id) if away_id else None, "away"),
        ]:
            if not team_key:
                continue
            s = fix_stats.get(int(team_id), {})
            entry = stats.setdefault(team_key, {
                "name": match.get(team_name_key, ""),
                "corners_for": [],
                "yellow": [],
                "red": [],
            })
            # Mantener solo los últimos _MAX_HISTORY partidos
            entry["corners_for"] = (entry.get("corners_for", []) + [s.get("corners", 0)])[-_MAX_HISTORY:]
            entry["yellow"] = (entry.get("yellow", []) + [s.get("yellow_cards", 0)])[-_MAX_HISTORY:]
            entry["red"] = (entry.get("red", []) + [s.get("red_cards", 0)])[-_MAX_HISTORY:]
            if not entry.get("name"):
                entry["name"] = match.get(team_name_key, "")

    stats["__fetched_ids__"] = list(fetched_fixture_ids)
    save_team_stats(league_code, stats)
    if new_fetches:
        logger.info("team_stats %s: fetched stats for %d new fixtures", league_code, new_fetches)
    return stats
