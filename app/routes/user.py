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
    created_at = user.get("created_at")
    return JSONResponse({
        "ok": True,
        "user": {
            "id": user.get("id"),
            "email": user.get("email"),
            "username": user.get("username"),
            "role": user.get("role"),
            "subscription_status": user.get("subscription_status"),
            "premium_until": premium_until,
            "created_at": created_at,
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

    total = followed_picks
    roi = None
    if total and total > 0:
        roi = round((wins - losses) / total * 100.0, 2)

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
    """Store a favorite pick_id for the current user. Optional: market, aftr_score, tier, edge."""
    uid, err = _require_user(request)
    if err is not None:
        return err
    pick_id = (payload.get("pick_id") or "").strip()
    if not pick_id:
        return JSONResponse(
            {"ok": False, "error": "pick_id_required"},
            status_code=400,
        )
    market = (payload.get("market") or "").strip() or None
    aftr_score = payload.get("aftr_score")
    if aftr_score is not None:
        try:
            aftr_score = float(aftr_score)
        except (TypeError, ValueError):
            aftr_score = None
    tier = (payload.get("tier") or "").strip() or None
    edge = payload.get("edge")
    if edge is not None:
        try:
            edge = float(edge)
        except (TypeError, ValueError):
            edge = None
    now = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT OR IGNORE INTO user_favorites (user_id, pick_id, created_at, market, aftr_score, tier, edge)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (uid, pick_id, now, market, aftr_score, tier, edge),
        )
        cur.execute(
            """UPDATE user_favorites SET
               market = COALESCE(?, market), aftr_score = COALESCE(?, aftr_score),
               tier = COALESCE(?, tier), edge = COALESCE(?, edge)
               WHERE user_id = ? AND pick_id = ?""",
            (market, aftr_score, tier, edge, uid, pick_id),
        )
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"ok": True, "pick_id": pick_id})


@router.get("/favorites")
def user_favorites(request: Request):
    """List favorites for the current user with optional market, aftr_score, tier, edge."""
    uid, err = _require_user(request)
    if err is not None:
        return err
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT pick_id, created_at, market, aftr_score, tier, edge
               FROM user_favorites WHERE user_id = ? ORDER BY created_at DESC""",
            (uid,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    def _parse_pick_id(pid: str):
        if not pid or "|" not in str(pid):
            return None
        parts = str(pid).split("|")
        if len(parts) < 2:
            return None
        league = (parts[0] or "").strip()
        try:
            match_id = int(parts[1])
        except (TypeError, ValueError):
            return None
        if not league:
            return None
        return league, match_id

    def _teams_for_pick_id(pid: str):
        parsed = _parse_pick_id(pid)
        if not parsed:
            return ("", "")
        league, match_id = parsed
        c = get_conn()
        try:
            cur2 = c.cursor()
            cur2.execute(
                "SELECT home, away FROM matches WHERE league = ? AND match_id = ? LIMIT 1",
                (league, match_id),
            )
            row = cur2.fetchone()
            if not row:
                return ("", "")
            home = row["home"] if "home" in row.keys() else row[0]
            away = row["away"] if "away" in row.keys() else row[1]
            return (str(home) if home is not None else "", str(away) if away is not None else "")
        finally:
            c.close()

    items = []
    for row in rows:
        r = dict(row)
        home, away = _teams_for_pick_id(r.get("pick_id"))
        items.append({
            "pick_id": r.get("pick_id"),
            "created_at": r.get("created_at"),
            "market": r.get("market"),
            "aftr_score": r.get("aftr_score"),
            "tier": r.get("tier"),
            "edge": r.get("edge"),
            "home": home,
            "away": away,
        })
    return JSONResponse({"ok": True, "favorites": items})


@router.get("/followed-ids")
def user_followed_ids(request: Request):
    """List all followed pick_ids for the current user (for persisted UI state)."""
    uid, err = _require_user(request)
    if err is not None:
        return err
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT pick_id FROM user_picks WHERE user_id = ?",
            (uid,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    pick_ids = [row["pick_id"] for row in rows]
    return JSONResponse({"ok": True, "pick_ids": pick_ids})


@router.post("/follow-pick")
def user_follow_pick(request: Request, payload: dict = Body(...)):
    """Store a followed pick for the current user (action=follow). Avoids duplicate (user_id, pick_id)."""
    uid, err = _require_user(request)
    if err is not None:
        return err
    pick_id = (payload.get("pick_id") or "").strip()
    if not pick_id:
        return JSONResponse(
            {"ok": False, "error": "pick_id_required"},
            status_code=400,
        )
    market = (payload.get("market") or "").strip() or None
    aftr_score = payload.get("aftr_score")
    if aftr_score is not None:
        try:
            aftr_score = float(aftr_score)
        except (TypeError, ValueError):
            aftr_score = None
    tier = (payload.get("tier") or "").strip() or None
    edge = payload.get("edge")
    if edge is not None:
        try:
            edge = float(edge)
        except (TypeError, ValueError):
            edge = None
    now = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM user_picks WHERE user_id = ? AND pick_id = ? LIMIT 1",
            (uid, pick_id),
        )
        if cur.fetchone():
            return JSONResponse({"ok": True, "pick_id": pick_id})
        cur.execute(
            """INSERT INTO user_picks
               (user_id, pick_id, action, result, created_at, market, aftr_score, tier, edge)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (uid, pick_id, "follow", "PENDING", now, market, aftr_score, tier, edge),
        )
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"ok": True, "pick_id": pick_id})


@router.get("/history")
def user_history(request: Request):
    """Followed picks for the current user, newest first. Limit 10. Includes market, aftr_score, tier, edge, result."""
    uid, err = _require_user(request)
    if err is not None:
        return err
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, pick_id, action, result, created_at, market, aftr_score, tier, edge
               FROM user_picks WHERE user_id = ?
               ORDER BY created_at DESC LIMIT 10""",
            (uid,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    def _parse_pick_id(pid: str):
        if not pid or "|" not in str(pid):
            return None
        parts = str(pid).split("|")
        if len(parts) < 2:
            return None
        league = (parts[0] or "").strip()
        try:
            match_id = int(parts[1])
        except (TypeError, ValueError):
            return None
        if not league:
            return None
        return league, match_id

    def _teams_for_pick_id(pid: str):
        parsed = _parse_pick_id(pid)
        if not parsed:
            return ("", "")
        league, match_id = parsed
        c = get_conn()
        try:
            cur2 = c.cursor()
            cur2.execute(
                "SELECT home, away FROM matches WHERE league = ? AND match_id = ? LIMIT 1",
                (league, match_id),
            )
            row = cur2.fetchone()
            if not row:
                return ("", "")
            home = row["home"] if "home" in row.keys() else row[0]
            away = row["away"] if "away" in row.keys() else row[1]
            return (str(home) if home is not None else "", str(away) if away is not None else "")
        finally:
            c.close()

    items = []
    for row in rows:
        r = dict(row)
        home, away = _teams_for_pick_id(r.get("pick_id"))
        items.append({
            "id": r.get("id"),
            "pick_id": r.get("pick_id"),
            "action": r.get("action"),
            "result": r.get("result") or "PENDING",
            "created_at": r.get("created_at"),
            "market": r.get("market"),
            "aftr_score": r.get("aftr_score"),
            "tier": r.get("tier"),
            "edge": r.get("edge"),
            "home": home,
            "away": away,
        })
    return JSONResponse({"ok": True, "history": items})
