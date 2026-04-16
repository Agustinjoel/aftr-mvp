"""
Endpoint de chat AFF — asistente de IA de AFTR usando Gemini Flash.
"""
from __future__ import annotations
import glob
import logging
import os

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from config.settings import settings
from data.cache import CACHE_DIR
from services.refresh_utils import _read_json_list

logger = logging.getLogger("aftr.chat")
router = APIRouter()


def _build_picks_context(league: str | None = None) -> str:
    """Construye contexto de picks del día para el system prompt."""
    lines = []

    pattern = os.path.join(str(CACHE_DIR), "daily_picks_*.json")
    files = glob.glob(pattern)

    for f in files[:5]:  # max 5 ligas
        picks = _read_json_list(os.path.basename(f))
        for p in picks[:10]:  # max 10 picks por liga
            home = p.get("home", "?")
            away = p.get("away", "?")
            market = p.get("best_market", "?")
            prob = p.get("best_prob")
            conf = p.get("confidence", "?")
            result = p.get("result", "PENDING")
            prob_str = f"{prob:.0%}" if prob else "?"
            lines.append(
                f"- {home} vs {away}: {market} ({prob_str} prob, confianza {conf}/10) — {result}"
            )

    return "\n".join(lines) if lines else "No hay picks disponibles en este momento."


SYSTEM_PROMPT = """Sos AFF, el asistente de inteligencia artificial de AFTR, una app de picks deportivos basada en modelos matemáticos (Poisson, xG).

Tu rol:
- Explicar los picks del día y qué significan
- Explicar conceptos: Over/Under, BTTS, 1X2, xG, confianza, AFTR score, mercados de apuestas
- Dar sugerencias basadas en los datos reales de los picks
- Responder preguntas sobre cómo funciona la app

Reglas:
- Solo hablás de AFTR, fútbol y apuestas deportivas
- Respondés siempre en el mismo idioma que el usuario (español o inglés)
- Sos conciso, directo y útil
- No inventás datos que no están en el contexto

Picks disponibles hoy:
{picks_context}"""


@router.post("/api/chat")
async def chat(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"reply": "No entendí el mensaje."}, status_code=400)

    user_message = (body.get("message") or "").strip()
    league = body.get("league")

    if not user_message:
        return JSONResponse({"reply": "Escribí algo para empezar."})

    api_key = getattr(settings, "GEMINI_API_KEY", "") or ""
    if not api_key:
        return JSONResponse({"reply": "AFF no está configurado todavía."})

    picks_context = _build_picks_context(league)
    system = SYSTEM_PROMPT.format(picks_context=picks_context)

    # Inyectar system prompt como primer turn user/model — funciona en cualquier
    # versión de la API sin depender de systemInstruction ni system_instruction.
    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": system}]},
            {"role": "model", "parts": [{"text": "Entendido. Soy AFF, listo para ayudarte."}]},
            {"role": "user", "parts": [{"text": user_message}]},
        ],
    }

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-1.5-flash:generateContent?key={api_key}"
    )

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 429:
                logger.warning("Gemini 429 (quota): %s", resp.text[:300])
                reply = "AFF está muy ocupado en este momento. Intentá en unos minutos."
            elif resp.status_code != 200:
                logger.warning("Gemini error %s: %s", resp.status_code, resp.text[:300])
                reply = "Hubo un error al consultar AFF. Intentá de nuevo."
            else:
                data = resp.json()
                reply = data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        logger.warning("Gemini error: %s", e)
        reply = "Hubo un error al consultar AFF. Intentá de nuevo."

    return JSONResponse({"reply": reply})
