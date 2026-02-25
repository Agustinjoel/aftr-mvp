from __future__ import annotations

from config.settings import settings
from core.poisson import match_probs, build_candidates
from core.model_b import estimate_xg_dynamic_split
from data.cache import read_json
from data.providers.team_form import get_team_recent_matches


def best_pick_from_probs(probs: dict) -> tuple[str, float]:
    cands = build_candidates(probs, min_prob=settings.min_prob_for_candidate)
    if not cands:
        return ("—", 0.0)
    best = cands[0]
    return (best["market"], float(best["prob"]))


def main():
    league = "PL"
    matches = read_json(f"daily_matches_{league}.json") or []
    matches = matches[:5]  # solo 5 para no quemar API

    print(f"Comparando A vs B (SPLIT) para {league} (muestras={len(matches)})")
    print("-" * 80)

    for m in matches:
        home = m.get("home", "")
        away = m.get("away", "")
        hid = m.get("home_team_id")
        aid = m.get("away_team_id")

        # Modelo A (xG fijo)
        xgA_h, xgA_a = settings.default_xg_home, settings.default_xg_away
        probsA = match_probs(xgA_h, xgA_a)
        mkA, pA = best_pick_from_probs(probsA)

        # Modelo B SPLIT (home/local vs away/visitante)
        xgB_h, xgB_a = xgA_h, xgA_a
        if hid and aid:
            hm = get_team_recent_matches(int(hid), days_back=30, limit=10)
            am = get_team_recent_matches(int(aid), days_back=30, limit=10)

            xgB_h, xgB_a = estimate_xg_dynamic_split(
                int(hid),
                int(aid),
                hm,
                am,
            )

        probsB = match_probs(xgB_h, xgB_a)
        mkB, pB = best_pick_from_probs(probsB)

        print(f"{home} vs {away}")
        print(f"  xG A: {xgA_h:.2f}/{xgA_a:.2f}  -> {mkA} ({pA*100:.1f}%)")
        print(f"  xG B: {xgB_h:.2f}/{xgB_a:.2f}  -> {mkB} ({pB*100:.1f}%)")
        print("-" * 80)


if __name__ == "__main__":
    main()