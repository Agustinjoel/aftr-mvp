import os
import requests
from dotenv import load_dotenv

load_dotenv()

BASE = "https://api.football-data.org/v4"
API_KEY = os.getenv("FOOTBALL_DATA_API_KEY")

HEADERS = {"X-Auth-Token": API_KEY} if API_KEY else {}

# IDs de competiciones en football-data.org (v4)
COMPETITIONS = {
    "PL": "PL",
    "PD": "PD",
    "SA": "SA",
    "BL1": "BL1",
    "FL1": "FL1",
    "CL": "CL",
}

def _get(path: str, params: dict | None = None) -> dict:
    if not API_KEY:
        raise RuntimeError("FOOTBALL_DATA_API_KEY no está seteada en .env")

    url = f"{BASE}{path}"
    r = requests.get(url, headers=HEADERS, params=params, timeout=20)

    # útil para debug
    if r.status_code != 200:
        raise RuntimeError(f"Football-Data error {r.status_code}: {r.text}")

    return r.json()

def get_upcoming_matches(league_code: str, days: int = 3) -> list[dict]:
    comp = COMPETITIONS.get(league_code, "PL")

    data = _get(f"/competitions/{comp}/matches", params={"status": "SCHEDULED"})
    matches = data.get("matches", [])

    out = []
    for m in matches:
        home = (m.get("homeTeam") or {}).get("name", "")
        away = (m.get("awayTeam") or {}).get("name", "")
        utc = m.get("utcDate", "")
        out.append({"utcDate": utc, "home": home, "away": away, "league": league_code})

    # devolvemos los primeros N (para no llenar de basura)
    return out[:60]