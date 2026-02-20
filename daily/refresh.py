from data.providers.football_data import get_upcoming_matches
from data.cache import write_json
from models.enums import LEAGUES

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
    return picks

def refresh_all():
    for code in LEAGUES.keys():
        matches = get_upcoming_matches(code)
        write_json(f"daily_matches_{code}.json", matches)
        write_json(f"daily_picks_{code}.json", make_basic_picks(matches))
        print(f"OK {code}: {len(matches)} matches")

if __name__ == "__main__":
    refresh_all()