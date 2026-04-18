"""
AFTR Auto-Settle — resuelve automáticamente bet_legs pendientes cuyo kickoff ya pasó.

Corre después de cada results refresh. Busca el partido terminado en el caché por
equipo + fecha, y resuelve el leg según el mercado.

Mercados soportados: 1, X, 2, 1X, X2, 12, over_X.X, under_X.X, btts_yes, btts_no,
                     dnb_1, dnb_2
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("aftr.auto_settle")

# Statuses that definitively mean the match is over
FINISHED_STATUSES = frozenset({
    "FINISHED", "FT", "FINAL", "AWARDED", "FINALIZADO",
    "AET",      # after extra time (API-Football)
    "PEN",      # after penalties (API-Football)
    "FT_PEN",   # some providers
})
# Statuses that mean the match is still in progress — never settle against these
LIVE_STATUSES = frozenset({
    "1H", "2H", "HT", "ET", "BT", "P", "SUSP",
    "LIVE", "IN_PLAY", "IN PLAY", "INPLAY",
    "PAUSED", "HALFTIME",
})
# Time (minutes from kickoff) before we attempt settlement when status is unknown.
# 120 min = kickoff + ~45' first half + 15' HT break + ~45' second half + ~15' stoppage buffer
SETTLE_AFTER_MIN = 115


def _normalize(name: str) -> str:
    """Normalización básica para comparar nombres de equipos."""
    return (name or "").lower().strip()


def _teams_match(leg_home: str, leg_away: str, cache_home: str, cache_away: str) -> bool:
    """True si los nombres de los equipos corresponden (tolerancia de subcadena)."""
    lh, la = _normalize(leg_home), _normalize(leg_away)
    ch, ca = _normalize(cache_home), _normalize(cache_away)
    if not lh or not la or not ch or not ca:
        return False
    # Coincidencia exacta
    if lh == ch and la == ca:
        return True
    # Subcadena: leg está contenido en cache o viceversa
    if (lh in ch or ch in lh) and (la in ca or ca in la):
        return True
    return False


def _resolve_market(market: str, home_goals: int, away_goals: int) -> str:
    """
    Retorna 'WON', 'LOST' o 'VOID' según el mercado y el resultado.
    """
    total = home_goals + away_goals
    market = market.lower().strip()

    if market == "1":
        return "WON" if home_goals > away_goals else "LOST"
    if market == "x":
        return "WON" if home_goals == away_goals else "LOST"
    if market == "2":
        return "WON" if away_goals > home_goals else "LOST"
    if market == "1x":
        return "WON" if home_goals >= away_goals else "LOST"
    if market == "x2":
        return "WON" if away_goals >= home_goals else "LOST"
    if market == "12":
        return "WON" if home_goals != away_goals else "LOST"

    if market.startswith("over_"):
        try:
            line = float(market[5:])
            return "WON" if total > line else "LOST"
        except ValueError:
            return "VOID"

    if market.startswith("under_"):
        try:
            line = float(market[6:])
            return "WON" if total < line else "LOST"
        except ValueError:
            return "VOID"

    if market == "btts_yes":
        return "WON" if home_goals > 0 and away_goals > 0 else "LOST"
    if market == "btts_no":
        return "WON" if home_goals == 0 or away_goals == 0 else "LOST"

    if market == "dnb_1":
        if home_goals > away_goals:
            return "WON"
        if home_goals == away_goals:
            return "VOID"
        return "LOST"
    if market == "dnb_2":
        if away_goals > home_goals:
            return "WON"
        if home_goals == away_goals:
            return "VOID"
        return "LOST"

    return "VOID"


def _load_all_finished_matches() -> list[dict]:
    """
    Lee todos los archivos de caché y devuelve partidos que tienen score FINAL confirmado.

    Estrategia de filtrado (orden de precedencia):
    1. Si el status es un LIVE_STATUS (partido en curso), siempre excluir.
    2. Si el status es un FINISHED_STATUS, incluir aunque el kickoff sea reciente.
    3. Si el status es desconocido/vacío, solo incluir si kickoff fue hace 120+ minutos
       (buffer suficiente para 90' + halftime + stoppage time).
    """
    from config.settings import settings
    from data.cache import read_json

    now = datetime.now(timezone.utc)
    finished: list[dict] = []
    for code in settings.league_codes():
        data = read_json(f"daily_matches_{code}.json")
        if not isinstance(data, list):
            continue
        for m in data:
            if not isinstance(m, dict):
                continue

            st = (m.get("status") or "").strip().upper()

            # 1) Siempre excluir partidos en curso o cancelados/postergados
            if st in LIVE_STATUSES:
                continue
            if st in ("CANCELLED", "POSTPONED", "SUSPENDED", "ABD", "AWD"):
                continue

            # 2) ¿El status confirma que el partido terminó?
            is_confirmed_finished = st in FINISHED_STATUSES

            # 3) Verificar kickoff pasado
            utc_raw = m.get("utcDate") or ""
            try:
                dt_str = str(utc_raw).replace("Z", "+00:00")
                kickoff_dt = datetime.fromisoformat(dt_str)
                if kickoff_dt.tzinfo is None:
                    kickoff_dt = kickoff_dt.replace(tzinfo=timezone.utc)
                elapsed_min = (now - kickoff_dt).total_seconds() / 60

                if is_confirmed_finished:
                    # Status confirms it — still require kickoff in past (>5 min) to avoid edge cases
                    if elapsed_min < 5:
                        continue
                else:
                    # Status unknown or not in FINISHED_STATUSES — require generous buffer
                    # 120 min = ~45' 1H + 15' HT + 45' 2H + 15' stoppage/injury time buffer
                    if elapsed_min < 120:
                        continue
            except Exception:
                if not is_confirmed_finished:
                    continue  # no kickoff date + unknown status → skip

            # Score: preferir extraTime (acumulativo, cubre partidos con ET),
            # luego fullTime (90 min), luego score.home/away o raíz del dict.
            score = m.get("score") or {}
            et = score.get("extraTime") or {}
            if et.get("home") is not None and et.get("away") is not None:
                gh, ga = et.get("home"), et.get("away")
            else:
                gh = score.get("home")
                ga = score.get("away")
                if gh is None or ga is None:
                    ft = score.get("fullTime") or {}
                    gh = ft.get("home")
                    ga = ft.get("away")
            # También acepta home_goals/away_goals en raíz del dict
            if gh is None:
                gh = m.get("home_goals")
            if ga is None:
                ga = m.get("away_goals")
            if gh is None or ga is None:
                continue
            try:
                gh, ga = int(gh), int(ga)
            except (TypeError, ValueError):
                continue

            raw_mid = m.get("match_id") or m.get("id")
            try:
                finished_mid = int(raw_mid) if raw_mid is not None else None
            except Exception:
                finished_mid = None
            finished.append({
                "home": m.get("home") or (m.get("homeTeam") or {}).get("name", ""),
                "away": m.get("away") or (m.get("awayTeam") or {}).get("name", ""),
                "home_goals": gh,
                "away_goals": ga,
                "utcDate": utc_raw,
                "status": st,
                "match_id": finished_mid,
            })
    return finished


def _recompute_bet_status(cur, bet_id: int) -> str:
    cur.execute("SELECT status FROM bet_legs WHERE bet_id = %s", (bet_id,))
    rows = cur.fetchall()
    if not rows:
        return "PENDING"
    statuses = [r["status"] for r in rows]
    if "LOST" in statuses:
        return "LOST"
    if all(s in ("WON", "VOID", "PUSHED") for s in statuses):
        return "WON"
    if any(s in ("WON", "PUSHED") for s in statuses):
        return "IN_PLAY"
    return "PENDING"


def _recompute_odds(cur, bet_id: int) -> float:
    cur.execute(
        "SELECT odds, status FROM bet_legs WHERE bet_id = %s ORDER BY sort_order",
        (bet_id,),
    )
    total = 1.0
    for r in cur.fetchall():
        if r["status"] in ("VOID", "PUSHED"):
            continue
        total *= float(r["odds"])
    return round(total, 3)


def _settle_bankroll(user_id: int, bet_id: int, bet_status: str,
                     stake: float, payout: float) -> None:
    """
    Actualiza current_bankroll y registra movimiento en bankroll_movements.
    WIN: delta = payout - stake  (ganancia neta, usando cuota real del tracker)
    LOSS: delta = -stake
    Solo actúa si el usuario tiene bankroll_settings configurado.
    """
    if bet_status == "WON":
        delta = round(payout - stake, 2)
        mtype = "WIN"
    elif bet_status == "LOST":
        delta = round(-stake, 2)
        mtype = "LOSS"
    else:
        return

    from app.db import get_conn, put_conn
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT initial_amount, current_bankroll FROM bankroll_settings WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            return  # usuario sin bankroll configurado

        base = (
            float(row["current_bankroll"])
            if row["current_bankroll"] is not None
            else float(row["initial_amount"])
        )
        new_balance = round(base + delta, 2)

        cur.execute(
            "UPDATE bankroll_settings SET current_bankroll = %s, updated_at = NOW() WHERE user_id = %s",
            (new_balance, user_id),
        )
        cur.execute(
            """INSERT INTO bankroll_movements
                   (user_id, bet_id, delta, balance_after, movement_type)
               VALUES (%s, %s, %s, %s, %s)""",
            (user_id, bet_id, delta, new_balance, mtype),
        )
        conn.commit()
        logger.info(
            "bankroll %s uid=%s bet=%s delta=%+.2f balance=%.2f",
            mtype, user_id, bet_id, delta, new_balance,
        )
    except Exception as _e:
        conn.rollback()
        logger.warning("bankroll settle error: %s", _e)
    finally:
        put_conn(conn)


def auto_settle_tracker_legs() -> int:
    """
    Resuelve bet_legs pendientes cuyos partidos ya terminaron.
    Retorna la cantidad de legs resueltos.
    """
    from app.db import get_conn, put_conn

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=SETTLE_AFTER_MIN)

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT bl.id AS leg_id, bl.bet_id, bl.home_team, bl.away_team,
                      bl.market, bl.kickoff_time, bl.match_id
               FROM bet_legs bl
               JOIN user_bets ub ON bl.bet_id = ub.id
               WHERE bl.status = 'PENDING'
                 AND (
                   (bl.kickoff_time IS NOT NULL AND bl.kickoff_time <= %s)
                   OR bl.kickoff_time IS NULL
                 )""",
            (cutoff,),
        )
        pending = list(cur.fetchall())
    finally:
        put_conn(conn)

    if not pending:
        return 0

    finished_matches = _load_all_finished_matches()
    if not finished_matches:
        return 0

    settled = 0
    _push_queue: list[dict] = []
    conn = get_conn()
    try:
        cur = conn.cursor()
        for leg in pending:
            leg_home = leg["home_team"] or ""
            leg_away = leg["away_team"] or ""
            market = leg["market"] or ""
            kickoff = leg["kickoff_time"]
            if kickoff and kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=timezone.utc)

            # Buscar partido terminado coincidente
            # Prioridad: match por match_id exacto; fallback: nombres de equipo
            leg_match_id = leg.get("match_id")
            matched = None

            if leg_match_id:
                for fm in finished_matches:
                    if fm.get("match_id") == leg_match_id:
                        matched = fm
                        break

            if not matched:
                for fm in finished_matches:
                    if not _teams_match(leg_home, leg_away, fm["home"], fm["away"]):
                        continue
                    if kickoff:
                        try:
                            utc_raw = str(fm["utcDate"]).replace("Z", "+00:00")
                            match_dt = datetime.fromisoformat(utc_raw)
                            if match_dt.tzinfo is None:
                                match_dt = match_dt.replace(tzinfo=timezone.utc)
                            if abs((match_dt - kickoff).total_seconds()) > 3 * 3600:
                                continue
                        except Exception as _err:
                            logger.warning("unexpected exception (non-fatal): %s", _err)
                    matched = fm
                    break

            if not matched:
                continue

            new_status = _resolve_market(market, matched["home_goals"], matched["away_goals"])
            resolved_at = now.isoformat()

            cur.execute(
                "UPDATE bet_legs SET status = %s, resolved_at = %s WHERE id = %s",
                (new_status, resolved_at, leg["leg_id"]),
            )

            bet_id = leg["bet_id"]
            new_bet_status = _recompute_bet_status(cur, bet_id)
            new_total_odds = _recompute_odds(cur, bet_id)

            cur.execute("SELECT stake FROM user_bets WHERE id = %s", (bet_id,))
            stake_row = cur.fetchone()
            new_payout = round(float(stake_row["stake"]) * new_total_odds, 2)
            settled_at = resolved_at if new_bet_status in ("WON", "LOST") else None

            cur.execute(
                """UPDATE user_bets
                   SET status = %s, total_odds = %s, potential_payout = %s, settled_at = %s
                   WHERE id = %s""",
                (new_bet_status, new_total_odds, new_payout, settled_at, bet_id),
            )

            settled += 1
            logger.info(
                "auto_settle leg=%s %s vs %s market=%s → %s (match: %d-%d)",
                leg["leg_id"], leg_home, leg_away, market, new_status,
                matched["home_goals"], matched["away_goals"],
            )

            # Push cuando la apuesta entera queda resuelta (WON o LOST)
            if new_bet_status in ("WON", "LOST"):
                _push_queue.append({
                    "user_id": leg["bet_id"],  # se resuelve abajo con user_id real
                    "_bet_id": bet_id,
                    "new_bet_status": new_bet_status,
                    "home": leg_home,
                    "away": leg_away,
                    "score_h": matched["home_goals"],
                    "score_a": matched["away_goals"],
                    "payout": new_payout,
                    "stake": float(stake_row["stake"]),
                })

        # Obtener user_id para cada bet en la cola de push
        for item in _push_queue:
            cur.execute("SELECT user_id, bet_type FROM user_bets WHERE id = %s", (item["_bet_id"],))
            row = cur.fetchone()
            if row:
                item["user_id"] = row["user_id"]
                item["bet_type"] = row.get("bet_type", "simple")

        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.exception("auto_settle error: %s", e)
        _push_queue.clear()
    finally:
        put_conn(conn)

    # Actualizar bankroll + enviar pushes fuera del bloque de DB
    for item in _push_queue:
        try:
            uid = item.get("user_id")
            if not uid:
                continue
            _settle_bankroll(
                user_id=uid,
                bet_id=item["_bet_id"],
                bet_status=item["new_bet_status"],
                stake=item["stake"],
                payout=item["payout"],
            )
        except Exception as _bk_err:
            logger.warning("bankroll update error: %s", _bk_err)

    for item in _push_queue:
        try:
            from services.push_notifications import send_to_user
            uid = item.get("user_id")
            if not uid:
                continue
            status = item["new_bet_status"]
            emoji = "✅" if status == "WON" else "❌"
            bet_type = item.get("bet_type", "simple")
            score_str = f"{item['score_h']}–{item['score_a']}"
            if bet_type == "combinada":
                body = (
                    f"Combinada {emoji} | {item['home']} {score_str} {item['away']}"
                    + (f" | Ganás ${item['payout']:.2f}" if status == "WON" else "")
                )
            else:
                body = (
                    f"{item['home']} {score_str} {item['away']}"
                    + (f" | Ganás ${item['payout']:.2f}" if status == "WON" else "")
                )
            payload = {
                "title": f"{emoji} Apuesta {'ganada' if status == 'WON' else 'perdida'}",
                "body": body,
                "tag": f"settle-{item['_bet_id']}",
                "url": "/tracker",
            }
            send_to_user(uid, payload)
        except Exception as _push_err:
            logger.warning("auto_settle push error: %s", _push_err)

    if settled:
        logger.info("auto_settle finished: %d legs resolved", settled)
    return settled
