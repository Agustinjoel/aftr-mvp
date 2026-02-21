from data.providers.football_data import get_upcoming_matches
from data.cache import write_json
from pathlib import Path
import sys

# Permite ejecutar `python daily/refresh.py` desde la raíz del proyecto
# en Windows/PowerShell sin romper imports absolutos.
if __package__ in (None, ""):
    sys.path.append(str(Path(__file__).resolve().parents[1]))

from data.providers.football_data import get_team_crest, get_upcoming_matches
from data.cache import read_json, write_json
from models.enums import LEAGUES

TEAM_CRESTS_FILE = "team_crests.json"


def _load_team_crests_cache() -> dict[str, str]:
    raw = read_json(TEAM_CRESTS_FILE)
    return raw if isinstance(raw, dict) else {}


def _cache_key(team_name: str, team_id: int | None = None) -> str:
    if team_id:
        return f"id:{team_id}"
    return f"name:{(team_name or '').strip().lower()}"


def _fill_missing_crests(matches: list[dict], crest_cache: dict[str, str]) -> list[dict]:
    # First pass: keep cache warm with any crest already present in payload.
    for m in matches:
        for side in ("home", "away"):
            team_name = m.get(side, "")
            team_id = m.get(f"{side}_team_id")
            crest = m.get(f"{side}_crest")
            if crest:
                crest_cache[_cache_key(team_name, team_id)] = crest
                crest_cache[_cache_key(team_name)] = crest

    # Second pass: fill missing from cache or provider details endpoint.
    for m in matches:
        for side in ("home", "away"):
            crest_key = f"{side}_crest"
            if m.get(crest_key):
                continue

            team_name = m.get(side, "")
            team_id = m.get(f"{side}_team_id")

            crest = None
            if team_id:
                crest = crest_cache.get(_cache_key(team_name, team_id))
            if not crest:
                crest = crest_cache.get(_cache_key(team_name))

            if not crest and team_id:
                crest = get_team_crest(team_id)

            if crest:
                m[crest_key] = crest
                if team_id:
                    crest_cache[_cache_key(team_name, team_id)] = crest
                crest_cache[_cache_key(team_name)] = crest

    return matches


def make_basic_picks(matches: list[dict]) -> list[dict]:
    # Picks “placeholders” para poblar la UI (después metemos Poisson)
    picks = []
    for m in matches:
        picks.append({
            "utcDate": m["utcDate"],
            "home": m["home"],
            "away": m["away"],
            "candidates": [
                {"market": "Over 1.5", "prob": 0.62},
                {"market": "BTTS Yes", "prob": 0.53},
            ]
        })
        picks.append(
            {
                "utcDate": m["utcDate"],
                "home": m["home"],
                "away": m["away"],
                "home_crest": m.get("home_crest"),
                "away_crest": m.get("away_crest"),
                "candidates": [
                    {"market": "Over 1.5", "prob": 0.62},
                    {"market": "BTTS Yes", "prob": 0.53},
                ],
            }
        )
    return picks


def refresh_all():
    crest_cache = _load_team_crests_cache()

    for code in LEAGUES.keys():
        matches = get_upcoming_matches(code)
        matches = _fill_missing_crests(matches, crest_cache)
        write_json(f"daily_matches_{code}.json", matches)
        write_json(f"daily_picks_{code}.json", make_basic_picks(matches))
        print(f"OK {code}: {len(matches)} matches")

    write_json(TEAM_CRESTS_FILE, crest_cache)


if __name__ == "__main__":
    refresh_all()
    refresh_all()