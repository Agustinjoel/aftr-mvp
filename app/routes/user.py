"""
Phase 1 user system: /user/me, /user/stats, /user/favorite, /user/follow-pick, /user/history.
All endpoints require session; return 401 JSON when not logged in.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse

from app.auth import get_user_id, get_user_by_id
from app.db import get_conn, put_conn
from config.settings import settings
from core.basketball_evaluation import evaluate_basketball_market
from core.evaluation import evaluate_market
from data.cache import read_json

router = APIRouter()

# Short-lived cache: building league pick indexes reads many JSON files.
_RESOLUTION_MAPS: tuple[dict, dict, dict, dict] | None = None
_RESOLUTION_MAPS_MONO: float = 0.0
_RESOLUTION_MAPS_TTL_SEC = 45.0


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
            "SELECT expires_at FROM subscriptions WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
    finally:
        put_conn(conn)
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


def _parse_utcdate_maybe(v: object) -> datetime | None:
    """Best-effort UTC datetime parsing from cached match/pick dicts."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        # Handle trailing Z -> +00:00
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _team_norm_key(v: object) -> str | None:
    """Lower/strip + validate team names for matching."""
    n = _norm_team_name(v)
    if n is None:
        return None
    return str(n).strip().lower()


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


def _as_pick_list(data: object) -> list[dict]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


def _dashboard_match_finished(m: dict) -> bool:
    """
    Conservative: treat as finished when we see explicit finished tokens OR when final scores are present
    and utcDate is in the past (and not clearly live).
    """
    if not isinstance(m, dict):
        return False

    finished_flag_raw = m.get("finished")
    if isinstance(finished_flag_raw, bool):
        if finished_flag_raw:
            return True
        # finished=false is a strong indicator it's not settled yet
        return False

    s = str(m.get("status") or m.get("match_status") or "").strip().upper()
    if not s and m.get("stage") is not None:
        s = str(m.get("stage")).strip().upper()

    finished_tokens = {"FINISHED", "FINAL", "FT", "SETTLED", "FINALIZADO", "ENDED", "AET", "PEN", "AWARDED"}
    live_tokens = {
        "LIVE",
        "IN_PLAY",
        "PAUSED",
        "HALFTIME",
        "HT",
        "BREAK",
        "1H",
        "2H",
        "Q1",
        "Q2",
        "Q3",
        "Q4",
        "OT",
    }
    not_finished_tokens = {"TIMED", "SCHEDULED", "NS", "TBD", "POSTPONED", "CANCELLED", "SUSP", "ABD", "CANCL"}

    if s in finished_tokens:
        return True
    if s in live_tokens:
        return False
    if s in not_finished_tokens:
        return False

    hs, aa = _extract_score(m)
    if hs is None or aa is None:
        return False
    dt = _parse_utcdate_maybe(m.get("utcDate"))
    if dt is None:
        return False
    # Fallback conservador: solo marcar como terminado si pasaron al menos 2h del kickoff
    # (evita evaluar partidos que están en vivo con score parcial).
    if dt <= datetime.now(timezone.utc) - timedelta(hours=2):
        return True
    return False


def _evaluate_market_for_league(league: str, market: str, hg: int, ag: int) -> str:
    sport = getattr(settings, "league_sport", {}).get(league, "football")
    if sport == "basketball":
        res, _ = evaluate_basketball_market(market, hg, ag)
        return _norm_result(res)
    res, _ = evaluate_market(market, hg, ag)
    return _norm_result(res)


