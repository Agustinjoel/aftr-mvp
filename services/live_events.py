"""
AFTR Live Events — detecta goles y resultados finales en tiempo real
usando API-Football (RapidAPI) y envía push notifications a usuarios
que tienen tracker bets en esos partidos.

Corre al final de cada live refresh job.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("aftr.live_events")

# Archivo de estado: {fixture_id_str: {home, away, score_h, score_a, status, notified_goals}}
_STATE_FILE = "live_events_state.json"

# Ventana: solo bets con kickoff en las últimas N horas
_KICKOFF_WINDOW_H = 4


def _load_state() -> dict:
    from data.cache import read_json
    raw = read_json(_STATE_FILE)
    return raw if isinstance(raw, dict) else {}


def _save_state(state: dict) -> None:
    from data.cache import write_json
    write_json(_STATE_FILE, state)


def _normalize(name: str) -> str:
    return (name or "").lower().strip()


def _teams_match(fix_home: str, fix_away: str, leg_home: str, leg_away: str) -> bool:
    fh, fa = _normalize(fix_home), _normalize(fix_away)
    lh, la = _normalize(leg_home), _normalize(leg_away)
    if not fh or not fa or not lh or not la:
        return False
    if fh == lh and fa == la:
        return True
    if (lh in fh or fh in lh) and (la in fa or fa in la):
        return True
    return False


def _load_pending_legs() -> list[dict]:
    """Carga bet_legs PENDING con kickoff en ventana de las últimas _KICKOFF_WINDOW_H horas."""
    from app.db import get_conn, put_conn
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=_KICKOFF_WINDOW_H)

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT bl.id AS leg_id, bl.bet_id, bl.home_team, bl.away_team,
                      bl.market, bl.kickoff_time, ub.user_id
               FROM bet_legs bl
               JOIN user_bets ub ON bl.bet_id = ub.id
               WHERE bl.status = 'PENDING'
                 AND bl.kickoff_time IS NOT NULL
                 AND bl.kickoff_time BETWEEN %s AND %s""",
            (window_start, now + timedelta(minutes=10)),
        )
        return list(cur.fetchall())
    finally:
        put_conn(conn)


def _resolve_market_for_push(market: str, home_goals: int, away_goals: int) -> str | None:
    """Retorna 'WON' / 'LOST' si ya se puede resolver el mercado, None si aún no."""
    market = (market or "").lower().strip()
    total = home_goals + away_goals

    # Mercados que solo se resuelven al final del partido
    if market in ("1", "x", "2", "1x", "x2", "12", "btts_yes", "btts_no", "dnb_1", "dnb_2"):
        return None  # Necesitamos resultado final

    # Over: se puede saber si ya se llegó al total
    if market.startswith("over_"):
        try:
            line = float(market[5:])
            if total > line:
                return "WON"
        except ValueError:
            pass
        return None

    # Under: se puede saber si es imposible que gane (demasiados goles)
    if market.startswith("under_"):
        try:
            line = float(market[6:])
            if total >= line:
                return "LOST"  # Ya hay suficientes goles → imposible ganar
        except ValueError:
            pass
        return None

    return None


def _final_result_emoji(status: str) -> str:
    return "✅" if status == "WON" else ("❌" if status == "LOST" else "")


