"""
API-Football (RapidAPI) provider — fixtures en vivo, próximos y finalizados.
API by Api-Sports: api-football-v3.p.rapidapi.com
Requiere env var API_FOOTBALL_KEY (X-RapidAPI-Key).
Si la key no está configurada, todas las funciones retornan vacío sin error.

Funciones públicas:
  fetch_live_fixtures()                 — partidos en vivo (todos)
  fetch_fixtures_by_league(id, season)  — próximos o finalizados por liga
  fetch_standings(id, season)           — tabla de posiciones
  normalize_apif_fixture(fixture, code) — convierte fixture al formato AFTR
  list_leagues(search)                  — lista ligas (para verificar IDs)
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any

import requests

logger = logging.getLogger("aftr.api_football")

BASE_URL = "https://api-football-v3.p.rapidapi.com"
HEADERS_TEMPLATE = {
    "X-RapidAPI-Host": "api-football-v3.p.rapidapi.com",
}

_HTTP_TIMEOUT = 10
_MIN_CALL_INTERVAL = 5.0
_last_call_ts: float = 0.0


def _api_key() -> str:
    return (os.getenv("API_FOOTBALL_KEY") or "").strip()


def _headers() -> dict[str, str]:
    return {**HEADERS_TEMPLATE, "X-RapidAPI-Key": _api_key()}


def _throttle() -> None:
    global _last_call_ts
    elapsed = time.time() - _last_call_ts
    if elapsed < _MIN_CALL_INTERVAL:
        time.sleep(_MIN_CALL_INTERVAL - elapsed)
    _last_call_ts = time.time()


def _get(path: str, params: dict | None = None) -> list[Any]:
    """Wrapper GET que retorna response[] o [] en caso de error."""
    key = _api_key()
    if not key:
        return []
    _throttle()
    url = f"{BASE_URL}{path}"
    try:
        resp = requests.get(url, headers=_headers(), params=params or {}, timeout=_HTTP_TIMEOUT)
        if resp.status_code == 429:
            logger.warning("api_football: rate limit hit (429)")
            return []
        if resp.status_code != 200:
            logger.warning("api_football: HTTP %s for %s", resp.status_code, path)
            return []
        data = resp.json()
        return data.get("response") or []
    except requests.RequestException as e:
        logger.warning("api_football: request error %s: %s", path, e)
        return []


def fetch_live_fixtures() -> list[dict]:
    """
    Retorna todos los partidos en vivo ahora mismo.
    Cada item incluye fixture.id, fixture.status.short, teams, goals.
    """
    items = _get("/fixtures", {"live": "all"})
    return [i for i in items if isinstance(i, dict)]


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures por liga — próximos y finalizados
# ──────────────────────────────────────────────────────────────────────────────

# Mapeo de status API-Football → status interno AFTR (mismo formato que football-data.org)
_STATUS_MAP: dict[str, str] = {
    "NS": "TIMED", "TBD": "TIMED",
    "1H": "IN_PLAY", "2H": "IN_PLAY", "ET": "IN_PLAY", "P": "IN_PLAY", "LIVE": "IN_PLAY",
    "HT": "PAUSED", "BT": "PAUSED",
    "FT": "FINISHED", "AET": "FINISHED", "PEN": "FINISHED", "AWD": "FINISHED", "WO": "FINISHED",
    "SUSP": "SUSPENDED", "INT": "PAUSED",
    "PST": "POSTPONED", "CANC": "CANCELLED", "ABD": "CANCELLED",
}


def normalize_apif_fixture(fx: dict, league_code: str = "") -> dict | None:
    """
    Convierte un fixture de API-Football al formato interno AFTR
    (mismo schema que daily_matches_{code}.json generado por football-data.org).
    Devuelve None si el fixture es inválido.
    """
    if not isinstance(fx, dict):
        return None

    fix_info = fx.get("fixture") or {}
    teams    = fx.get("teams") or {}
    goals    = fx.get("goals") or {}
    score    = fx.get("score") or {}

    fix_id = fix_info.get("id")
    if not fix_id:
        return None

    home_team = teams.get("home") or {}
    away_team = teams.get("away") or {}
    home_name = home_team.get("name", "")
    away_name = away_team.get("name", "")
    if not home_name or not away_name:
        return None

    date_str   = fix_info.get("date") or ""
    status_obj = fix_info.get("status") or {}
    st_short   = (status_obj.get("short") or "NS").upper()
    elapsed    = status_obj.get("elapsed")

    aftr_status = _STATUS_MAP.get(st_short, st_short)

    # Goles: usar goals.home/away (dato en tiempo real); fallback score.fulltime
    hg = goals.get("home")
    ag = goals.get("away")
    if hg is None or ag is None:
        ft = score.get("fulltime") or score.get("fullTime") or {}
        hg = ft.get("home")
        ag = ft.get("away")
    # Goles de penales/extra time para el resultado final
    if hg is None:
        for key in ("extratime", "extraTime", "penalty"):
            sub = score.get(key) or {}
            if sub.get("home") is not None:
                hg = sub["home"]
                ag = sub.get("away")
                break

    return {
        "match_id":    int(fix_id),
        "utcDate":     date_str,
        "home":        home_name,
        "away":        away_name,
        "home_crest":  home_team.get("logo") or None,
        "away_crest":  away_team.get("logo") or None,
        "home_id":     home_team.get("id"),
        "away_id":     away_team.get("id"),
        "status":      aftr_status,
        "status_short": st_short,
        "elapsed":     elapsed,
        "score":       {"home": hg, "away": ag},
        "home_goals":  hg,
        "away_goals":  ag,
        "sport":       "football",
        "league_code": league_code,
        # Compatibilidad con football-data.org: algunos módulos leen "homeTeam"/"awayTeam"
        "homeTeam":    {"name": home_name, "crest": home_team.get("logo") or ""},
        "awayTeam":    {"name": away_name, "crest": away_team.get("logo") or ""},
    }


def fetch_fixtures_by_league(
    league_id: int,
    season: int,
    *,
    league_code: str = "",
    days_upcoming: int = 7,
    days_finished: int = 7,
) -> tuple[list[dict], list[dict]]:
    """
    Trae próximos y finalizados para una liga/temporada dada.
    Devuelve (upcoming_normalized, finished_normalized) — ambas en formato AFTR.

    Hace 2 llamadas: una para partidos desde hoy hasta +days_upcoming, otra para
    los últimos days_finished días (para resultados y forma de equipos).
    """
    now   = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    # Próximos
    date_to_up = (now + timedelta(days=days_upcoming)).strftime("%Y-%m-%d")
    raw_up = _get("/fixtures", {
        "league": league_id,
        "season": season,
        "from":   today,
        "to":     date_to_up,
    })
    upcoming = []
    for fx in raw_up:
        n = normalize_apif_fixture(fx, league_code)
        if n:
            upcoming.append(n)

    # Finalizados recientes
    date_from_fin = (now - timedelta(days=days_finished)).strftime("%Y-%m-%d")
    raw_fin = _get("/fixtures", {
        "league": league_id,
        "season": season,
        "from":   date_from_fin,
        "to":     today,
        "status": "FT-AET-PEN-AWD",
    })
    finished = []
    for fx in raw_fin:
        n = normalize_apif_fixture(fx, league_code)
        if n and n.get("home_goals") is not None:
            finished.append(n)

    logger.info(
        "api_football fetch_fixtures_by_league: id=%s season=%s code=%s → up=%d fin=%d",
        league_id, season, league_code, len(upcoming), len(finished),
    )
    return upcoming, finished


def fetch_standings(league_id: int, season: int) -> list[dict]:
    """
    Trae la tabla de posiciones de una liga/temporada.
    Devuelve lista de grupos/tablas; cada grupo es una lista de filas con
    {rank, team.name, points, goalsDiff, all.played/win/draw/lose, etc.}
    """
    items = _get("/standings", {"league": league_id, "season": season})
    if not items:
        return []
    # La respuesta es [{league: {standings: [[row, ...], ...]}}]
    rows: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        league_obj = item.get("league") or {}
        for group in (league_obj.get("standings") or []):
            for row in group:
                if isinstance(row, dict):
                    rows.append(row)
    return rows


def list_leagues(search: str = "", country: str = "") -> list[dict]:
    """
    Lista todas las ligas disponibles en API-Football.
    Útil para verificar IDs. Ejecutar con: python scripts/list_apif_leagues.py
    """
    params: dict[str, Any] = {}
    if search:
        params["search"] = search
    if country:
        params["country"] = country
    items = _get("/leagues", params)
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        lg = item.get("league") or {}
        ct = item.get("country") or {}
        result.append({
            "id":      lg.get("id"),
            "name":    lg.get("name"),
            "type":    lg.get("type"),
            "country": ct.get("name"),
            "logo":    lg.get("logo"),
        })
    return result
