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
from config.settings import settings
from data.cache import read_json

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


def _norm_team_name(v: object) -> str | None:
    """Normalize incoming team strings; return None for placeholders/missing."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    low = s.lower()
    if low in {"ninguno", "none", "null", "n/a", "-", "—", "–"}:
        return None
    return s


def _norm_result(v: object) -> str:
    """Normalize result strings into {WIN, LOSS, PUSH, PENDING}."""
    r = (str(v or "")).strip().upper()
    return r if r in {"WIN", "LOSS", "PUSH", "PENDING"} else "PENDING"


def _daily_pick_id(p: dict, league_code: str) -> str:
    """
    Reproduce the same stable-ish pick_id logic used by the UI renderer.
    Enough for resolving "pending" history rows into finished results.
    """
    if not isinstance(p, dict):
        return ""
    pid = p.get("id") or p.get("pick_id")
    if pid is not None and str(pid).strip():
        return str(pid).strip()

    match_id = str(p.get("match_id") or p.get("id") or "")
    market = str(p.get("best_market") or "")
    utc = str(p.get("utcDate") or "")
    return "|".join([str(league_code or "").strip(), match_id, market, utc]).strip("|") or "unknown"


def _safe_int(v: object) -> int | None:
    try:
        if v is None:
            return None
        s = str(v).strip()
        if not s:
            return None
        return int(float(s))
    except Exception:
        return None


def _extract_score(p: object) -> tuple[int | None, int | None]:
    """Best-effort final score extraction from daily pick dicts."""
    if not isinstance(p, dict):
        return (None, None)
    h = _safe_int(p.get("score_home"))
    a = _safe_int(p.get("score_away"))
    if h is not None and a is not None:
        return (h, a)

    sc = p.get("score")
    if isinstance(sc, dict):
        hh = _safe_int(sc.get("home"))
        aa = _safe_int(sc.get("away"))
        if hh is not None and aa is not None:
            return (hh, aa)
        ft = sc.get("fullTime") or sc.get("full_time")
        if isinstance(ft, dict):
            hh = _safe_int(ft.get("home"))
            aa = _safe_int(ft.get("away"))
            if hh is not None and aa is not None:
                return (hh, aa)

    return (None, None)


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
    """Store a favorite pick_id for the current user. Optional: market, aftr_score, tier, edge, home_team, away_team."""
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
    home_team = _norm_team_name(payload.get("home_team"))
    away_team = _norm_team_name(payload.get("away_team"))
    now = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT OR IGNORE INTO user_favorites
               (user_id, pick_id, created_at, market, aftr_score, tier, edge, home_team, away_team)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (uid, pick_id, now, market, aftr_score, tier, edge, home_team, away_team),
        )
        cur.execute(
            """UPDATE user_favorites SET
               market = COALESCE(?, market), aftr_score = COALESCE(?, aftr_score),
               tier = COALESCE(?, tier), edge = COALESCE(?, edge),
               home_team = COALESCE(?, home_team), away_team = COALESCE(?, away_team)
               WHERE user_id = ? AND pick_id = ?""",
            (market, aftr_score, tier, edge, home_team, away_team, uid, pick_id),
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
    items: list[dict] = []
    try:
        conn = get_conn()
        try:
            cur = conn.cursor()

            def _table_columns(table: str) -> set[str]:
                # Introspect columns so missing tables/columns never crash.
                cur2 = conn.cursor()
                cur2.execute(f"PRAGMA table_info({table})")
                cols = cur2.fetchall()
                # SQLite returns columns with "name" as the second field.
                out = set()
                for c in cols:
                    try:
                        out.add(str(c.get("name")))
                    except Exception:
                        out.add(str(c[1]))
                return out

            uf_cols = _table_columns("user_favorites")
            up_cols = _table_columns("user_picks")

            has_home_away_in_favs = "home_team" in uf_cols and "away_team" in uf_cols
            has_home_away_in_picks = "home_team" in up_cols and "away_team" in up_cols

            base_cols = ["pick_id", "created_at", "market", "aftr_score", "tier", "edge"]
            select_cols = base_cols[:]
            if has_home_away_in_favs:
                select_cols += ["home_team", "away_team"]

            sql = "SELECT " + ", ".join(select_cols) + " FROM user_favorites WHERE user_id = ? ORDER BY created_at DESC"
            cur.execute(sql, (uid,))
            rows = cur.fetchall()

            for row in rows:
                r = dict(row)
                pick_id = r.get("pick_id")
                home = r.get("home_team") if has_home_away_in_favs else None
                away = r.get("away_team") if has_home_away_in_favs else None

                if (home is None or away is None) and has_home_away_in_picks:
                    cur.execute(
                        "SELECT home_team, away_team FROM user_picks WHERE user_id = ? AND pick_id = ? LIMIT 1",
                        (uid, pick_id),
                    )
                    rr = cur.fetchone()
                    if rr:
                        rr_d = dict(rr)
                        home = rr_d.get("home_team")
                        away = rr_d.get("away_team")

                items.append({
                    "pick_id": pick_id,
                    "created_at": r.get("created_at"),
                    "market": r.get("market"),
                    "aftr_score": r.get("aftr_score"),
                    "tier": r.get("tier"),
                    "edge": r.get("edge"),
                    "home": "" if home is None else str(home),
                    "away": "" if away is None else str(away),
                })
        finally:
            conn.close()
    except Exception:
        # Never crash the user panel.
        items = []

    return JSONResponse({"ok": True, "favorites": items})


@router.post("/unfavorite")
def user_unfavorite(request: Request, payload: dict = Body(...)):
    """Remove a pick from the current user's favorites. Body: { "pick_id": "..." }."""
    uid, err = _require_user(request)
    if err is not None:
        return err
    pick_id = (payload.get("pick_id") or "").strip()
    if not pick_id:
        return JSONResponse(
            {"ok": False, "error": "pick_id_required"},
            status_code=400,
        )
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM user_favorites WHERE user_id = ? AND pick_id = ?",
            (uid, pick_id),
        )
        conn.commit()
        deleted = cur.rowcount
    finally:
        conn.close()
    return JSONResponse({"ok": True, "removed": deleted > 0, "pick_id": pick_id})


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
    """Store a followed pick for the current user (action=follow). Avoids duplicate (user_id, pick_id). Optional: home_team/away_team."""
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
    home_team = _norm_team_name(payload.get("home_team"))
    away_team = _norm_team_name(payload.get("away_team"))
    now = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT OR IGNORE INTO user_picks
               (user_id, pick_id, action, result, created_at, market, aftr_score, tier, edge, home_team, away_team)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (uid, pick_id, "follow", "PENDING", now, market, aftr_score, tier, edge, home_team, away_team),
        )
        cur.execute(
            """UPDATE user_picks SET
               market = COALESCE(?, market), aftr_score = COALESCE(?, aftr_score),
               tier = COALESCE(?, tier), edge = COALESCE(?, edge),
               home_team = COALESCE(?, home_team), away_team = COALESCE(?, away_team)
               WHERE user_id = ? AND pick_id = ?""",
            (market, aftr_score, tier, edge, home_team, away_team, uid, pick_id),
        )
        conn.commit()
    finally:
        conn.close()
    return JSONResponse({"ok": True, "pick_id": pick_id})


