"""
Basketball market evaluation only. Uses points (home_goals/away_goals = home/away points).
Returns (WIN | LOSS | PUSH, reason). Used only for basketball/NBA flow.
"""
from __future__ import annotations

import re


def evaluate_basketball_market(market: str, home_pts: int, away_pts: int) -> tuple[str, str]:
    """
    Evaluate basketball market with final score (home_pts, away_pts).
    Markets: Moneyline Home/Away, Total Points Over/Under, Spread (placeholder).
    Returns: (result, reason) with result in WIN | LOSS | PUSH.
    """
    m = (market or "").strip()
    m_lower = m.lower()
    hg, ag = home_pts, away_pts
    total = hg + ag
    score = f"{hg}-{ag}"

    # Moneyline
    if m_lower in ("moneyline home", "moneyline_home", "ml home"):
        return ("WIN", score) if hg > ag else ("LOSS", score)
    if m_lower in ("moneyline away", "moneyline_away", "ml away"):
        return ("WIN", score) if ag > hg else ("LOSS", score)

    # Total Points Over/Under (e.g. "Over 220.5", "Under 220.5")
    over_match = re.match(r"over\s*(\d+(?:\.\d+)?)", m_lower)
    if over_match:
        try:
            line = float(over_match.group(1))
            return ("WIN", f"Total {total} (>{line})") if total > line else ("LOSS", f"Total {total} (<={line})")
        except ValueError:
            pass
    under_match = re.match(r"under\s*(\d+(?:\.\d+)?)", m_lower)
    if under_match:
        try:
            line = float(under_match.group(1))
            return ("WIN", f"Total {total} (<{line})") if total < line else ("LOSS", f"Total {total} (>={line})")
        except ValueError:
            pass

    # Spread (placeholder)
    if "spread" in m_lower:
        return ("PUSH", "Spread evaluation not yet implemented")

    return ("PUSH", "Market not supported")
