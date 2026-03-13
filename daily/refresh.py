from data.providers.football_data import get_upcoming_matches
from data.cache import write_json
from models.enums import LEAGUES

from core.combos import build_global_combos


def make_basic_picks(matches: list[dict]) -> list[dict]:
    picks = []
    for m in matches:
        mid = m.get("match_id")
        if mid is None:
            mid = m.get("id")

        picks.append({
            "match_id": mid,
            "utcDate": m.get("utcDate"),
            "home": m.get("home"),
            "away": m.get("away"),
            "home_crest": m.get("home_crest"),
            "away_crest": m.get("away_crest"),
            "candidates": [
                {"market": "Over 1.5", "prob": 0.62},
                {"market": "BTTS Yes", "prob": 0.53},
            ]
        })
    return picks


def refresh_all():
    picks_by_league = {}

    for code in LEAGUES.keys():
        matches = get_upcoming_matches(code)
        write_json(f"daily_matches_{code}.json", matches)

        picks = make_basic_picks(matches)
        write_json(f"daily_picks_{code}.json", picks)

        picks_by_league[code] = picks
        print(f"OK {code}: {len(matches)} matches")

    combos = build_global_combos(picks_by_league)
    write_json("daily_combos.json", combos)
    print("OK combos: daily_combos.json")


if __name__ == "__main__":
    refresh_all()