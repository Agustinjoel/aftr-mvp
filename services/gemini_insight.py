"""
Genera análisis táctico breve para un pick via Gemini API.
Se llama durante el refresh (best-effort, no falla si Gemini no responde).
"""
from __future__ import annotations
import logging

logger = logging.getLogger("aftr.gemini_insight")


def get_match_insight(pick: dict) -> str | None:
    """Devuelve 2-3 oraciones de análisis táctico o None si falla."""
    from config.settings import settings
    api_key = getattr(settings, "GEMINI_API_KEY", "") or ""
    if not api_key:
        return None

    home  = pick.get("home") or "Local"
    away  = pick.get("away") or "Visitante"
    mkt   = pick.get("best_market") or "—"
    prob  = pick.get("best_prob")
    xg_h  = pick.get("xg_home")
    xg_a  = pick.get("xg_away")
    tier  = pick.get("tier") or ""

    try:
        prob_str = f"{float(prob)*100:.1f}%" if prob is not None else "—"
        xg_line  = (
            f"xG estimados: {home} {float(xg_h):.2f} — {float(xg_a):.2f} {away}."
            if xg_h is not None and xg_a is not None else ""
        )
    except (TypeError, ValueError):
        prob_str = "—"
        xg_line  = ""

    prompt = (
        f"Partido de fútbol: {home} vs {away}.\n"
        f"Predicción AFTR: {mkt} con {prob_str} de probabilidad (tier: {tier}).\n"
        f"{xg_line}\n"
        f"En exactamente 2 oraciones cortas en español, justificá tácticamente "
        f"esta predicción. Sé directo, analítico y evitá clichés. "
        f"No menciones jugadores específicos."
    )

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.0-flash:generateContent?key={api_key}"
    )

    try:
        import httpx
        resp = httpx.post(
            url,
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=10,
        )
        if resp.status_code == 200:
            data  = resp.json()
            parts = ((data.get("candidates") or [{}])[0]
                     .get("content", {}).get("parts") or [])
            text  = "".join(p.get("text", "") for p in parts).strip()
            return text[:500] if text else None
        logger.debug("gemini_insight HTTP %s", resp.status_code)
    except Exception as exc:
        logger.debug("gemini_insight error: %s", exc)
    return None
