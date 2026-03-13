"""
One-off: query API-Sports Basketball /leagues and /games to find NBA league id and season.
Run from project root with API_SPORTS_KEY set.
"""
from __future__ import annotations

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import API_SPORTS_KEY

def main():
    if not API_SPORTS_KEY:
        print("Set API_SPORTS_KEY in .env")
        return
    import requests
    BASE = "https://v1.basketball.api-sports.io"
    h = {"x-apisports-key": API_SPORTS_KEY}

    # 1) GET /leagues
    r = requests.get(f"{BASE}/leagues", headers=h, timeout=20)
    print("GET /leagues status:", r.status_code)
    data = r.json() if r.status_code == 200 else {}
    response = data.get("response")
    if not isinstance(response, list):
        response = data.get("leagues") or []
    print("Raw leagues count:", len(response))

    # Find NBA
    nba = [x for x in response if isinstance(x, dict) and "nba" in str(x.get("name", "")).lower() or "nba" in str(x.get("league", "")).lower() or (x.get("id") == 12)]
    if not nba:
        # Show first 5 to see structure
        for i, L in enumerate(response[:8]):
            if isinstance(L, dict):
                print("  League sample:", L)
        # Try by id 12
        for L in response:
            if isinstance(L, dict) and (L.get("id") == 12 or L.get("league", {}).get("id") == 12):
                nba.append(L)
                break
    if nba:
        print("NBA-related league(s):", nba)
    else:
        print("No NBA found in response. Keys in first item:", list(response[0].keys()) if response and isinstance(response[0], dict) else "n/a")

    # 2) Try /games with different league/season
    for league_id in (12, 1, 2):
        for season in ("2024", "2025", "2024-2025"):
            r2 = requests.get(f"{BASE}/games", headers=h, params={"league": league_id, "season": season}, timeout=20)
            count = 0
            if r2.status_code == 200:
                d = r2.json()
                resp = d.get("response") or d.get("games") or []
                count = len(resp) if isinstance(resp, list) else 0
            if count > 0:
                print(f"  league={league_id} season={season} -> {count} games")
    print("Done.")

if __name__ == "__main__":
    main()
