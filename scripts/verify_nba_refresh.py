"""
One-off verification: run refresh for NBA only and report API request, counts, and output files.
Do not modify football code or redesign anything.
"""
from __future__ import annotations

import os
import sys

# Ensure project root on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta, timezone

from config.settings import API_SPORTS_KEY, CACHE_DIR

def main() -> None:
    print("=" * 60)
    print("NBA REFRESH VERIFICATION")
    print("=" * 60)

    # Exact API-Sports Basketball request (same as provider) - season format YYYY-YYYY
    now_utc = datetime.now(timezone.utc)
    start_year = now_utc.year if now_utc.month >= 10 else now_utc.year - 1
    season = f"{start_year}-{start_year + 1}"
    print("\n1) EXACT API-SPORTS BASKETBALL REQUEST (used by provider)")
    print(f"   GET https://v1.basketball.api-sports.io/games")
    print(f"   Params: league=12, season={season}")
    print(f"   Header: x-apisports-key: <API_SPORTS_KEY>")

    if not API_SPORTS_KEY:
        print("\n   API_SPORTS_KEY (or APISPORTS_KEY) not set. Set it in .env to run live API and refresh.")
        print("   Cache files would be written to:", CACHE_DIR)
        return

    # 1) Execute request and get raw count
    import requests
    BASE = "https://v1.basketball.api-sports.io"
    league_id = 12  # NBA
    params = {"league": league_id, "season": season}
    url = f"{BASE}/games"
    headers = {"x-apisports-key": API_SPORTS_KEY}
    r = requests.get(url, headers=headers, params=params, timeout=20)
    print(f"   HTTP Status: {r.status_code}")

    raw_data = r.json() if r.status_code == 200 else {}
    response = raw_data.get("response")
    if not isinstance(response, list):
        response = raw_data.get("games") or []
    raw_count = len(response)
    print(f"\n   Raw games count returned: {raw_count}")

    # 2) Normalized counts from provider
    from data.providers.api_sports_basketball import get_upcoming_games, get_finished_games
    upcoming = get_upcoming_games("NBA", days=7)
    finished = get_finished_games("NBA", days_back=7)
    print("\n2) NORMALIZED GAMES (provider)")
    print(f"   Upcoming (kept): {len(upcoming)}")
    print(f"   Finished (kept): {len(finished)}")

    # 3) Run refresh for NBA only
    print("\n3) RUNNING refresh_league('NBA')")
    from services.refresh import refresh_league
    n_matches, n_picks = refresh_league("NBA")
    print(f"   refresh_league returned: upcoming_matches={n_matches}, picks_daily={n_picks}")

    # 4) Check output files
    matches_file = CACHE_DIR / "daily_matches_NBA.json"
    picks_file = CACHE_DIR / "daily_picks_NBA.json"
    print("\n4) OUTPUT FILES")
    print(f"   daily_matches_NBA.json: {'EXISTS' if matches_file.exists() else 'MISSING'} ({matches_file})")
    if matches_file.exists():
        from data.cache import read_json
        data = read_json("daily_matches_NBA.json")
        count = len(data) if isinstance(data, list) else 0
        print(f"   -> entries: {count}")
        if count and isinstance(data, list) and data[0]:
            print(f"   -> first entry keys: {list(data[0].keys())[:12]}...")
            print(f"   -> sport on first: {data[0].get('sport')}")
    print(f"   daily_picks_NBA.json: {'EXISTS' if picks_file.exists() else 'MISSING'} ({picks_file})")
    if picks_file.exists():
        from data.cache import read_json
        data = read_json("daily_picks_NBA.json")
        count = len(data) if isinstance(data, list) else 0
        print(f"   -> entries: {count}")
    print("\n" + "=" * 60)

if __name__ == "__main__":
    main()
