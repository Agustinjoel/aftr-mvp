"""
Capa de datos para la UI: extracción de scores, carga de ligas y debug de partidos live.
Sin dependencias de FastAPI ni renderizado HTML.
"""
from __future__ import annotations

import logging
import os
import re
import time
import threading
from datetime import datetime, timezone, timedelta
from typing import Any

from config.settings import settings
from data.cache import read_json_with_fallback

from app.ui_helpers import _safe_int, _is_pick_valid, _parse_utcdate_maybe
from app.ui_picks_calc import _result_norm as _pick_result_norm
from app.ui_matches import (
    MATCH_LIVE_STATUSES,
    _match_live_status_token,
    isMatchFinished,
    isMatchLive,
)

logger = logging.getLogger("aftr.ui.data")


# =========================================================
# Extracción de score desde un partido
# =========================================================

def _extract_score_from_match(m: dict) -> tuple[int | None, int | None]:
    """
    Extrae (home_score, away_score) desde un dict de partido.
    Soporta múltiples formatos de proveedor: score str, score dict, score.fullTime, home_goals/away_goals.
    """
    if not isinstance(m, dict):
        return (None, None)

    def _parse_score_string(score_str: object) -> tuple[int | None, int | None]:
        if score_str is None:
            return (None, None)
        s = str(score_str).strip()
        if not s:
            return (None, None)
        # Acepta cualquier formato: "1-1", "1:1", "1–1", "Final 1-1"
        nums = re.findall(r"\d+", s)
        if len(nums) >= 2:
            return (_safe_int(nums[0]), _safe_int(nums[1]))
        return (None, None)

    sc = m.get("score")
    if isinstance(sc, str):
        return _parse_score_string(sc)
    if isinstance(sc, dict):
        h = sc.get("home")
        a = sc.get("away")
        if h is not None and a is not None:
            return (_safe_int(h), _safe_int(a))
        ft = sc.get("fullTime") or sc.get("full_time")
        if isinstance(ft, dict):
            h = ft.get("home")
            a = ft.get("away")
            if h is not None and a is not None:
                return (_safe_int(h), _safe_int(a))

    hg = m.get("home_goals")
    ag = m.get("away_goals")
    if hg is not None and ag is not None:
        hi, ai = _safe_int(hg), _safe_int(ag)
        if hi is not None and ai is not None:
            return (hi, ai)

    return (None, None)


# =========================================================
# Extracción de score desde un pick (con fallback al match)
# =========================================================

def _extract_score(
    p: dict,
    match_by_id: dict[int, dict] | None = None,
) -> tuple[int | None, int | None]:
    """
    Extrae (home_score, away_score) desde un pick. Intenta campos explícitos primero;
    si no encuentra, busca en match_by_id (keyed por int o por (league, int)).
    """
    if not isinstance(p, dict):
        return (None, None)

    def _parse_score_string(score_str: object) -> tuple[int | None, int | None]:
        if score_str is None:
            return (None, None)
        s = str(score_str).strip()
        if not s:
            return (None, None)
        nums = re.findall(r"\d+", s)
        if len(nums) >= 2:
            return (_safe_int(nums[0]), _safe_int(nums[1]))
        return (None, None)

    # Campos explícitos más comunes
    for hk, ak in [
        ("score_home",  "score_away"),
        ("home_score",  "away_score"),
        ("homeScore",   "awayScore"),
        ("h_home_score","h_away_score"),   # legacy
    ]:
        h = p.get(hk)
        a = p.get(ak)
        if h is not None and a is not None:
            return (_safe_int(h), _safe_int(a))

    sc = p.get("score")
    if isinstance(sc, str):
        return _parse_score_string(sc)
    if isinstance(sc, dict):
        hh = sc.get("home")
        aa = sc.get("away")
        if hh is not None and aa is not None:
            return (_safe_int(hh), _safe_int(aa))
        ft = sc.get("fullTime") or sc.get("full_time")
        if isinstance(ft, dict):
            hh = ft.get("home")
            aa = ft.get("away")
            if hh is not None and aa is not None:
                return (_safe_int(hh), _safe_int(aa))

    mid = _safe_int(p.get("match_id") or p.get("id"))
    if match_by_id and mid is not None:
        # Caso home: keyed por int
        if mid in match_by_id:
            return _extract_score_from_match(match_by_id[mid])
        # Caso dashboard: keyed por (league_code, int)
        league_code = (p.get("_league") or p.get("league") or "").strip()
        if league_code:
            for k in [
                (league_code, mid),
                (str(league_code), mid),
                (league_code, str(mid)),
                (str(league_code), str(mid)),
            ]:
                if k in match_by_id:
                    return _extract_score_from_match(match_by_id[k])

    return (None, None)


