from __future__ import annotations

import sys
from pathlib import Path

# ✅ permite ejecutar: python .\scripts\backfill_scores.py desde root en Windows
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.cache import read_json, write_json
from data.providers.football_data import get_finished_matches


def main(league: str = "PL", days_back: int = 10) -> None:
    picks_file = f"daily_picks_{league}.json"

    picks = read_json(picks_file) or []
    if not isinstance(picks, list):
        print("picks no es lista")
        return

    finished = get_finished_matches(league, days_back=days_back) or []
    lookup: dict[int, tuple[int, int]] = {}
    for m in finished:
        mid = m.get("match_id")
        hg = m.get("home_goals")
        ag = m.get("away_goals")
        if mid is None or hg is None or ag is None:
            continue
        try:
            lookup[int(mid)] = (int(hg), int(ag))
        except Exception:
            pass

    updated = 0
    for p in picks:
        if not isinstance(p, dict):
            continue
        mid = p.get("match_id")
        if mid is None:
            continue
        try:
            mid_i = int(mid)
        except Exception:
            continue
        if mid_i not in lookup:
            continue

        hg, ag = lookup[mid_i]
        p["score_home"] = hg
        p["score_away"] = ag
        updated += 1

    write_json(picks_file, picks)
    print(f"OK {league}: scores agregados a {updated} picks")


if __name__ == "__main__":
    main()