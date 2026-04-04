"""
Evaluación de mercados de fútbol (goles). Devuelve (WIN | LOSS | PUSH, reason).
Basketball usa core.basketball_evaluation.
"""
from __future__ import annotations
import re


def evaluate_market(market: str, home_goals: int, away_goals: int) -> tuple[str, str]:
    """
    Evalúa el mercado de fútbol con el resultado (home_goals, away_goals).
    Returns: (result, reason) con result in WIN | LOSS | PUSH.
    PUSH solo se devuelve si el mercado es genuinamente irreconocible.
    """
    m = (market or "").strip()
    m_lower = m.lower()
    hg, ag = home_goals, away_goals
    total = hg + ag
    score = f"{hg}-{ag}"

    # --- 1X2 exactos ---
    if m_lower in ("home win", "1", "local"):
        return ("WIN", score) if hg > ag else ("LOSS", score)
    if m_lower in ("away win", "2", "visitante"):
        return ("WIN", score) if ag > hg else ("LOSS", score)
    if m_lower in ("draw", "x", "empate"):
        return ("WIN", score) if hg == ag else ("LOSS", score)
    if m_lower in ("1x", "home or draw", "local o empate"):
        return ("WIN", score) if hg >= ag else ("LOSS", score)
    if m_lower in ("x2", "draw or away", "empate o visitante"):
        return ("WIN", score) if ag >= hg else ("LOSS", score)
    if m_lower in ("12", "home or away"):
        return ("WIN", score) if hg != ag else ("LOSS", f"{score} (empate)")

    # --- Aliases con subcadenas ---
    if "home win" in m_lower or "home_win" in m_lower:
        return ("WIN", score) if hg > ag else ("LOSS", score)
    if "away win" in m_lower or "away_win" in m_lower:
        return ("WIN", score) if ag > hg else ("LOSS", score)
    if "draw" in m_lower or "empate" in m_lower:
        return ("WIN", score) if hg == ag else ("LOSS", score)
    if "1x" in m_lower:
        return ("WIN", score) if hg >= ag else ("LOSS", score)
    if "x2" in m_lower:
        return ("WIN", score) if ag >= hg else ("LOSS", score)

    # --- Over/Under genérico: Over X.Y / Under X.Y ---
    over_m = re.match(r'over\s+(\d+\.?\d*)', m_lower)
    if over_m:
        threshold = float(over_m.group(1))
        return (
            ("WIN", f"Total {total} > {threshold}") if total > threshold
            else ("LOSS", f"Total {total} <= {threshold}")
        )
    under_m = re.match(r'under\s+(\d+\.?\d*)', m_lower)
    if under_m:
        threshold = float(under_m.group(1))
        return (
            ("WIN", f"Total {total} < {threshold}") if total < threshold
            else ("LOSS", f"Total {total} >= {threshold}")
        )

    # --- BTTS / GG / NG ---
    if "btts yes" in m_lower or "ambos marcan" in m_lower or m_lower in ("gg", "btts"):
        return ("WIN", f"{hg}-{ag}") if (hg >= 1 and ag >= 1) else ("LOSS", f"{hg}-{ag}")
    if "btts no" in m_lower or m_lower == "ng":
        return ("WIN", f"{hg}-{ag}") if (hg == 0 or ag == 0) else ("LOSS", f"{hg}-{ag}")

    # --- Mercado no reconocido ---
    return ("PUSH", f"Market not supported: {m!r}")