def process_live_events() -> int:
    """
    Busca partidos en vivo via API-Football, detecta cambios de score,
    y envía pushes a usuarios con tracker bets en esos partidos.
    Retorna cantidad de notificaciones enviadas.
    """
    from data.providers.api_football import fetch_live_fixtures, _api_key
    from services.push_notifications import send_to_user

    if not _api_key():
        return 0

    live_fixtures = fetch_live_fixtures()
    if not live_fixtures:
        return 0

    pending_legs = _load_pending_legs()
    if not pending_legs:
        # Limpiamos estado de fixtures viejos pero no hacemos nada más
        return 0

    state = _load_state()
    state_changed = False
    notifications_sent = 0

    for fixture in live_fixtures:
        if not isinstance(fixture, dict):
            continue

        fix_info = fixture.get("fixture") or {}
        fix_id = str(fix_info.get("id") or "")
        if not fix_id:
            continue

        fix_status = (fix_info.get("status") or {}).get("short", "")
        fix_elapsed = (fix_info.get("status") or {}).get("elapsed") or 0

        teams = fixture.get("teams") or {}
        home_name = (teams.get("home") or {}).get("name", "")
        away_name = (teams.get("away") or {}).get("name", "")

        goals = fixture.get("goals") or {}
        score_h = goals.get("home")
        score_a = goals.get("away")
        if score_h is None or score_a is None:
            continue
        try:
            score_h, score_a = int(score_h), int(score_a)
        except (TypeError, ValueError):
            continue

        # Buscar legs que correspondan a este fixture
        matched_legs = [
            leg for leg in pending_legs
            if _teams_match(home_name, away_name, leg["home_team"], leg["away_team"])
        ]
        if not matched_legs:
            continue

        prev = state.get(fix_id) or {}
        prev_h = prev.get("score_h", -1)
        prev_a = prev.get("score_a", -1)
        is_final = fix_status in ("FT", "AET", "PEN", "FINISHED")
        goal_scored = (score_h != prev_h or score_a != prev_a) and (prev_h >= 0 or prev_a >= 0)
        first_time_seen = prev_h < 0 and prev_a < 0

        # Update state
        state[fix_id] = {
            "home": home_name,
            "away": away_name,
            "score_h": score_h,
            "score_a": score_a,
            "status": fix_status,
        }
        state_changed = True

        # Si es la primera vez que vemos el fixture, no notificamos (sin baseline)
        if first_time_seen and not is_final:
            continue

        # Agrupar legs por usuario
        legs_by_user: dict[int, list[dict]] = {}
        for leg in matched_legs:
            uid = leg["user_id"]
            legs_by_user.setdefault(uid, []).append(leg)

        if is_final and not prev.get("notified_final"):
            # Resultado final — notificar a todos los dueños de bets
            state[fix_id]["notified_final"] = True
            for uid, legs in legs_by_user.items():
                markets = ", ".join(leg["market"] for leg in legs if leg.get("market"))
                payload = {
                    "title": f"FT: {home_name} {score_h}–{score_a} {away_name}",
                    "body": f"Tu apuesta: {markets}" if markets else "Partido terminado",
                    "tag": f"ft-{fix_id}",
                    "url": "/tracker",
                    "data": {"fixture_id": fix_id},
                }
                sent = send_to_user(uid, payload)
                if sent > 0:
                    notifications_sent += sent
                    logger.info(
                        "live_events FT push: fix=%s %s %d-%d %s user=%s",
                        fix_id, home_name, score_h, score_a, away_name, uid,
                    )

        elif goal_scored and not is_final:
            # Gol anotado durante el partido
            notified_goals = prev.get("notified_goals") or []
            score_key = f"{score_h}-{score_a}"
            if score_key in notified_goals:
                continue
            state[fix_id]["notified_goals"] = notified_goals + [score_key]

            for uid, legs in legs_by_user.items():
                # Verificar si algún mercado ya se puede resolver (ej. over ya ganado)
                early_results = []
                for leg in legs:
                    early = _resolve_market_for_push(leg["market"], score_h, score_a)
                    if early == "WON":
                        early_results.append(f"{leg['market']} ✅")

                if early_results:
                    body = f"⚽ {score_h}–{score_a} | {', '.join(early_results)}"
                else:
                    markets = ", ".join(leg["market"] for leg in legs if leg.get("market"))
                    body = f"⚽ {score_h}–{score_a} min {fix_elapsed}" + (f" | {markets}" if markets else "")

                payload = {
                    "title": f"{home_name} {score_h}–{score_a} {away_name}",
                    "body": body,
                    "tag": f"goal-{fix_id}-{score_key}",
                    "url": "/tracker",
                    "data": {"fixture_id": fix_id},
                }
                sent = send_to_user(uid, payload)
                if sent > 0:
                    notifications_sent += sent
                    logger.info(
                        "live_events GOAL push: fix=%s %s %d-%d %s min=%s user=%s",
                        fix_id, home_name, score_h, score_a, away_name, fix_elapsed, uid,
                    )

    # Limpiar estado de fixtures que ya terminaron hace más de 2h
    keys_to_remove = [
        k for k, v in state.items()
        if isinstance(v, dict) and v.get("status") in ("FT", "AET", "PEN", "FINISHED")
        and v.get("notified_final")
    ]
    for k in keys_to_remove:
        del state[k]
        state_changed = True

    if state_changed:
        _save_state(state)

    return notifications_sent
