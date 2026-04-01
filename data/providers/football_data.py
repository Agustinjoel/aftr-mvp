"""
Cliente de la API Football-Data.org v4.
Usa config.settings para API key.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import copy
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import requests

from config.settings import CACHE_DIR, FOOTBALL_DATA_API_KEY

logger = logging.getLogger(__name__)

# --- Per-refresh cycle: dedupe identical GETs + HTTP / rate-limit stats (thread-safe) ---
_cycle_lock = threading.Lock()
_cycle_active = False
_cycle_cache: dict[str, dict[str, Any]] = {}
_stats_lock = threading.Lock()
_cycle_stats: dict[str, int] = {
    "http_requests": 0,
    "cache_hits": 0,
    "rate_limit_sleep_sec": 0,
}

# Respuestas GET recientes fuera del ciclo de refresh (TTL corto; ahorra cuota).
_ttl_lock = threading.Lock()
_ttl_store: dict[str, tuple[float, dict[str, Any]]] = {}


def _http_ttl_seconds() -> int:
    try:
        from config.settings import settings

        return max(0, int(getattr(settings, "football_http_cache_ttl_sec", 45) or 0))
    except Exception:
        try:
            return max(0, int((os.getenv("FOOTBALL_HTTP_CACHE_TTL_SEC") or "45").strip()))
        except ValueError:
            return 0


def _apply_rate_backoff_from_wait(wait: int) -> None:
    try:
        from services.refresh_rate_guard import apply_backoff_seconds

        cap = float((os.getenv("REFRESH_BACKOFF_SECONDS") or "120").strip() or "120")
        apply_backoff_seconds(float(wait), cap)
    except Exception:
        pass


def _cache_key(path: str, params: dict | None) -> str:
    p = params or {}
    enc = json.dumps(sorted(p.items()), default=str, sort_keys=True)
    return f"{path}|{enc}"


def _note_rate_limit_sleep(seconds: int) -> None:
    try:
        s = max(0, int(seconds))
    except (TypeError, ValueError):
        s = 0
    with _stats_lock:
        _cycle_stats["rate_limit_sleep_sec"] += s


@contextmanager
def football_data_refresh_cycle() -> Iterator[dict[str, int]]:
    """
    Wrap a single refresh run: reset stats, enable in-memory response dedupe for identical URLs.
    Yields a live stats dict (http_requests, cache_hits, rate_limit_sleep_sec).
    """
    global _cycle_active
    with _cycle_lock:
        with _stats_lock:
            _cycle_stats["http_requests"] = 0
            _cycle_stats["cache_hits"] = 0
            _cycle_stats["rate_limit_sleep_sec"] = 0
            _cycle_cache.clear()
            _cycle_active = True
    try:
        yield _cycle_stats
    finally:
        with _cycle_lock:
            _cycle_active = False
            _cycle_cache.clear()


def get_football_data_cycle_stats_snapshot() -> dict[str, int]:
    with _stats_lock:
        return dict(_cycle_stats)


class UnsupportedCompetitionError(Exception):
    """Raised when the API returns 403 for a competition (not in current plan)."""
    def __init__(self, league_code: str) -> None:
        self.league_code = league_code
        super().__init__(f"Competition not available with current API key: {league_code}")

BASE = "https://api.football-data.org/v4"
COMPETITIONS = {
    "BSA": "BSA",  # Brasileirao
    "ELC": "ELC",  # Championship
    "PL": "PL",
    "CL": "CL",
    "EL": "EL",    # UEFA Europa League
    "EC": "EC",    # Euro
    "FL1": "FL1",
    "BL1": "BL1",
    "SA": "SA",
    "DED": "DED",  # Eredivisie
    "PPL": "PPL",  # Portugal
    "CLI": "CLI",  # Copa Libertadores
    "PD": "PD",
    "WC": "WC",
}

_UNSUPPORTED_FILE: Path = CACHE_DIR / "unsupported_leagues.json"


def _load_unsupported() -> set[str]:
    """Load list of league codes that returned 403 (restricted with current API key)."""
    try:
        if _UNSUPPORTED_FILE.exists():
            raw = _UNSUPPORTED_FILE.read_text(encoding="utf-8")
            data = json.loads(raw)
            return set(data) if isinstance(data, list) else set()
    except Exception as e:
        logger.debug("Could not load unsupported leagues: %s", e)
    return set()


def _save_unsupported(codes: set[str]) -> None:
    """Persist unsupported league codes so UI and refresh can skip them."""
    try:
        _UNSUPPORTED_FILE.parent.mkdir(parents=True, exist_ok=True)
        _UNSUPPORTED_FILE.write_text(json.dumps(sorted(codes)), encoding="utf-8")
    except Exception as e:
        logger.warning("Could not save unsupported leagues: %s", e)


def get_unsupported_leagues() -> set[str]:
    """Return league codes that are restricted (403) with the current API key."""
    return _load_unsupported()


def register_unsupported(league_code: str) -> None:
    """Mark a league as unsupported after a 403 response. Extensible for other restricted competitions."""
    codes = _load_unsupported()
    if league_code in codes:
        return
    codes.add(league_code)
    _save_unsupported(codes)
    logger.info("Competition %s marked as unsupported (403) for current API key.", league_code)


# ✅ DEV/PROD switch:
#   AFTR_SLEEP_ON_429=0 -> no duerme, tira error rápido (ideal dev)
#   AFTR_SLEEP_ON_429=1 -> duerme y reintenta (ideal prod)
SLEEP_ON_429 = os.getenv("AFTR_SLEEP_ON_429", "1").strip().lower() in ("1", "true", "yes")


def _headers() -> dict[str, str]:
    if not FOOTBALL_DATA_API_KEY:
        raise RuntimeError("FOOTBALL_DATA_API_KEY no está configurada (config o .env)")
    return {"X-Auth-Token": FOOTBALL_DATA_API_KEY}


def _get(path: str, params: dict | None = None) -> dict:
    ck = _cache_key(path, params)
    with _cycle_lock:
        active = _cycle_active
        if active and ck in _cycle_cache:
            with _stats_lock:
                _cycle_stats["cache_hits"] += 1
            return copy.deepcopy(_cycle_cache[ck])

    ttl = _http_ttl_seconds()
    if ttl > 0 and not active:
        nowm = time.monotonic()
        with _ttl_lock:
            ent = _ttl_store.get(ck)
            if ent and nowm < ent[0]:
                logger.debug("football_data: cache TTL hit (no HTTP) | %s", path)
                return copy.deepcopy(ent[1])

    url = f"{BASE}{path}"
    attempt = 0
    while True:
        with _cycle_lock:
            act = _cycle_active
        if act:
            with _stats_lock:
                _cycle_stats["http_requests"] += 1

        r = requests.get(url, headers=_headers(), params=params, timeout=20)

        # NOMBRES CORRECTOS DE HEADERS (observados en responses)
        remaining = r.headers.get("X-Requests-Available-Minute") or r.headers.get("X-Requests-Available-Minute".lower())
        reset = r.headers.get("X-RequestCounter-Reset") or r.headers.get("X-RequestCounter-Reset".lower())

        # si 429 -> sleep según reset (o backoff)
        if r.status_code == 429:
            # si AFTR_SLEEP_ON_429 está en env y es 0, fallamos rápido; si está 1, dormimos
            sleep_on_429 = int(os.getenv("AFTR_SLEEP_ON_429", "1"))
            wait = int(reset or 60)
            if sleep_on_429:
                logger.warning("Football-Data 429. Sleeping %ss... (%s)", wait, url)
                _note_rate_limit_sleep(wait)
                _apply_rate_backoff_from_wait(wait)
                time.sleep(wait)
                attempt = 0
                continue
            else:
                # fallback rápido: raise y que el caller lo maneje
                raise RuntimeError(f"Football-Data 429: {r.text}")

        # si va quedando poco presupuesto, esperamos un poco para no freakear
        if remaining is not None:
            try:
                rem = int(remaining)
                if rem <= 1:
                    wait = int(reset or 60)
                    logger.warning("Rate limit bajo (%s). Sleeping %ss...", rem, wait)
                    _note_rate_limit_sleep(wait)
                    _apply_rate_backoff_from_wait(wait)
                    time.sleep(wait)
            except ValueError:
                pass

        if r.status_code == 403:
            # Competition restricted with current API plan; mark and raise so callers can skip gracefully.
            parts = path.strip("/").split("/")
            comp = parts[1] if len(parts) >= 2 and parts[0] == "competitions" else None
            if comp:
                register_unsupported(comp)
                raise UnsupportedCompetitionError(comp)
            raise RuntimeError(f"Football-Data 403: {r.text}")

        if r.status_code != 200:
            raise RuntimeError(f"Football-Data Error {r.status_code}: {r.text}")

        payload = r.json()
        with _cycle_lock:
            if _cycle_active:
                if isinstance(payload, dict):
                    _cycle_cache[ck] = copy.deepcopy(payload)
        if isinstance(payload, dict) and ttl > 0:
            with _ttl_lock:
                _ttl_store[ck] = (time.monotonic() + float(ttl), copy.deepcopy(payload))
        return payload


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

    out: list[dict] = []
    for m in matches:
        home = (m.get("homeTeam") or {}).get("name", "")
        away = (m.get("awayTeam") or {}).get("name", "")
        utc = m.get("utcDate", "")
        hid = (m.get("homeTeam") or {}).get("id")
        aid = (m.get("awayTeam") or {}).get("id")

        st = (m.get("status") or "").strip().upper()
        minute = m.get("minute")
        row = {
            "match_id": m.get("id"),
            "utcDate": utc,
            "home": home,
            "away": away,
            "league": league_code,
            "home_team_id": hid,
            "away_team_id": aid,
            "home_crest": _crest_from_team_id(hid),
            "away_crest": _crest_from_team_id(aid),
        }
        if st:
            row["status"] = st
        if minute is not None:
            row["minute"] = minute
        out.append(row)

    return out[:60]


def _fd_match_to_aftr_row(m: dict, league_code: str) -> dict:
    """Normaliza un match crudo de Football-Data API v4 al shape AFTR (daily_matches)."""
    ft = ((m.get("score") or {}).get("fullTime")) or {}
    hg, ag = ft.get("home"), ft.get("away")
    hid = (m.get("homeTeam") or {}).get("id")
    aid = (m.get("awayTeam") or {}).get("id")
    st = (m.get("status") or "").strip().upper() or "IN_PLAY"
    minute = m.get("minute")
    row = {
        "match_id": m.get("id"),
        "utcDate": m.get("utcDate", ""),
        "home": (m.get("homeTeam") or {}).get("name", ""),
        "away": (m.get("awayTeam") or {}).get("name", ""),
        "league": league_code,
        "home_team_id": hid,
        "away_team_id": aid,
        "home_crest": _crest_from_team_id(hid),
        "away_crest": _crest_from_team_id(aid),
        "home_goals": hg,
        "away_goals": ag,
        "status": st,
    }
    if minute is not None:
        row["minute"] = minute
    return row


def get_live_matches(league_code: str) -> list[dict]:
    """
    Partidos en juego: una petición por liga con status=IN_PLAY (mínima huella de API).
    """
    comp = COMPETITIONS.get(league_code, "PL")
    out: list[dict] = []
    # Una sola llamada por liga (IN_PLAY); PAUSED omitido para ahorrar cuota.
    try:
        data = _get(
            f"/competitions/{comp}/matches",
            params={"status": "IN_PLAY"},
        )
    except UnsupportedCompetitionError:
        raise
    except Exception as e:
        logger.debug("get_live_matches %s: %s", league_code, e)
        return out
    for m in data.get("matches") or []:
        if not isinstance(m, dict):
            continue
        row = _fd_match_to_aftr_row(m, league_code)
        row["sport"] = "football"
        out.append(row)
    return out


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

    out: list[dict] = []
    for m in matches:
        home = (m.get("homeTeam") or {}).get("name", "")
        away = (m.get("awayTeam") or {}).get("name", "")
        utc = m.get("utcDate", "")

        ft = ((m.get("score") or {}).get("fullTime")) or {}
        hg = ft.get("home")
        ag = ft.get("away")

        hid = (m.get("homeTeam") or {}).get("id")
        aid = (m.get("awayTeam") or {}).get("id")

        if hg is None or ag is None:
            continue

        out.append({
            "match_id": m.get("id"),
            "utcDate": utc,
            "home": home,
            "away": away,
            "league": league_code,
            "home_team_id": hid,
            "away_team_id": aid,
            "home_crest": _crest_from_team_id(hid),
            "away_crest": _crest_from_team_id(aid),
            "home_goals": hg,
            "away_goals": ag,
        })

    return out


def get_standings(league_code: str) -> list[dict]:
    """
    Tabla de posiciones de la competición (TOTAL).
    Retorna lista ordenada por posición:
    {position, team_id, team_name, team_crest, played, won, draw, lost, gf, ga, gd, points}
    """
    comp = COMPETITIONS.get(league_code)
    if not comp:
        return []
    try:
        data = _get(f"/competitions/{comp}/standings")
    except UnsupportedCompetitionError:
        return []
    except Exception as e:
        logger.warning("get_standings %s: %s", league_code, e)
        return []

    for group in (data.get("standings") or []):
        if (group.get("type") or "").upper() == "TOTAL":
            out = []
            for row in (group.get("table") or []):
                team = row.get("team") or {}
                tid = team.get("id")
                out.append({
                    "position":  row.get("position"),
                    "team_id":   tid,
                    "team_name": team.get("shortName") or team.get("name") or "—",
                    "team_crest": team.get("crest") or _crest_from_team_id(tid),
                    "played":    row.get("playedGames", 0),
                    "won":       row.get("won", 0),
                    "draw":      row.get("draw", 0),
                    "lost":      row.get("lost", 0),
                    "gf":        row.get("goalsFor", 0),
                    "ga":        row.get("goalsAgainst", 0),
                    "gd":        row.get("goalDifference", 0),
                    "points":    row.get("points", 0),
                })
            return out
    return []
