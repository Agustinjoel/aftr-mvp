"""
Odds utilities: implied probability from decimal odds, edge (AFTR prob - implied prob).
Used by football pipeline; extensible for NBA later.
"""
from __future__ import annotations


def implied_probability(decimal_odds: float) -> float | None:
    """
    Implied probability from decimal odds: 1 / decimal_odds.
    Returns None if odds <= 0 or invalid.
    """
    if decimal_odds is None or decimal_odds <= 0:
        return None
    try:
        p = 1.0 / float(decimal_odds)
        return round(p, 4) if 0 <= p <= 1 else None
    except (TypeError, ValueError):
        return None


def edge(aftr_prob: float | None, implied_prob: float | None) -> float | None:
    """
    Edge = AFTR probability - implied probability.
    Positive = model sees value vs market. Returns None if either input is None.
    """
    if aftr_prob is None or implied_prob is None:
        return None
    try:
        return round(float(aftr_prob) - float(implied_prob), 4)
    except (TypeError, ValueError):
        return None


# AFTR market name (from picks) -> possible keys in odds_by_market / outcome names
MARKET_TO_ODDS_KEY: dict[str, list[str]] = {
    "home win": ["Home Win"],
    "away win": ["Away Win"],
    "draw": ["Draw"],
    "over 2.5": ["Over 2.5"],
    "under 2.5": ["Under 2.5"],
    "over 1.5": ["Over 1.5"],
    "under 1.5": ["Under 1.5"],
    "btts yes": ["BTTS Yes"],
    "btts no": ["BTTS No"],
    "1x": ["Home Win", "Draw"],
    "x2": ["Away Win", "Draw"],
    "12": ["Home Win", "Away Win"],
}


def get_decimal_and_implied_for_market(odds_row: dict, market_name: str) -> tuple[float | None, float | None]:
    """
    From a normalized odds row (from odds_football), get decimal odds and implied prob for the given market.
    market_name: pick's best_market (e.g. "Home Win", "Over 2.5", "DRAW").
    Returns (decimal_odds, implied_prob); either can be None if not found.
    """
    if not odds_row or not market_name:
        return None, None
    by_market = odds_row.get("odds_by_market") or {}
    mkt_lower = (market_name or "").strip().lower()
    candidates = MARKET_TO_ODDS_KEY.get(mkt_lower)
    if not candidates:
        candidates = [market_name.strip(), market_name.strip().title()]
    decimal = None
    for source in ["h2h", "totals_25", "totals_15"]:
        outcomes = by_market.get(source) or {}
        for out_name, dec_val in outcomes.items():
            if out_name in candidates or (out_name or "").strip().lower() == mkt_lower:
                try:
                    decimal = float(dec_val)
                    break
                except (TypeError, ValueError):
                    pass
        if decimal is not None:
            break
    if decimal is None:
        for outcomes in by_market.values():
            if not isinstance(outcomes, dict):
                continue
            for out_name, dec_val in outcomes.items():
                if (out_name or "").strip().lower() == mkt_lower:
                    try:
                        decimal = float(dec_val)
                        break
                    except (TypeError, ValueError):
                        pass
            if decimal is not None:
                break
    if decimal is None:
        return None, None
    impl = implied_probability(decimal)
    return decimal, impl
