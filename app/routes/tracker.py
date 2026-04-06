"""
AFTR Tracker — bet tracking API.
POST   /tracker/bets          → create bet with legs
GET    /tracker/bets          → list my bets (JSON)
PATCH  /tracker/legs/{leg_id} → resolve leg (WON/LOST/VOID/PUSHED)
DELETE /tracker/bets/{bet_id} → delete bet
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse

from app.auth import get_user_id
from app.db import get_conn, put_conn

router = APIRouter()

VALID_MARKETS = {
    "1", "X", "2",
    "1X", "X2", "12",
    "over_1.5", "over_2.5", "over_3.5",
    "under_1.5", "under_2.5",
    "btts_yes", "btts_no",
    "dnb_1", "dnb_2",
}

VALID_LEG_STATUSES = {"WON", "LOST", "VOID", "PUSHED"}


def _require_user(request: Request):
    uid = get_user_id(request)
    if not uid:
        return None, JSONResponse({"ok": False, "error": "not_authenticated"}, status_code=401)
    return uid, None


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
            continue  # void/pushed = 1.0x multiplier
        total *= float(r["odds"])
    return round(total, 3)


@router.post("/bets")
def create_bet(request: Request, payload: dict = Body(...)):
    uid, err = _require_user(request)
    if err:
        return err

    legs = payload.get("legs") or []
    stake = payload.get("stake")
    bet_type = payload.get("bet_type", "simple")
    note = payload.get("note", "")

    if not legs:
        return JSONResponse({"ok": False, "error": "missing_legs"}, status_code=400)
    if not stake:
        return JSONResponse({"ok": False, "error": "missing_stake"}, status_code=400)
    if bet_type not in ("simple", "combinada"):
        return JSONResponse({"ok": False, "error": "invalid_bet_type"}, status_code=400)

    try:
        stake = float(stake)
        assert stake > 0
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_stake"}, status_code=400)

    for leg in legs:
        if not leg.get("home_team") or not leg.get("away_team"):
            return JSONResponse({"ok": False, "error": "missing_team"}, status_code=400)
        if leg.get("market") not in VALID_MARKETS:
            return JSONResponse({"ok": False, "error": f"invalid_market"}, status_code=400)
        try:
            assert float(leg["odds"]) >= 1.0
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid_odds"}, status_code=400)

    total_odds = 1.0
    for leg in legs:
        total_odds *= float(leg["odds"])
    total_odds = round(total_odds, 3)
    potential_payout = round(stake * total_odds, 2)

    now = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO user_bets
               (user_id, bet_type, stake, total_odds, potential_payout, status, note, created_at)
               VALUES (%s, %s, %s, %s, %s, 'PENDING', %s, %s) RETURNING id""",
            (uid, bet_type, stake, total_odds, potential_payout, note or None, now),
        )
        bet_id = cur.fetchone()["id"]
        for i, leg in enumerate(legs):
            kickoff = leg.get("kickoff_time") or None
            cur.execute(
                """INSERT INTO bet_legs
                   (bet_id, home_team, away_team, market, odds, status, sort_order, kickoff_time)
                   VALUES (%s, %s, %s, %s, %s, 'PENDING', %s, %s)""",
                (bet_id, leg["home_team"], leg["away_team"], leg["market"], float(leg["odds"]), i, kickoff),
            )
        conn.commit()
        return JSONResponse({"ok": True, "bet_id": bet_id})
    except Exception as e:
        conn.rollback()
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    finally:
        put_conn(conn)


@router.get("/bets")
def list_bets(request: Request):
    uid, err = _require_user(request)
    if err:
        return err

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, bet_type, stake, total_odds, potential_payout, status, note, created_at, settled_at
               FROM user_bets WHERE user_id = %s ORDER BY created_at DESC LIMIT 100""",
            (uid,),
        )
        bets = [dict(r) for r in cur.fetchall()]
        for bet in bets:
            cur.execute(
                """SELECT id, home_team, away_team, market, odds, status, sort_order, resolved_at
                   FROM bet_legs WHERE bet_id = %s ORDER BY sort_order""",
                (bet["id"],),
            )
            bet["legs"] = [dict(r) for r in cur.fetchall()]
            for key in ("created_at", "settled_at"):
                if bet[key] and hasattr(bet[key], "isoformat"):
                    bet[key] = bet[key].isoformat()
            for leg in bet["legs"]:
                if leg.get("resolved_at") and hasattr(leg["resolved_at"], "isoformat"):
                    leg["resolved_at"] = leg["resolved_at"].isoformat()
                leg["odds"] = float(leg["odds"]) if leg["odds"] else None
            bet["stake"] = float(bet["stake"]) if bet["stake"] else None
            bet["total_odds"] = float(bet["total_odds"]) if bet["total_odds"] else None
            bet["potential_payout"] = float(bet["potential_payout"]) if bet["potential_payout"] else None
        return JSONResponse({"ok": True, "bets": bets})
    finally:
        put_conn(conn)


@router.patch("/legs/{leg_id}")
def resolve_leg(leg_id: int, request: Request, payload: dict = Body(...)):
    uid, err = _require_user(request)
    if err:
        return err

    new_status = (payload.get("status") or "").upper()
    if new_status not in VALID_LEG_STATUSES:
        return JSONResponse({"ok": False, "error": "invalid_status"}, status_code=400)

    now = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT bl.id, bl.bet_id FROM bet_legs bl
               JOIN user_bets ub ON bl.bet_id = ub.id
               WHERE bl.id = %s AND ub.user_id = %s""",
            (leg_id, uid),
        )
        row = cur.fetchone()
        if not row:
            return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)

        bet_id = row["bet_id"]
        cur.execute(
            "UPDATE bet_legs SET status = %s, resolved_at = %s WHERE id = %s",
            (new_status, now, leg_id),
        )

        new_bet_status = _recompute_bet_status(cur, bet_id)
        new_total_odds = _recompute_odds(cur, bet_id)

        cur.execute("SELECT stake FROM user_bets WHERE id = %s", (bet_id,))
        stake_row = cur.fetchone()
        new_payout = round(float(stake_row["stake"]) * new_total_odds, 2)
        settled_at = now if new_bet_status in ("WON", "LOST") else None

        cur.execute(
            """UPDATE user_bets
               SET status = %s, total_odds = %s, potential_payout = %s, settled_at = %s
               WHERE id = %s""",
            (new_bet_status, new_total_odds, new_payout, settled_at, bet_id),
        )
        conn.commit()
        return JSONResponse({
            "ok": True,
            "bet_status": new_bet_status,
            "total_odds": new_total_odds,
            "potential_payout": new_payout,
        })
    except Exception as e:
        conn.rollback()
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    finally:
        put_conn(conn)


@router.delete("/bets/{bet_id}")
def delete_bet(bet_id: int, request: Request):
    uid, err = _require_user(request)
    if err:
        return err

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM user_bets WHERE id = %s AND user_id = %s RETURNING id",
            (bet_id, uid),
        )
        if not cur.fetchone():
            conn.rollback()
            return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
        conn.commit()
        return JSONResponse({"ok": True})
    except Exception as e:
        conn.rollback()
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    finally:
        put_conn(conn)
