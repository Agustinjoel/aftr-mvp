"""
One-off: query API-Sports Basketball leagues (NBA id 12) and games with different season values.
Run from project root with API_SPORTS_KEY set. Prints NBA league object and game counts per season.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

import requests

def main():
    key = os.getenv("API_SPORTS_KEY") or os.getenv("APISPORTS_KEY")
    if not key:
        print("Set API_SPORTS_KEY in .env")
        return
    BASE = "https://v1.basketball.api-sports.io"
    h = {"x-apisports-key": key}

    # 1) Leagues - find NBA (id 12)
    r = requests.get(f"{BASE}/leagues", headers=h, timeout=20)
    print("GET /leagues status:", r.status_code)
    data = r.json() if r.status_code == 200 else {}
    resp = data.get("response") or data.get("leagues") or []
    nba = [x for x in resp if isinstance(x, dict) and x.get("id") == 12]
    if not nba:
        nba = [x for x in resp if isinstance(x, dict) and "nba" in str(x.get("name", "")).lower()]
    print("\n--- NBA league entry (id=12) ---")
    print(json.dumps(nba[0] if nba else {}, indent=2))

    # 2) Games with different season formats for league 12
    print("\n--- GET /games league=12, various season values ---")
    for season in ["2025-2026", "2024-2025", "2025", "2024"]:
        r2 = requests.get(f"{BASE}/games", headers=h, params={"league": 12, "season": season}, timeout=20)
        d = r2.json() if r2.status_code == 200 else {}
        games = d.get("response") or d.get("games") or []
        print("  season=%s -> raw game count=%d" % (repr(season), len(games)))
    print("Done.")

if __name__ == "__main__":
    main()
