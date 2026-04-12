"""
API-Football (api-sports.io) provider — fixtures en vivo, próximos y finalizados.
API by Api-Sports: v3.football.api-sports.io
Requiere env var API_FOOTBALL_KEY (x-apisports-key).
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

BASE_URL = "https://v3.football.api-sports.io"
HEADERS_TEMPLATE: dict[str, str] = {}

_HTTP_TIMEOUT = 10
_MIN_CALL_INTERVAL = 5.0
_last_call_ts: float = 0.0


def _api_key() -> str:
    return (os.getenv("API_FOOTBALL_KEY") or "").strip()


def _headers() -> dict[str, str]:
    return {"x-apisports-key": _api_key()}


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


def fetch_odds_apif(league_id: int, season: int, date_str: str) -> list[dict]:
    """
    Trae odds de API-Football para una liga/fecha específica.
    Devuelve lista normalizada al formato AFTR:
      { home_team, away_team, date_iso, fixture_id, odds_by_market }

    Bets mapeados:
      - id=1 "Match Winner"   → h2h: { Home Win, Draw, Away Win }
      - id=5 "Goals Over/Under" → totals_25: { Over 2.5, Under 2.5 }
    """
    items = _get("/odds", {"league": league_id, "season": season, "date": date_str})
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        norm = _normalize_apif_odds_event(item)
        if norm:
            out.append(norm)
    logger.info(
        "api_football fetch_odds_apif: league=%s season=%s date=%s → %d events",
        league_id, season, date_str, len(out),
    )
    return out


def _normalize_apif_odds_event(item: dict) -> dict | None:
    """Convierte un evento de /odds de API-Football al formato de odds AFTR."""
    fixture = item.get("fixture") or {}
    teams = (item.get("teams") or {})
    home_team = (teams.get("home") or {}).get("name", "")
    away_team = (teams.get("away") or {}).get("name", "")
    fix_date = fixture.get("date") or ""

    if not home_team or not away_team:
        return None

    # date_iso from fixture.date (ISO string)
    date_iso = ""
    if fix_date:
        try:
            from datetime import datetime, timezone as _tz
            s = fix_date.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            date_iso = dt.date().isoformat()
        except Exception:
            date_iso = fix_date[:10] if len(fix_date) >= 10 else ""

    if not date_iso:
        return None

    odds_by_market: dict[str, dict[str, float]] = {}
    bookmaker_title = ""

    for bm in item.get("bookmakers") or []:
        bm_name = (bm.get("name") or "").strip()
        for bet in bm.get("bets") or []:
            bet_id = bet.get("id")
            if bet_id == 1 and "h2h" not in odds_by_market:
                # Match Winner: Home / Draw / Away
                by_outcome: dict[str, float] = {}
                for v in bet.get("values") or []:
                    val = (v.get("value") or "").strip()
                    try:
                        dec = float(v.get("odd") or 0)
                    except (TypeError, ValueError):
                        continue
                    if val == "Home":
                        by_outcome["Home Win"] = dec
                    elif val == "Draw":
                        by_outcome["Draw"] = dec
                    elif val == "Away":
                        by_outcome["Away Win"] = dec
                if by_outcome:
                    odds_by_market["h2h"] = by_outcome
                    if not bookmaker_title and bm_name:
                        bookmaker_title = bm_name
            elif bet_id == 5 and "totals_25" not in odds_by_market:
                # Goals Over/Under — pick the 2.5 line
                for v in bet.get("values") or []:
                    val = (v.get("value") or "").strip()
                    try:
                        dec = float(v.get("odd") or 0)
                    except (TypeError, ValueError):
                        continue
                    if val == "Over 2.5":
                        odds_by_market.setdefault("totals_25", {})["Over 2.5"] = dec
                    elif val == "Under 2.5":
                        odds_by_market.setdefault("totals_25", {})["Under 2.5"] = dec
                    if not bookmaker_title and bm_name:
                        bookmaker_title = bm_name
            elif bet_id == 45 and "corners" not in odds_by_market:
                # Corners Over/Under — preferred line 9.5, fallback to first available
                corner_candidates: dict[str, dict[str, float]] = {}
                for v in bet.get("values") or []:
                    val = (v.get("value") or "").strip()
                    try:
                        dec = float(v.get("odd") or 0)
                    except (TypeError, ValueError):
                        continue
                    # val format: "Over 9.5" / "Under 9.5"
                    parts = val.split()
                    if len(parts) == 2 and parts[0] in ("Over", "Under"):
                        line = parts[1]
                        corner_candidates.setdefault(line, {})[parts[0]] = dec
                # Pick preferred line: 9.5 > 10.5 > 8.5 > first available
                for preferred in ("9.5", "10.5", "8.5"):
                    if preferred in corner_candidates and len(corner_candidates[preferred]) == 2:
                        mk = {f"Over {preferred}": corner_candidates[preferred]["Over"],
                              f"Under {preferred}": corner_candidates[preferred]["Under"]}
                        odds_by_market["corners"] = mk
                        if not bookmaker_title and bm_name:
                            bookmaker_title = bm_name
                        break
                if "corners" not in odds_by_market and corner_candidates:
                    line = next(iter(corner_candidates))
                    if len(corner_candidates[line]) == 2:
                        mk = {f"Over {line}": corner_candidates[line]["Over"],
                              f"Under {line}": corner_candidates[line]["Under"]}
                        odds_by_market["corners"] = mk
            elif bet_id == 80 and "cards" not in odds_by_market:
                # Cards Over/Under — preferred line 3.5, fallback to first available
                card_candidates: dict[str, dict[str, float]] = {}
                for v in bet.get("values") or []:
                    val = (v.get("value") or "").strip()
                    try:
                        dec = float(v.get("odd") or 0)
                    except (TypeError, ValueError):
                        continue
                    parts = val.split()
                    if len(parts) == 2 and parts[0] in ("Over", "Under"):
                        line = parts[1]
                        card_candidates.setdefault(line, {})[parts[0]] = dec
                for preferred in ("3.5", "4.5", "2.5"):
                    if preferred in card_candidates and len(card_candidates[preferred]) == 2:
                        mk = {f"Over {preferred}": card_candidates[preferred]["Over"],
                              f"Under {preferred}": card_candidates[preferred]["Under"]}
                        odds_by_market["cards"] = mk
                        if not bookmaker_title and bm_name:
                            bookmaker_title = bm_name
                        break
                if "cards" not in odds_by_market and card_candidates:
                    line = next(iter(card_candidates))
                    if len(card_candidates[line]) == 2:
                        mk = {f"Over {line}": card_candidates[line]["Over"],
                              f"Under {line}": card_candidates[line]["Under"]}
                        odds_by_market["cards"] = mk

    if not odds_by_market:
        return None

    result: dict = {
        "home_team": home_team,
        "away_team": away_team,
        "date_iso": date_iso,
        "fixture_id": fixture.get("id"),
        "odds_by_market": odds_by_market,
    }
    if bookmaker_title:
        result["bookmaker_title"] = bookmaker_title
    return result


def fetch_fixture_statistics(fixture_id: int) -> dict:
    """
    Trae estadísticas de un partido finalizado.
    Devuelve {team_id: {corners, yellow_cards, red_cards}} para los dos equipos.
    """
    items = _get("/fixtures/statistics", {"fixture": fixture_id})
    result: dict[int, dict] = {}
    for team_block in items:
        if not isinstance(team_block, dict):
            continue
        team = team_block.get("team") or {}
        team_id = team.get("id")
        if not team_id:
            continue
        stats: dict[str, int] = {"corners": 0, "yellow_cards": 0, "red_cards": 0}
        for s in team_block.get("statistics") or []:
            stype = (s.get("type") or "").strip()
            val = s.get("value")
            try:
                n = int(val) if val is not None else 0
            except (TypeError, ValueError):
                n = 0
            if stype == "Corner Kicks":
                stats["corners"] = n
            elif stype == "Yellow Cards":
                stats["yellow_cards"] = n
            elif stype == "Red Cards":
                stats["red_cards"] = n
        result[int(team_id)] = stats
    return result


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