def _settle_from_pick_and_match(
    league: str,
    p: dict,
    market_row: str | None,
    matches_idx: dict[tuple[str, int], dict],
    matches_by_team: dict[tuple[str, str, str], list[dict]],
) -> tuple[str, int | None, int | None]:
    mkt = (market_row or p.get("best_market") or p.get("market") or "").strip()

    r = _norm_result(p.get("result"))
    if r == "PENDING":
        r = _norm_result(p.get("status"))
    hs, aa = _extract_score(p)
    if r in ("WIN", "LOSS", "PUSH"):
        return r, hs, aa

    mid = _safe_int(p.get("match_id") or p.get("id"))
    if mid is not None:
        match = matches_idx.get((league, mid))
        if isinstance(match, dict) and _dashboard_match_finished(match):
            hs2, aa2 = _extract_score(match)
            if hs2 is None or aa2 is None:
                hs2, aa2 = hs, aa
            if hs2 is not None and aa2 is not None:
                ev = _evaluate_market_for_league(league, mkt, hs2, aa2)
                return ev, hs2, aa2

    # Fallback by team pair when match_id doesn't resolve.
    home_key = _team_norm_key(p.get("home_team") or p.get("home"))
    away_key = _team_norm_key(p.get("away_team") or p.get("away"))
    if not home_key or not away_key:
        return "PENDING", None, None

    candidates = matches_by_team.get((league, home_key, away_key), [])
    for cand in candidates:
        if not isinstance(cand, dict) or not _dashboard_match_finished(cand):
            continue
        hs2, aa2 = _extract_score(cand)
        if hs2 is not None and aa2 is not None:
            ev = _evaluate_market_for_league(league, mkt, hs2, aa2)
            return ev, hs2, aa2

    return "PENDING", None, None


def _pick_resolution_maps() -> tuple[
    dict[str, tuple[str, dict]],
    dict[tuple[str, str, str], tuple[str, dict]],
    dict[tuple[str, int], dict],
    dict[tuple[str, str, str], list[dict]],
]:
    """
    by_pick_id: pick_id / composite id -> (league, pick_dict)
    by_triple: (league, match_id_str, utc) -> (league, pick_dict)
    matches_idx: (league, match_id_int) -> match dict
    matches_by_team: (league, home_norm, away_norm) -> list[match dict]
    """
    by_pick_id: dict[str, tuple[str, dict]] = {}
    by_triple: dict[tuple[str, str, str], tuple[str, dict]] = {}
    matches_idx: dict[tuple[str, int], dict] = {}
    matches_by_team: dict[tuple[str, str, str], list[dict]] = {}

    def add_from_picks(league: str, picks: list[dict], history_first: bool) -> None:
        for p in picks:
            pid = str(p.get("id") or p.get("pick_id") or "").strip()
            cand = _daily_pick_id(p, league)
            mid = str(p.get("match_id") or p.get("id") or "").strip()
            utc = str(p.get("utcDate") or "")
            triple = (league, mid, utc)
            payload = (league, p)

            if pid:
                if history_first or pid not in by_pick_id:
                    by_pick_id[pid] = payload
            if cand and cand != "unknown":
                if history_first or cand not in by_pick_id:
                    by_pick_id[cand] = payload
            if mid and utc:
                if history_first or triple not in by_triple:
                    by_triple[triple] = payload

    league_codes = list((getattr(settings, "leagues", {}) or {}).keys())
    for lg in league_codes:
        add_from_picks(lg, _as_pick_list(read_json(f"picks_history_{lg}.json")), True)
    for lg in league_codes:
        add_from_picks(lg, _as_pick_list(read_json(f"daily_picks_{lg}.json")), False)

    for lg in league_codes:
        for m in _as_pick_list(read_json(f"daily_matches_{lg}.json")):
            mid = _safe_int(m.get("match_id") or m.get("id"))
            if mid is not None:
                matches_idx[(lg, mid)] = m
            home_key = _team_norm_key(m.get("home_team") or m.get("home"))
            away_key = _team_norm_key(m.get("away_team") or m.get("away"))
            if home_key and away_key:
                matches_by_team.setdefault((lg, home_key, away_key), []).append(m)

    return by_pick_id, by_triple, matches_idx, matches_by_team


def _get_resolution_maps_cached() -> tuple[
    dict[str, tuple[str, dict]],
    dict[tuple[str, str, str], tuple[str, dict]],
    dict[tuple[str, int], dict],
    dict[tuple[str, str, str], list[dict]],
]:
    global _RESOLUTION_MAPS, _RESOLUTION_MAPS_MONO
    now = time.monotonic()
    if _RESOLUTION_MAPS is not None and (now - _RESOLUTION_MAPS_MONO) < _RESOLUTION_MAPS_TTL_SEC:
        return _RESOLUTION_MAPS
    _RESOLUTION_MAPS = _pick_resolution_maps()
    _RESOLUTION_MAPS_MONO = now
    return _RESOLUTION_MAPS