# =========================================================
# ID estable de pick (para favoritos)
# =========================================================

def _pick_id_for_card(p: dict, best: dict | None = None) -> str:
    """ID estable de un pick. Usa p.id si existe; sino compone liga|match_id|market|utcDate."""
    if not isinstance(p, dict):
        return ""
    pid = p.get("id") or p.get("pick_id")
    if pid is not None and str(pid).strip():
        return str(pid).strip()
    league   = (p.get("_league") or p.get("league") or "").strip()
    match_id = str(p.get("match_id") or p.get("id") or "")
    market   = (best or {}).get("market") or p.get("best_market") or ""
    utc      = str(p.get("utcDate") or "")
    return "|".join([league, match_id, market, utc]).strip("|") or "unknown"


# =========================================================
# Debug live (AFTR_LIVE_DEBUG=1)
# =========================================================

def _debug_log_live_match_candidates(league_code: str, matches: list[dict]) -> None:
    """
    Diagnóstico de candidatos live: kickoff pasado, no finalizado.
    Habilitá con: AFTR_LIVE_DEBUG=1
    """
    if os.getenv("AFTR_LIVE_DEBUG", "").strip().lower() not in ("1", "true", "yes", "on"):
        return
    now = datetime.now(timezone.utc)
    fin_like = frozenset(
        {"FINISHED", "FINAL", "FT", "AWARDED", "CANCELLED", "POSTPONED", "SETTLED", "FINALIZADO"},
    )
    for m in matches:
        if not isinstance(m, dict):
            continue
        st = _match_live_status_token(m)
        if st in fin_like:
            continue
        dt = _parse_utcdate_maybe(m.get("utcDate"))
        if dt is None or dt > now:
            continue
        raw_bundle: dict[str, Any] = {
            "status":       m.get("status"),
            "match_status": m.get("match_status"),
            "state":        m.get("state"),
            "minute":       m.get("minute"),
            "elapsed":      m.get("elapsed"),
            "time_elapsed": m.get("time_elapsed"),
            "match_minute": m.get("match_minute"),
            "live":         m.get("live"),
        }
        fx = m.get("fixture")
        if isinstance(fx, dict):
            raw_bundle["fixture.status"] = fx.get("status")
        logger.info(
            "AFTR_LIVE_DEBUG league=%s home=%s away=%s kickoff=%s status_fields=%s "
            "token=%s isMatchLive=%s isMatchFinished=%s",
            league_code,
            m.get("home"), m.get("away"), m.get("utcDate"),
            raw_bundle, st,
            isMatchLive(m), isMatchFinished(m),
        )


# =========================================================
# Carga de datos de todas las ligas
# =========================================================

_HOME_CACHE_TTL_SECONDS = 180  # 3 minutos
_home_cache: dict = {}
_home_cache_lock = threading.Lock()


