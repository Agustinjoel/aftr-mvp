"""
Cliente de la API Football-Data.org v4.
Usa config.settings para API key.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from config.settings import FOOTBALL_DATA_API_KEY

BASE = "https://api.football-data.org/v4"
COMPETITIONS = {"PL": "PL", "PD": "PD", "SA": "SA", "BL1": "BL1", "FL1": "FL1", "CL": "CL"}


def _headers() -> dict[str, str]:
    if not FOOTBALL_DATA_API_KEY:
        raise RuntimeError("FOOTBALL_DATA_API_KEY no está configurada (config o .env)")
    return {"X-Auth-Token": FOOTBALL_DATA_API_KEY}


def _get(path: str, params: dict | None = None) -> dict:
    url = f"{BASE}{path}"
    r = requests.get(url, headers=_headers(), params=params, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"Football-Data error {r.status_code}: {r.text}")
    return r.json()


def _crest_from_team_id(team_id: int | None) -> str | None:
    """URL del escudo; None si team_id es None."""
    if team_id is None:
        return None
    return f"https://crests.football-data.org/{team_id}.png"


def get_team_crest(team_id: int) -> str:
    """URL del escudo del equipo (football-data.org crests CDN)."""
    return _crest_from_team_id(team_id) or ""


def get_upcoming_matches(league_code: str, days: int = 3) -> list[dict]:
    """Partidos programados; incluye home_crest/away_crest desde CDN."""
    comp = COMPETITIONS.get(league_code, "PL")
    data = _get(f"/competitions/{comp}/matches", params={"status": "SCHEDULED"})
    matches = data.get("matches", [])

    out = []
    for m in matches:
        home = (m.get("homeTeam") or {}).get("name", "")
        away = (m.get("awayTeam") or {}).get("name", "")
        utc = m.get("utcDate", "")
        hid = (m.get("homeTeam") or {}).get("id")
        aid = (m.get("awayTeam") or {}).get("id")
        out.append({
            "utcDate": utc,
            "home": home,
            "away": away,
            "league": league_code,
            "home_team_id": hid,
            "away_team_id": aid,
            "home_crest": _crest_from_team_id(hid),
            "away_crest": _crest_from_team_id(aid),
        })
    return out[:60]


def get_finished_matches(league_code: str, days_back: int = 5) -> list[dict]:
    """
    Partidos finalizados con resultado (home_goals, away_goals).
    Incluye home, away, utcDate para cruce con picks.
    """
    comp = COMPETITIONS.get(league_code, "PL")
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days_back)
    date_from = start.strftime("%Y-%m-%d")
    date_to = end.strftime("%Y-%m-%d")
    data = _get(
        f"/competitions/{comp}/matches",
        params={"status": "FINISHED", "dateFrom": date_from, "dateTo": date_to},
    )
    matches = data.get("matches", [])

    out = []
    for m in matches:
        home = (m.get("homeTeam") or {}).get("name", "")
        away = (m.get("awayTeam") or {}).get("name", "")
        utc = m.get("utcDate", "")
        score = m.get("score") or {}
        ft = score.get("fullTime") or {}
        hg = ft.get("home")
        ag = ft.get("away")
        if hg is None or ag is None:
            continue
        out.append({
            "utcDate": utc,
            "home": home,
            "away": away,
            "league": league_code,
            "home_goals": int(hg),
            "away_goals": int(ag),
        })
    return out