@router.post("/unfollow")
def user_unfollow(request: Request, payload: dict = Body(...)):
    """Stop following a pick (remove from user_picks). Body: { "pick_id": "..." }."""
    uid, err = _require_user(request)
    if err is not None:
        return err
    pick_id = (payload.get("pick_id") or "").strip()
    if not pick_id:
        return JSONResponse(
            {"ok": False, "error": "pick_id_required"},
            status_code=400,
        )
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM user_picks WHERE user_id = ? AND pick_id = ?",
            (uid, pick_id),
        )
        conn.commit()
        deleted = cur.rowcount
    finally:
        conn.close()
    return JSONResponse({"ok": True, "removed": deleted > 0, "pick_id": pick_id})


@router.get("/history")
def user_history(request: Request):
    """Followed picks for the current user, newest first. Limit 10. Includes market, aftr_score, tier, edge, result."""
    uid, err = _require_user(request)
    if err is not None:
        return err
    items: list[dict] = []
    try:
        conn = get_conn()
        try:
            cur = conn.cursor()

            def _table_columns(table: str) -> set[str]:
                cur2 = conn.cursor()
                cur2.execute(f"PRAGMA table_info({table})")
                cols = cur2.fetchall()
                out = set()
                for c in cols:
                    try:
                        out.add(str(c.get("name")))
                    except Exception:
                        out.add(str(c[1]))
                return out

            up_cols = _table_columns("user_picks")
            has_home_away = "home_team" in up_cols and "away_team" in up_cols

            base_cols = ["id", "pick_id", "action", "result", "created_at", "market", "aftr_score", "tier", "edge"]
            select_cols = base_cols[:]
            if has_home_away:
                select_cols += ["home_team", "away_team"]

            sql = "SELECT " + ", ".join(select_cols) + " FROM user_picks WHERE user_id = ? ORDER BY created_at DESC LIMIT 10"
            cur.execute(sql, (uid,))
            rows = cur.fetchall()

            pending_pick_ids: set[str] = set()
            for row in rows:
                r = dict(row)
                pick_id = r.get("pick_id")
                if not pick_id:
                    continue
                if _norm_result(r.get("result")) == "PENDING":
                    pending_pick_ids.add(str(pick_id))

            resolved_daily: dict[str, dict] = {}
            if pending_pick_ids:
                # If some pick_id values are composite (league|match_id|market|utc),
                # resolve by (league_code, match_id, utc) so market mismatches don't block resolution.
                pending_composite_map: dict[tuple[str, str, str], set[str]] = {}
                for pid in pending_pick_ids:
                    parts = str(pid).split("|")
                    if len(parts) == 4:
                        # league_code, match_id, market, utc
                        key = (parts[0], parts[1], parts[3])
                        pending_composite_map.setdefault(key, set()).add(pid)

                # Resolve pending history rows using the latest daily_picks cache.
                for league_code in list(getattr(settings, "leagues", {}).keys()):
                    picks = read_json(f"daily_picks_{league_code}.json") or []
                    if not isinstance(picks, list):
                        continue
                    for p in picks:
                        if not isinstance(p, dict):
                            continue
                        pid_raw = p.get("id") or p.get("pick_id")
                        pid_raw = str(pid_raw).strip() if pid_raw is not None else ""
                        if pid_raw and pid_raw in pending_pick_ids:
                            resolved_daily[pid_raw] = p
                        else:
                            computed_pid = _daily_pick_id(p, str(league_code))
                            if computed_pid and computed_pid in pending_pick_ids:
                                resolved_daily[computed_pid] = p

                        # Composite fallback resolution (ignoring market)
                        match_id = str(p.get("match_id") or p.get("id") or "")
                        utc = str(p.get("utcDate") or "")
                        comp_key = (str(league_code), match_id, utc)
                        if comp_key in pending_composite_map:
                            for resolved_pid in pending_composite_map[comp_key]:
                                resolved_daily[resolved_pid] = p
                            pending_composite_map.pop(comp_key, None)
                        if pending_pick_ids.issubset(resolved_daily.keys()):
                            break
                    if pending_pick_ids.issubset(resolved_daily.keys()):
                        break

            for row in rows:
                r = dict(row)
                pick_id = r.get("pick_id")
                home = r.get("home_team") if has_home_away else None
                away = r.get("away_team") if has_home_away else None

                result = _norm_result(r.get("result"))
                final_score = None
                if result == "PENDING" and pick_id:
                    dp = resolved_daily.get(str(pick_id))
                    if isinstance(dp, dict):
                        result = _norm_result(dp.get("result"))
                        if result == "PENDING":
                            status_raw = str(dp.get("status") or "").strip().upper()
                            if status_raw in ("WIN", "LOSS", "PUSH"):
                                result = status_raw
                        if has_home_away:
                            if _norm_team_name(home) is None:
                                home = dp.get("home_team") or dp.get("home")
                            if _norm_team_name(away) is None:
                                away = dp.get("away_team") or dp.get("away")
                        if result in ("WIN", "LOSS", "PUSH"):
                            hs, a_s = _extract_score(dp)
                            if hs is not None and a_s is not None:
                                final_score = f"{hs}-{a_s}"

                items.append({
                    "id": r.get("id"),
                    "pick_id": r.get("pick_id"),
                    "action": r.get("action"),
                    "result": result,
                    "created_at": r.get("created_at"),
                    "market": r.get("market"),
                    "aftr_score": r.get("aftr_score"),
                    "tier": r.get("tier"),
                    "edge": r.get("edge"),
                    "home": "" if home is None else str(home),
                    "away": "" if away is None else str(away),
                    "final_score": final_score,
                })
        finally:
            conn.close()
    except Exception:
        items = []

    return JSONResponse({"ok": True, "history": items})