def _load_all_leagues_data(
    league_codes: list[str] | None = None,
) -> tuple[
    list[dict],           # all_picks
    dict[Any, dict],      # match_by_key: (league, match_id) -> match
    list[dict],           # all_settled
    list[dict],           # all_upcoming
    dict[str, list[dict]],# picks_by_league
    dict[str, list[dict]],# matches_by_league
]:
    """
    Carga picks y partidos para todas (o las indicadas) las ligas. Devuelve:
    - all_picks: todos los picks con _league inyectado
    - match_by_key: (league, match_id) -> dict del partido
    - all_settled: picks con result WIN/LOSS/PUSH
    - all_upcoming: picks con result PENDING
    - picks_by_league: código → lista de picks
    - matches_by_league: código → lista de partidos
    """
    # --- TTL cache ---
    cache_key = tuple(sorted(league_codes)) if league_codes else "__all__"
    now_mono = time.monotonic()
    with _home_cache_lock:
        cached = _home_cache.get(cache_key)
        if cached is not None:
            ts, result = cached
            if now_mono - ts < _HOME_CACHE_TTL_SECONDS:
                logger.debug("_load_all_leagues_data: cache HIT key=%s", cache_key)
                return result
        logger.debug("_load_all_leagues_data: cache MISS key=%s", cache_key)

    codes = league_codes or list(settings.leagues.keys())
    all_picks:        list[dict]           = []
    match_by_key:     dict[Any, dict]      = {}
    picks_by_league:  dict[str, list[dict]] = {}
    matches_by_league: dict[str, list[dict]] = {}

    # Lazy-import DB helpers (best-effort; may not be available in tests)
    try:
        from app.db import get_published_picks_for_league as _db_get_picks
        _db_available = True
    except Exception:
        _db_available = False

    for code in codes:
        raw_matches = read_json_with_fallback(f"daily_matches_{code}.json") or []
        raw_picks   = read_json_with_fallback(f"daily_picks_{code}.json")   or []
        if not isinstance(raw_matches, list):
            raw_matches = []
        if not isinstance(raw_picks, list):
            raw_picks = []

        # Si el JSON de picks está vacío (reinicio de Render), cargar desde Postgres
        if not raw_picks and _db_available:
            try:
                db_picks = _db_get_picks(code)
                if db_picks:
                    raw_picks = db_picks
                    logger.info(
                        "load_all_leagues: %s — Cargando %d picks desde Postgres (JSON vacío)",
                        code, len(raw_picks),
                    )
            except Exception as _db_err:
                logger.debug("load_all_leagues: %s DB fallback error: %s", code, _db_err)

        matches = [m for m in raw_matches if isinstance(m, dict)]
        picks   = [p for p in raw_picks   if isinstance(p, dict)]

        logger.info("load_all_leagues: %s raw_matches=%s raw_picks=%s", code, len(matches), len(picks))

        for m in matches:
            mid = _safe_int(m.get("match_id") or m.get("id"))
            if mid is not None:
                match_by_key[(code, mid)] = m
        matches_by_league[code] = matches
        _debug_log_live_match_candidates(code, matches)

        filtered: list[dict] = []
        for p in picks:
            p = dict(p)
            p["_league"] = code
            if _is_pick_valid(p):
                filtered.append(p)

        if picks and not filtered:
            logger.warning(
                "load_all_leagues: %s had %s raw picks but 0 passed _is_pick_valid; usando fallback",
                code, len(picks),
            )
            for p in picks:
                p = dict(p)
                p["_league"] = code
                filtered.append(p)

        logger.info("load_all_leagues: %s after filter picks=%s", code, len(filtered))

        for p in filtered:
            all_picks.append(p)
            picks_by_league.setdefault(code, []).append(p)

    all_settled:  list[dict] = []
    all_upcoming: list[dict] = []
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")

    for p in all_picks:
        league_code = (p.get("_league") or p.get("league") or "").strip()
        mid         = _safe_int(p.get("match_id") or p.get("id"))
        match_obj   = match_by_key.get((league_code, mid)) if league_code and mid is not None else None

        pick_result = _pick_result_norm(p)

        if pick_result in ("WIN", "LOSS", "PUSH"):
            all_settled.append(p)
        elif pick_result == "PENDING":
            # Pick no resuelto: usar utcDate como referencia principal.
            # Solo marcar como settled si el match_obj confirma explícitamente FT,
            # o si el kickoff pasó hace más de 4h sin match_obj que diga lo contrario.
            match_explicitly_finished = isMatchFinished(match_obj) if isinstance(match_obj, dict) else False
            if match_explicitly_finished:
                all_settled.append(p)
            else:
                dt = _parse_utcdate_maybe(p.get("utcDate"))
                if dt is not None and dt < now_utc - timedelta(hours=4):
                    # Sin datos del partido y kickoff ya pasó hace >4h → asumir terminado
                    all_settled.append(p)
                else:
                    all_upcoming.append(p)
        else:
            # Fallback para resultados no reconocidos
            finished = isMatchFinished(p) or (isMatchFinished(match_obj) if isinstance(match_obj, dict) else False)
            (all_settled if finished else all_upcoming).append(p)

    # Log: qué partidos ve el motor para hoy
    today_ids = [
        p.get("match_id") or p.get("id")
        for p in all_upcoming
        if (p.get("utcDate") or "").startswith(today_str)
    ]
    logger.info(
        "Partidos encontrados para hoy (%s): %s",
        today_str,
        today_ids if today_ids else "(ninguno — picks futuros o sin fecha de hoy)",
    )
    logger.info(
        "load_all_leagues: total picks=%s settled=%s upcoming=%s leagues=%s",
        len(all_picks), len(all_settled), len(all_upcoming), list(picks_by_league.keys()),
    )

    result = all_picks, match_by_key, all_settled, all_upcoming, picks_by_league, matches_by_league
    with _home_cache_lock:
        _home_cache[cache_key] = (time.monotonic(), result)
    return result


_USER_COUNT_BASE = 50  # offset inicial para parecer orgánico desde el día 1

def get_display_user_count() -> str:
    """Retorna el conteo de usuarios registrados + base fija, como string formateado."""
    try:
        from app.db import get_conn, put_conn
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) AS n FROM users")
            row = cur.fetchone()
            real = int(row["n"]) if row else 0
        finally:
            put_conn(conn)
        total = real + _USER_COUNT_BASE
        return f"{total:,}".replace(",", ".")
    except Exception:
        return f"{_USER_COUNT_BASE}"
