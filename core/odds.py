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

# Double-chance markets: implied prob = sum of component implied probs (from h2h)
_DOUBLE_CHANCE = {"1x", "x2", "12"}


def _get_h2h_raw_probs(by_market: dict) -> dict[str, float]:
    """Extract raw (pre-vig) h2h decimal odds as implied probs: {'Home Win': 0.4, ...}."""
    h2h = by_market.get("h2h") or {}
    out: dict[str, float] = {}
    for name, dec in h2h.items():
        try:
            p = 1.0 / float(dec)
            if 0 < p <= 1:
                out[name] = p
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    return out


def _implied_double_chance(by_market: dict, components: list[str]) -> tuple[float | None, float | None]:
    """
    For double-chance markets (1X, X2, 12), combine component implied probs from h2h.
    Returns (synthetic_decimal, implied_prob) or (None, None) if data missing.
    We use overround-adjusted (normalized) probabilities so the edge reflects true market view.
    """
    raw = _get_h2h_raw_probs(by_market)
    if not raw:
        return None, None
    total_raw = sum(raw.values())
    if total_raw <= 0:
        return None, None
    # Normalize to remove bookmaker overround
    norm: dict[str, float] = {k: v / total_raw for k, v in raw.items()}
    combined = sum(norm.get(c, 0.0) for c in components)
    if combined <= 0 or combined > 1:
        return None, None
    synthetic_decimal = round(1.0 / combined, 3)
    return synthetic_decimal, round(combined, 4)


def get_decimal_and_implied_for_market(odds_row: dict, market_name: str) -> tuple[float | None, float | None]:
    """
    From a normalized odds row (from odds_football), get decimal odds and implied prob for the given market.
    market_name: pick's best_market (e.g. "Home Win", "Over 2.5", "1X").
    For double-chance markets (1X, X2, 12) computes combined normalized implied probability from h2h.
    Returns (decimal_odds, implied_prob); either can be None if not found.
    """
    if not odds_row or not market_name:
        return None, None
    by_market = odds_row.get("odds_by_market") or {}
    mkt_lower = (market_name or "").strip().lower()

    # Double-chance: combine component probs from h2h (vig-adjusted)
    if mkt_lower in _DOUBLE_CHANCE:
        components = MARKET_TO_ODDS_KEY[mkt_lower]
        return _implied_double_chance(by_market, components)

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
