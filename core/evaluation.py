"""
Evaluación de un pick según resultado del partido (goles).
Devuelve (WIN | LOSS | PUSH, reason). PUSH solo si el mercado no es soportado.
"""
from __future__ import annotations


def evaluate_market(market: str, home_goals: int, away_goals: int) -> tuple[str, str]:
    """
    Evalúa el mercado con el resultado (home_goals, away_goals).
    Returns: (result, reason) con result in WIN | LOSS | PUSH.
    """
    m = (market or "").strip()
    m_lower = m.lower()
    hg, ag = home_goals, away_goals
    score = f"{hg}-{ag}"

    # --- 1X2 y derivados (soporte explícito) ---
    if m_lower == "home win":
        return ("WIN", score) if hg > ag else ("LOSS", score)
    if m_lower == "away win":
        return ("WIN", score) if ag > hg else ("LOSS", score)
    if m_lower == "draw":
        return ("WIN", score) if hg == ag else ("LOSS", score)
    if m_lower == "1x":
        return ("WIN", score) if hg >= ag else ("LOSS", score)
    if m_lower == "x2":
        return ("WIN", score) if ag >= hg else ("LOSS", score)
    if m == "12":
        return ("WIN", score) if hg != ag else ("LOSS", f"{score} (empate)")

    # --- Aliases / variantes (compatibilidad) ---
    if "home win" in m_lower or "local" in m_lower:
        return ("WIN", score) if hg > ag else ("LOSS", score)
    if "away win" in m_lower or "visitante" in m_lower:
        return ("WIN", score) if ag > hg else ("LOSS", score)
    if "draw" in m_lower or "empate" in m_lower:
        return ("WIN", score) if hg == ag else ("LOSS", score)
    if "1x" in m_lower:
        return ("WIN", score) if hg >= ag else ("LOSS", score)
    if "x2" in m_lower:
        return ("WIN", score) if ag >= hg else ("LOSS", score)

    # --- Over/Under, BTTS ---
    total = hg + ag
    if "under 2.5" in m_lower:
        return ("WIN", f"Total {total} (<=2)") if total <= 2 else ("LOSS", f"Total {total} (>=3)")
    if "over 2.5" in m_lower:
        return ("WIN", f"Total {total} (>=3)") if total >= 3 else ("LOSS", f"Total {total} (<=2)")
    if "btts yes" in m_lower or "ambos marcan" in m_lower:
        return ("WIN", f"HG {hg} / AG {ag}") if (hg >= 1 and ag >= 1) else ("LOSS", f"HG {hg} / AG {ag}")
    if "btts no" in m_lower:
        return ("WIN", f"HG {hg} / AG {ag}") if (hg == 0 or ag == 0) else ("LOSS", f"HG {hg} / AG {ag}")

    return ("PUSH", "Market not supported")
