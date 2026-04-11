"""
Detección de estado de partido: live, finished, minuto en curso, formato de status.
Sin dependencias de FastAPI ni renderizado HTML.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.ui_helpers import _safe_int, _parse_utcdate_maybe
from app.ui_picks_calc import _result_norm

import logging
logger = logging.getLogger("app/ui_matches.py")



# =========================================================
# Constantes de estado live
# =========================================================

#: Statuses de proveedor que indican partido en curso (no finalizado).
MATCH_LIVE_STATUSES: frozenset[str] = frozenset(
    {
        "LIVE",
        "IN_PLAY",
        "INPLAY",
        "PLAYING",
        # Football halves
        "1H",
        "FIRST_HALF",
        "H1",
        "2H",
        "SECOND_HALF",
        "H2",
        "HT",
        "HALFTIME",
        "HALF_TIME",
        "BREAK",
        "PAUSED",
        "SUSPENDED",
        "ET",
        "EXTRA_TIME",
        "AET",
        "PENALTIES",
        "PENALTY_SHOOTOUT",
        "INT",
        "LIVE_1H",
        "LIVE_2H",
        # Basketball / API-Sports (short codes)
        "Q1",
        "Q2",
        "Q3",
        "Q4",
        "1Q",
        "2Q",
        "3Q",
        "4Q",
        "OT",
        "BT",
        "C1",
        "C2",
        "C3",
        "C4",
    }
)


# =========================================================
# Normalización de status
# =========================================================

def _match_live_status_token(match: dict) -> str:
    """
    Extrae el token de status normalizado (UPPER) desde formas planas o anidadas
    de distintos proveedores (Football-Data, API-Sports, etc.).
    """
    if not isinstance(match, dict):
        return ""
    for key in ("status", "match_status", "state"):
        v = match.get(key)
        if v is not None and str(v).strip():
            return str(v).strip().upper()
    fx = match.get("fixture")
    if isinstance(fx, dict):
        st = fx.get("status")
        if isinstance(st, dict):
            for sub in ("short", "long", "shortName", "type"):
                x = st.get(sub)
                if x is not None and str(x).strip():
                    return str(x).strip().upper()
        elif st is not None and str(st).strip():
            return str(st).strip().upper()
    return ""


# =========================================================
# Detección de estado
# =========================================================

def isMatchFinished(match: dict) -> bool:
    """
    Devuelve True si el partido finalizó. Prioridad:
    1. status explícito (FINISHED / FINAL / SETTLED)
    2. resultado WIN/LOSS/PUSH en `result`
    3. flag `finished`
    4. score presente + utcDate ya pasó (excepto si sigue vivo o TIMED/SCHEDULED)
    """
    if not isinstance(match, dict):
        return False

    status_raw = _match_live_status_token(match)

    if status_raw in {"FINISHED", "FINAL", "SETTLED", "FINALIZADO"}:
        return True
    if status_raw in {"WIN", "LOSS", "PUSH"}:
        return True

    # En curso → nunca tratar como finalizado solo por score+tiempo…
    # …pero sí si el kickoff ya pasó hace más de 3h30 (partido seguro terminado aunque el cache no se actualizó)
    if status_raw in MATCH_LIVE_STATUSES:
        dt = _parse_utcdate_maybe(match.get("utcDate"))
        if dt is not None and datetime.now(timezone.utc) > dt + timedelta(hours=2, minutes=15):
            return True
        return False

    raw_result = match.get("result")
    if raw_result is not None:
        norm = _result_norm({"result": raw_result})
        if norm in {"WIN", "LOSS", "PUSH"}:
            return True

    finished_flag_raw = match.get("finished")
    if isinstance(finished_flag_raw, bool):
        if finished_flag_raw:
            return True
    elif finished_flag_raw is not None:
        try:
            if str(finished_flag_raw).strip().lower() in {"1", "true", "yes", "y", "finished"}:
                return True
        except Exception as _silent_err:
            logger.debug("silenced exception (non-fatal): %s", _silent_err)

    # Heurística: score presente + kickoff pasó
    home_score = match.get("home_score")
    away_score = match.get("away_score")
    if home_score is None and away_score is None:
        home_score = (
            match.get("score_home") if match.get("score_home") is not None
            else match.get("homeScore")
        )
        away_score = (
            match.get("score_away") if match.get("score_away") is not None
            else match.get("awayScore")
        )
    if home_score is None or away_score is None:
        sc = match.get("score")
        if isinstance(sc, dict):
            hh = sc.get("home")
            aa = sc.get("away")
            if hh is not None and aa is not None:
                home_score = hh if home_score is None else home_score
                away_score = aa if away_score is None else away_score
            ft = sc.get("fullTime") or sc.get("full_time")
            if isinstance(ft, dict):
                fh = ft.get("home")
                fa = ft.get("away")
                if fh is not None and fa is not None:
                    home_score = fh if home_score is None else home_score
                    away_score = fa if away_score is None else away_score

    if home_score is not None and away_score is not None:
        dt = _parse_utcdate_maybe(match.get("utcDate"))
        if dt is not None and dt <= datetime.now(timezone.utc):
            if _safe_int(home_score) is not None and _safe_int(away_score) is not None:
                # TIMED/SCHEDULED + score + pasó el kickoff → probablemente LIVE con lag de proveedor
                if status_raw in {"TIMED", "SCHEDULED"}:
                    return False
                return True

    return False


def isMatchLive(match: dict) -> bool:
    """True si el partido está en curso (por status) y no fue marcado como finalizado."""
    if not isinstance(match, dict):
        return False
    if isMatchFinished(match):
        return False
    status_raw = _match_live_status_token(match)
    return status_raw in MATCH_LIVE_STATUSES


# =========================================================
# Minuto y formato de status
# =========================================================

def _live_minute_suffix(match: dict) -> str | None:
    """Devuelve el minuto de juego formateado (ej. \"67'\") o None si no está disponible."""
    if not isinstance(match, dict):
        return None
    for key in ("minute", "elapsed", "time_elapsed", "match_minute"):
        raw = match.get(key)
        if raw is None:
            continue
        try:
            mi = int(float(str(raw).replace("'", "").strip()))
            if mi >= 0:
                return f"{mi}'"
        except (TypeError, ValueError):
            continue
    return None


def _format_live_status_line(match: dict) -> str:
    """Status compacto para headers live (ej. 🔴 67', HT, ET 105')."""
    if not isinstance(match, dict):
        return "🔴 LIVE"
    st = _match_live_status_token(match)
    minute_s = _live_minute_suffix(match)

    if st in {"HT", "HALFTIME", "HALF_TIME", "BREAK"}:
        return "HT"
    if st in {"1H", "FIRST_HALF", "H1", "2H", "SECOND_HALF", "H2",
              "LIVE", "IN_PLAY", "INPLAY", "PLAYING", "LIVE_1H", "LIVE_2H"}:
        return f"🔴 {minute_s}" if minute_s else "🔴 LIVE"
    if st in {"ET", "EXTRA_TIME", "AET"}:
        return f"ET {minute_s}" if minute_s else "ET"
    if st in {"PENALTIES", "PENALTY_SHOOTOUT"}:
        return "Pen."
    if st in {"PAUSED", "SUSPENDED"}:
        return st.title()
    if st == "INT":
        return "Int."
    if minute_s:
        return f"🔴 {minute_s}"
    return "🔴 LIVE"