def _lookup_pick_record(
    pick_id: str,
    by_pick_id: dict[str, tuple[str, dict]],
    by_triple: dict[tuple[str, str, str], tuple[str, dict]],
) -> tuple[str, dict] | None:
    pid = str(pick_id).strip()
    if pid in by_pick_id:
        return by_pick_id[pid]
    parts = pid.split("|")
    if len(parts) == 4:
        key = (parts[0].strip(), parts[1].strip(), parts[3].strip())
        if key in by_triple:
            return by_triple[key]
    return None


def _user_picks_extra_columns(cur) -> set[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'user_picks' AND table_schema = 'public'"
    )
    return {str(r["column_name"]) for r in cur.fetchall()}


def _sync_followed_picks_for_user(uid: int) -> None:
    """Persist WIN/LOSS/PUSH onto user_picks using picks_history / daily_picks / daily_matches."""
    by_id, by_triple, matches_idx, matches_by_team = _get_resolution_maps_cached()
    settled_at = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cols = _user_picks_extra_columns(cur)
        has_scores = "score_home" in cols and "score_away" in cols
        has_settled = "settled_at" in cols
        has_status = "status" in cols
        cur.execute(
            "SELECT pick_id, market, COALESCE(result, '') AS result FROM user_picks WHERE user_id = %s",
            (uid,),
        )
        for row in cur.fetchall():
            if _norm_result(row["result"]) not in ("", "PENDING"):
                continue
            pick_id = row["pick_id"]
            got = _lookup_pick_record(str(pick_id), by_id, by_triple)
            league: str | None = None
            r = "PENDING"
            hs: int | None = None
            aa: int | None = None

            if got:
                league, p = got
                r, hs, aa = _settle_from_pick_and_match(league, p, row["market"], matches_idx, matches_by_team)
            else:
                # Reconstruct from composite pick_id when we can't resolve pick metadata from JSON caches.
                # Expected: league|match_id|market|utc
                parts = str(pick_id).split("|")
                if len(parts) == 4:
                    lg = (parts[0] or "").strip()
                    mid_s = (parts[1] or "").strip()
                    mkt = (parts[2] or "").strip()
                    mid = _safe_int(mid_s)
                    if lg and mid is not None:
                        match = matches_idx.get((lg, mid))
                        if isinstance(match, dict) and _dashboard_match_finished(match):
                            hs2, aa2 = _extract_score(match)
                            if hs2 is not None and aa2 is not None:
                                league = lg
                                r = _evaluate_market_for_league(lg, mkt, hs2, aa2)
                                hs, aa = hs2, aa2

            if r == "PENDING":
                continue
            if has_scores and has_settled:
                if has_status:
                    cur.execute(
                        """UPDATE user_picks SET result = %s, score_home = %s, score_away = %s, settled_at = %s, status = %s
                           WHERE user_id = %s AND pick_id = %s""",
                        (r, hs, aa, settled_at, "SETTLED", uid, pick_id),
                    )
                else:
                    cur.execute(
                        """UPDATE user_picks SET result = %s, score_home = %s, score_away = %s, settled_at = %s
                           WHERE user_id = %s AND pick_id = %s""",
                        (r, hs, aa, settled_at, uid, pick_id),
                    )
            elif has_settled:
                if has_status:
                    cur.execute(
                        "UPDATE user_picks SET result = %s, settled_at = %s, status = %s WHERE user_id = %s AND pick_id = %s",
                        (r, settled_at, "SETTLED", uid, pick_id),
                    )
                else:
                    cur.execute(
                        "UPDATE user_picks SET result = %s, settled_at = %s WHERE user_id = %s AND pick_id = %s",
                        (r, settled_at, uid, pick_id),
                    )
            else:
                if has_status:
                    cur.execute(
                        "UPDATE user_picks SET result = %s, status = %s WHERE user_id = %s AND pick_id = %s",
                        (r, "SETTLED", uid, pick_id),
                    )
                else:
                    cur.execute(
                        "UPDATE user_picks SET result = %s WHERE user_id = %s AND pick_id = %s",
                        (r, uid, pick_id),
                    )
        conn.commit()
    finally:
        put_conn(conn)


