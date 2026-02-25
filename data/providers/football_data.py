"""
Cliente de la API Football-Data.org v4.
Usa config.settings para API key.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import time
import logging

import requests

from config.settings import FOOTBALL_DATA_API_KEY

logger = logging.getLogger(__name__)

BASE = "https://api.football-data.org/v4"
COMPETITIONS = {
    "BSA": "BSA",  # Brasileirao
    "ELC": "ELC",  # Championship
    "PL": "PL",
    "CL": "CL",
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

# ✅ DEV/PROD switch:
#   AFTR_SLEEP_ON_429=0 -> no duerme, tira error rápido (ideal dev)
#   AFTR_SLEEP_ON_429=1 -> duerme y reintenta (ideal prod)
SLEEP_ON_429 = os.getenv("AFTR_SLEEP_ON_429", "1").strip().lower() in ("1", "true", "yes")


def _headers() -> dict[str, str]:
    if not FOOTBALL_DATA_API_KEY:
        raise RuntimeError("FOOTBALL_DATA_API_KEY no está configurada (config o .env)")
    return {"X-Auth-Token": FOOTBALL_DATA_API_KEY}


def _get(path: str, params: dict | None = None) -> dict:
    url = f"{BASE}{path}"
    attempt = 0
    while True:
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
                    time.sleep(wait)
            except ValueError:
                pass

        if r.status_code != 200:
            raise RuntimeError(f"Football-Data Error {r.status_code}: {r.text}")

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

    out: list[dict] = []
    for m in matches:
        home = (m.get("homeTeam") or {}).get("name", "")
        away = (m.get("awayTeam") or {}).get("name", "")
        utc = m.get("utcDate", "")
        hid = (m.get("homeTeam") or {}).get("id")
        aid = (m.get("awayTeam") or {}).get("id")

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
