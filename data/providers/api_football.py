"""
API-Football (RapidAPI) provider — live fixtures y eventos en tiempo real.
Requiere env var API_FOOTBALL_KEY (X-RapidAPI-Key).
Si la key no está configurada, todas las funciones retornan vacío sin error.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

import requests

logger = logging.getLogger("aftr.api_football")

BASE_URL = "https://api-football-v3.p.rapidapi.com"
HEADERS_TEMPLATE = {
    "X-RapidAPI-Host": "api-football-v3.p.rapidapi.com",
}

# Timeout por request
_HTTP_TIMEOUT = 10

# Rate-limit mínimo entre llamadas (segundos) para no abusar del plan
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


def fetch_fixtures_by_date(date_str: str) -> list[dict]:
    """
    Retorna partidos de una fecha (YYYY-MM-DD).
    Útil para buscar fixture_id de un partido conocido por equipo+fecha.
    """
    items = _get("/fixtures", {"date": date_str})
    return [i for i in items if isinstance(i, dict)]