def _resolved_row_for_favorite(
    pick_id: str,
    market: str | None,
    by_id: dict[str, tuple[str, dict]],
    by_triple: dict[tuple[str, str, str], tuple[str, dict]],
    matches_idx: dict[tuple[str, int], dict],
    matches_by_team: dict[tuple[str, str, str], list[dict]],
) -> tuple[str, str | None]:
    """(result, final_score str or None) for display; favorites have no result column in DB."""
    got = _lookup_pick_record(str(pick_id), by_id, by_triple)
    if not got:
        # Attempt reconstruction from composite pick_id
        parts = str(pick_id).split("|")
        if len(parts) == 4:
            lg = (parts[0] or "").strip()
            mid = _safe_int((parts[1] or "").strip())
            mkt_from_id = (parts[2] or "").strip()
            if lg and mid is not None:
                match = matches_idx.get((lg, mid))
                if isinstance(match, dict) and _dashboard_match_finished(match):
                    hs2, aa2 = _extract_score(match)
                    if hs2 is not None and aa2 is not None:
                        ev = _evaluate_market_for_league(lg, market or mkt_from_id, hs2, aa2)
                        return ev, f"{hs2}-{aa2}"
        return "PENDING", None

    league, p = got
    r, hs, aa = _settle_from_pick_and_match(league, p, market, matches_idx, matches_by_team)
    if r == "PENDING" or hs is None or aa is None:
        return r, None
    return r, f"{hs}-{aa}"


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
    """followed_picks, favorites_count, wins, losses, push, pending, roi, winrate inputs."""
    uid, err = _require_user(request)
    if err is not None:
        return err
    _sync_followed_picks_for_user(uid)
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS n FROM user_favorites WHERE user_id = %s",
            (uid,),
        )
        favorites_count = cur.fetchone()["n"]

        cur.execute(
            "SELECT COUNT(*) AS n FROM user_picks WHERE user_id = %s",
            (uid,),
        )
        followed_picks = cur.fetchone()["n"]

        cur.execute(
            """SELECT result, COUNT(*) AS n FROM user_picks WHERE user_id = %s
               GROUP BY COALESCE(result, 'PENDING')""",
            (uid,),
        )
        by_result = {str(row["result"] or "PENDING"): row["n"] for row in cur.fetchall()}
        wins = by_result.get("WIN", 0)
        losses = by_result.get("LOSS", 0)
        push = by_result.get("PUSH", 0)
        pending = by_result.get("PENDING", 0)

        cur.execute(
            """SELECT result FROM user_picks
               WHERE user_id = %s AND result IN ('WIN', 'LOSS', 'PUSH')
               ORDER BY created_at DESC""",
            (uid,),
        )
        streak_count = 0
        streak_kind: str | None = None
        for row in cur.fetchall():
            r = str(row["result"]).upper()
            if r == "PUSH":
                continue
            if streak_kind is None:
                streak_kind = r
                streak_count = 1
            elif r == streak_kind:
                streak_count += 1
            else:
                break
    finally:
        put_conn(conn)

    settled = wins + losses + push
    roi = None
    if settled > 0:
        roi = round((wins - losses) / settled * 100.0, 2)
    winrate = None
    wl = wins + losses
    if wl > 0:
        winrate = round(wins / wl * 100.0, 2)

    return JSONResponse({
        "ok": True,
        "stats": {
            "followed_picks": followed_picks,
            "favorites_count": favorites_count,
            "wins": wins,
            "losses": losses,
            "push": push,
            "pending": pending,
            "roi": roi,
            "winrate": winrate,
            "streak_count": streak_count,
            "streak_kind": streak_kind,
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
            """INSERT INTO user_favorites
               (user_id, pick_id, created_at, market, aftr_score, tier, edge, home_team, away_team)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT(user_id, pick_id) DO NOTHING""",
            (uid, pick_id, now, market, aftr_score, tier, edge, home_team, away_team),
        )
        cur.execute(
            """UPDATE user_favorites SET
               market = COALESCE(%s, market), aftr_score = COALESCE(%s, aftr_score),
               tier = COALESCE(%s, tier), edge = COALESCE(%s, edge),
               home_team = COALESCE(%s, home_team), away_team = COALESCE(%s, away_team)
               WHERE user_id = %s AND pick_id = %s""",
            (market, aftr_score, tier, edge, home_team, away_team, uid, pick_id),
        )
        conn.commit()
    finally:
        put_conn(conn)
    return JSONResponse({"ok": True, "pick_id": pick_id})


@router.get("/favorites")
def user_favorites(request: Request):
    """List favorites for the current user with optional market, aftr_score, tier, edge."""
    uid, err = _require_user(request)
    if err is not None:
        return err
    try:
        by_id, by_triple, matches_idx, matches_by_team = _get_resolution_maps_cached()
    except Exception:
        by_id, by_triple, matches_idx, matches_by_team = {}, {}, {}, {}
    items: list[dict] = []
    try:
        conn = get_conn()
        try:
            cur = conn.cursor()

            # PostgreSQL schema is always fully up-to-date (no legacy column guards needed)
            base_cols = ["pick_id", "created_at", "market", "aftr_score", "tier", "edge", "home_team", "away_team"]
            sql = "SELECT " + ", ".join(base_cols) + " FROM user_favorites WHERE user_id = %s ORDER BY created_at DESC"
            cur.execute(sql, (uid,))
            rows = cur.fetchall()

            for row in rows:
                r = dict(row)
                pick_id = r.get("pick_id")
                home = r.get("home_team")
                away = r.get("away_team")

                if home is None or away is None:
                    cur.execute(
                        "SELECT home_team, away_team FROM user_picks WHERE user_id = %s AND pick_id = %s LIMIT 1",
                        (uid, pick_id),
                    )
                    rr = cur.fetchone()
                    if rr:
                        rr_d = dict(rr)
                        home = rr_d.get("home_team")
                        away = rr_d.get("away_team")

                fres, fscore = _resolved_row_for_favorite(
                    str(pick_id), r.get("market"), by_id, by_triple, matches_idx, matches_by_team
                )
                items.append({
                    "pick_id": pick_id,
                    "created_at": r.get("created_at"),
                    "market": r.get("market"),
                    "aftr_score": r.get("aftr_score"),
                    "tier": r.get("tier"),
                    "edge": r.get("edge"),
                    "home": "" if home is None else str(home),
                    "away": "" if away is None else str(away),
                    "result": fres,
                    "final_score": fscore,
                })
        finally:
            put_conn(conn)
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
            "DELETE FROM user_favorites WHERE user_id = %s AND pick_id = %s",
            (uid, pick_id),
        )
        conn.commit()
        deleted = cur.rowcount
    finally:
        put_conn(conn)
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
            "SELECT pick_id FROM user_picks WHERE user_id = %s",
            (uid,),
        )
        rows = cur.fetchall()
    finally:
        put_conn(conn)
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
            """INSERT INTO user_picks
               (user_id, pick_id, action, result, created_at, market, aftr_score, tier, edge, home_team, away_team)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT(user_id, pick_id) DO NOTHING""",
            (uid, pick_id, "follow", "PENDING", now, market, aftr_score, tier, edge, home_team, away_team),
        )
        cur.execute(
            """UPDATE user_picks SET
               market = COALESCE(%s, market), aftr_score = COALESCE(%s, aftr_score),
               tier = COALESCE(%s, tier), edge = COALESCE(%s, edge),
               home_team = COALESCE(%s, home_team), away_team = COALESCE(%s, away_team)
               WHERE user_id = %s AND pick_id = %s""",
            (market, aftr_score, tier, edge, home_team, away_team, uid, pick_id),
        )
        conn.commit()
    finally:
        put_conn(conn)

    # Confirmation email (background, non-blocking)
    try:
        _u = get_user_by_id(uid)
        if _u and _u.get("email"):
            import threading
            from app.email_utils import send_pick_follow_email
            _uname = (_u.get("username") or _u.get("email", "").split("@")[0] or "").strip()
            threading.Thread(
                target=send_pick_follow_email,
                args=(_u["email"], _uname, home_team or "", away_team or "", market or "", aftr_score, tier, None),
                daemon=True,
            ).start()
    except Exception:
        pass

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
            "DELETE FROM user_picks WHERE user_id = %s AND pick_id = %s",
            (uid, pick_id),
        )
        conn.commit()
        deleted = cur.rowcount
    finally:
        put_conn(conn)
    return JSONResponse({"ok": True, "removed": deleted > 0, "pick_id": pick_id})


