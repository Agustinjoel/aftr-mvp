"""
Phase 1 user system: /user/me, /user/stats, /user/favorite, /user/follow-pick, /user/history.
All endpoints require session; return 401 JSON when not logged in.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse

from app.auth import get_user_id, get_user_by_id
from app.db import get_conn

router = APIRouter()


def _require_user(request: Request) -> tuple[int | None, JSONResponse | None]:
    """Return (user_id, None) if logged in, else (None, 401 response)."""
    uid = get_user_id(request)
    if not uid:
        return None, JSONResponse(
            {"ok": False, "error": "not_authenticated"},
            status_code=401,
        )
    return uid, None


def _premium_until(user_id: int) -> str | None:
    """Return subscriptions.expires_at for active plan, else None."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT expires_at FROM subscriptions WHERE user_id = ?",
            (user_id,),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return None
    exp = row["expires_at"]
    if not exp:
        return None
    try:
        dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt <= datetime.now(timezone.utc):
            return None
        return str(exp)
    except Exception:
        return None


@router.get("/me")
def user_me(request: Request):
    """Current user: id, email, username, role, subscription_status, premium_until."""
    uid, err = _require_user(request)
    if err is not None:
        return err
    user = get_user_by_id(uid)
    if not user:
        return JSONResponse(
            {"ok": False, "error": "user_not_found"},
            status_code=401,
        )
    premium_until = _premium_until(uid)
    return JSONResponse({
        "ok": True,
        "user": {
            "id": user.get("id"),
            "email": user.get("email"),
            "username": user.get("username"),
            "role": user.get("role"),
            "subscription_status": user.get("subscription_status"),
            "premium_until": premium_until,
        },
    })


@router.get("/stats")
def user_stats(request: Request):
    """followed_picks, favorites_count, wins, losses, pending, roi (placeholder)."""
    uid, err = _require_user(request)
    if err is not None:
        return err
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS n FROM user_favorites WHERE user_id = ?",
            (uid,),
        )
        favorites_count = cur.fetchone()["n"]

        cur.execute(
            "SELECT COUNT(*) AS n FROM user_picks WHERE user_id = ?",
            (uid,),
        )
        followed_picks = cur.fetchone()["n"]

        cur.execute(
            """SELECT result, COUNT(*) AS n FROM user_picks WHERE user_id = ?
               GROUP BY COALESCE(result, 'PENDING')""",
            (uid,),
        )
        by_result = {str(row["result"] or "PENDING"): row["n"] for row in cur.fetchall()}
        wins = by_result.get("WIN", 0)
        losses = by_result.get("LOSS", 0)
        push = by_result.get("PUSH", 0)
        pending = by_result.get("PENDING", 0)
    finally:
        conn.close()

    settled = wins + losses + push
    roi = None
    if settled > 0:
        roi = round((wins - losses) / settled * 100.0, 2)

    return JSONResponse({
        "ok": True,
        "stats": {
            "followed_picks": followed_picks,
            "favorites_count": favorites_count,
            "wins": wins,
            "losses": losses,
            "pending": pending,
            "roi": roi,
        },
    })


@router.post("/favorite")
def user_favorite(request: Request, payload: dict = Body(...)):
    """Store a favorite pick_id for the current user."""
    uid, err = _require_user(request)
    if err is not None:
        return err
    pick_id = (payload.get("pick_id") or "").strip()
    if not pick_id:
        return JSONResponse(
            {"ok": False, "error": "pick_id_required"},
            status_code=400,
        )
    now = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT OR IGNORE INTO user_favorites (user_id, pick_id, created_at)
               VALUES (?, ?, ?)""",
            (uid, pick_id, now),
        )
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"ok": True, "pick_id": pick_id})


@router.post("/follow-pick")
def user_follow_pick(request: Request, payload: dict = Body(...)):
    """Store a followed pick for the current user (action=follow)."""
    uid, err = _require_user(request)
    if err is not None:
        return err
    pick_id = (payload.get("pick_id") or "").strip()
    if not pick_id:
        return JSONResponse(
            {"ok": False, "error": "pick_id_required"},
            status_code=400,
        )
    now = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO user_picks (user_id, pick_id, action, result, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (uid, pick_id, "follow", None, now),
        )
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"ok": True, "pick_id": pick_id})


@router.get("/history")
def user_history(request: Request):
    """Followed picks for the current user, newest first."""
    uid, err = _require_user(request)
    if err is not None:
        return err
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, pick_id, action, result, created_at
               FROM user_picks WHERE user_id = ?
               ORDER BY created_at DESC""",
            (uid,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    items = [
        {
            "id": row["id"],
            "pick_id": row["pick_id"],
            "action": row["action"],
            "result": row["result"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]
    return JSONResponse({"ok": True, "history": items})