@router.get("/history")
def user_history(request: Request):
    """Followed picks for the current user, newest first. Limit 10. Includes market, aftr_score, tier, edge, result."""
    uid, err = _require_user(request)
    if err is not None:
        return err
    try:
        _sync_followed_picks_for_user(uid)
    except Exception:
        pass
    items: list[dict] = []
    try:
        conn = get_conn()
        try:
            cur = conn.cursor()

            # PostgreSQL schema always has home_team/away_team
            base_cols = ["id", "pick_id", "action", "result", "created_at", "market", "aftr_score", "tier", "edge", "home_team", "away_team"]
            extra_cols = _user_picks_extra_columns(cur)
            if "score_home" in extra_cols and "score_away" in extra_cols:
                base_cols += ["score_home", "score_away"]
            if "settled_at" in extra_cols:
                base_cols += ["settled_at"]
            sql = "SELECT " + ", ".join(base_cols) + " FROM user_picks WHERE user_id = %s ORDER BY created_at DESC LIMIT 50"
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
                home = r.get("home_team")
                away = r.get("away_team")

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
            put_conn(conn)
    except Exception:
        items = []

    return JSONResponse({"ok": True, "history": items})


@router.post("/favorite-team")
def user_set_favorite_team(request: Request, payload: dict = Body(...)):
    """Save the user's favorite team (team_id, team_name, team_crest)."""
    uid, err = _require_user(request)
    if err:
        return err
    team_id    = str(payload.get("team_id") or "").strip()
    team_name  = str(payload.get("team_name") or "").strip()[:120]
    team_crest = str(payload.get("team_crest") or "").strip()[:300]
    if not team_name:
        return JSONResponse({"ok": False, "error": "team_name required"}, status_code=400)
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE users SET favorite_team_id=%s, favorite_team_name=%s, favorite_team_crest=%s
               WHERE id=%s""",
            (team_id or None, team_name, team_crest or None, uid),
        )
        conn.commit()
    finally:
        put_conn(conn)
    return JSONResponse({"ok": True, "team_name": team_name})


@router.get("/available-teams")
def user_available_teams(request: Request):
    """Return all unique teams found across cached picks/matches + DB history for team selector.
    No auth required — team names are public data."""
    from data.cache import read_json_with_fallback
    from config.settings import settings
    seen: dict[str, dict] = {}

    def _add(name: str | None, crest: str | None, team_id: str = "") -> None:
        if not name or not isinstance(name, str) or not name.strip():
            return
        key = name.strip().lower()
        if key not in seen:
            seen[key] = {"team_name": name.strip(), "team_crest": crest or "", "team_id": team_id}

    # 1. From JSON cache files (fastest, most complete when populated)
    for code in settings.league_codes():
        for source in [f"daily_matches_{code}.json", f"daily_picks_{code}.json"]:
            items = read_json_with_fallback(source)
            if not isinstance(items, list):
                continue
            for item in items:
                _add(item.get("home_team") or item.get("home"),
                     item.get("home_crest"),
                     str(item.get("home_id") or ""))
                _add(item.get("away_team") or item.get("away"),
                     item.get("away_crest"),
                     str(item.get("away_id") or ""))

    # 2. Fallback: from DB user_picks/user_favorites (always available, no crest)
    if len(seen) < 10:
        try:
            conn = get_conn()
            try:
                cur = conn.cursor()
                cur.execute("SELECT DISTINCT home_team, away_team FROM user_picks WHERE home_team IS NOT NULL LIMIT 500")
                for row in cur.fetchall():
                    _add(row.get("home_team"), None)
                    _add(row.get("away_team"), None)
                cur.execute("SELECT DISTINCT home_team, away_team FROM user_favorites WHERE home_team IS NOT NULL LIMIT 500")
                for row in cur.fetchall():
                    _add(row.get("home_team"), None)
                    _add(row.get("away_team"), None)
            finally:
                put_conn(conn)
        except Exception:
            pass

    teams = sorted(seen.values(), key=lambda t: t["team_name"])
    return JSONResponse({"ok": True, "teams": teams})


# ─── Bankroll ────────────────────────────────────────────────────────────────

@router.get("/bankroll")
def user_bankroll_get(request: Request):
    """Return bankroll settings + computed current balance. Premium only."""
    uid, err = _require_user(request)
    if err:
        return err
    from app.user_helpers import is_premium_active
    user = get_user_by_id(uid)
    if not user or not is_premium_active(user):
        return JSONResponse({"ok": False, "error": "premium_required"}, status_code=403)

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM bankroll_settings WHERE user_id = %s", (uid,))
        settings_row = cur.fetchone()
        cur.execute(
            "SELECT result, edge FROM user_picks WHERE user_id = %s AND result IN ('WIN','LOSS','PUSH')",
            (uid,),
        )
        resolved = cur.fetchall()
    finally:
        put_conn(conn)

    initial = float(settings_row["initial_amount"]) if settings_row else 10000.0
    stake   = float(settings_row["stake_per_unit"]) if settings_row else 1000.0
    currency = settings_row["currency"] if settings_row else "ARS"

    # P&L: WIN=+1u, LOSS=-1u, PUSH=0u (multiplied by stake_per_unit)
    pnl_units = 0.0
    for row in resolved:
        r = (row.get("result") or "").upper()
        if r == "WIN":
            pnl_units += 1.0
        elif r == "LOSS":
            pnl_units -= 1.0
    pnl_money = round(pnl_units * stake, 2)
    current = round(initial + pnl_money, 2)

    return JSONResponse({
        "ok": True,
        "initial_amount": initial,
        "stake_per_unit": stake,
        "currency": currency,
        "current_bankroll": current,
        "total_pnl": pnl_money,
        "total_picks_settled": len(resolved),
        "configured": settings_row is not None,
    })


@router.post("/bankroll")
def user_bankroll_post(request: Request, payload: dict = Body(...)):
    """Save/update bankroll settings. Premium only."""
    uid, err = _require_user(request)
    if err:
        return err
    from app.user_helpers import is_premium_active
    user = get_user_by_id(uid)
    if not user or not is_premium_active(user):
        return JSONResponse({"ok": False, "error": "premium_required"}, status_code=403)

    try:
        initial = float(payload.get("initial_amount", 10000))
        stake   = float(payload.get("stake_per_unit", 1000))
        if initial <= 0 or stake <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "invalid_values"}, status_code=400)

    currency = str(payload.get("currency", "ARS"))[:8]
    now_str  = datetime.now(timezone.utc).isoformat()

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO bankroll_settings (user_id, initial_amount, stake_per_unit, currency, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (user_id) DO UPDATE SET
                 initial_amount = EXCLUDED.initial_amount,
                 stake_per_unit = EXCLUDED.stake_per_unit,
                 currency       = EXCLUDED.currency,
                 updated_at     = EXCLUDED.updated_at""",
            (uid, initial, stake, currency, now_str, now_str),
        )
        conn.commit()
    finally:
        put_conn(conn)

    return JSONResponse({"ok": True})
