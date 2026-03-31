import threading
from typing import Iterable

import psycopg2.extras

from fastapi import APIRouter, Query

from config.settings import settings
from data.cache import read_json


router = APIRouter()

# Cache de schema de la tabla picks (nunca cambia en runtime sin reinicio)
_picks_columns_cache: set[str] | None = None
_picks_columns_lock = threading.Lock()


def _get_picks_columns(conn) -> set[str]:
    global _picks_columns_cache
    if _picks_columns_cache is not None:
        return _picks_columns_cache
    with _picks_columns_lock:
        if _picks_columns_cache is not None:
            return _picks_columns_cache
        cur = conn.cursor()
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'picks' AND table_schema = 'public'"
        )
        rows = cur.fetchall()
        _picks_columns_cache = {row["column_name"] for row in rows} if rows else set()
        return _picks_columns_cache


# =========================
# JSON summary (simple)
# =========================
def summary_from_json(league: str) -> dict:
    picks = read_json(f"daily_picks_{league}.json") or []

    wins = sum(1 for p in picks if (p.get("result") == "WIN"))
    losses = sum(1 for p in picks if (p.get("result") == "LOSS"))
    push = sum(1 for p in picks if (p.get("result") == "PUSH"))
    pending = sum(1 for p in picks if (p.get("result") in (None, "", "PENDING")))

    settled = wins + losses + push

    # Unidades aproximadas:
    # WIN = best_fair - 1, LOSS = -1, PUSH = 0
    net_units = 0.0
    for p in picks:
        res = (p.get("result") or "PENDING").upper()
        fair = float(p.get("best_fair") or 0.0)
        if res == "WIN":
            net_units += max(fair - 1.0, 0.0)
        elif res == "LOSS":
            net_units -= 1.0

    roi = (net_units / settled * 100.0) if settled > 0 else 0.0
    winrate = (wins / settled * 100.0) if settled > 0 else 0.0

    return {
        "league": league,
        "source": "json",
        "total_picks": len(picks),
        "wins": wins,
        "losses": losses,
        "push": push,
        "pending": pending,
        "winrate": round(winrate, 2),
        "roi": round(roi, 2),
        "yield": round(roi, 2),
        "net_units": round(net_units, 2),
    }


# =========================
# API: Combos globales  -> /api/combos (mounted with prefix /api)
# =========================
@router.get("/combos")
def api_combos():
    return read_json("daily_combos.json") or {}


# =========================
# API: Picks por liga  -> /api/picks?league=PL and /api/picks/PL
# =========================
@router.get("/picks")
def get_picks(league: str = Query(settings.default_league)):
    league = league if settings.is_valid_league(league) else settings.default_league
    return read_json(f"daily_picks_{league}.json") or []


@router.get("/picks/{league}")
def get_picks_by_league(league: str):
    league = league if settings.is_valid_league(league) else settings.default_league
    return read_json(f"daily_picks_{league}.json") or []


# =========================
# API: Summary para tu KPI bar  -> /api/stats/summary
# =========================
@router.get("/stats/summary")
def get_stats_summary(league: str = Query(settings.default_league)):
    league = league if settings.is_valid_league(league) else settings.default_league
    return summary_from_json(league)


# =========================================================
# (Opcional) soporte SQLite a futuro
# lo dejo acá por si lo querés usar después.
# =========================================================
def _safe_odds(fair: float | None, prob: float | None) -> float | None:
    if fair and fair > 1:
        return float(fair)
    if prob and prob > 0:
        return 1 / float(prob)
    return None


def _read_pick_rows(league: str, db_path: str | None = None) -> tuple[list, str]:
    """
    Returns rows + source tag from PostgreSQL.
    source: pg | json
    """
    import psycopg2
    from config.settings import DATABASE_URL
    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cols = _get_picks_columns(conn)
            if not cols:
                return [], "json"
            cur = conn.cursor()
            if "result" in cols:
                cur.execute(
                    "SELECT result, best_fair, best_prob FROM picks WHERE league = %s",
                    (league,),
                )
                return cur.fetchall(), "pg"
            if "status" in cols:
                cur.execute(
                    "SELECT status AS result, fair AS best_fair, prob AS best_prob FROM picks WHERE league = %s",
                    (league,),
                )
                return cur.fetchall(), "pg"
        finally:
            conn.close()
    except psycopg2.Error:
        pass

    return [], "json"


def _compute_metrics(rows: Iterable) -> dict[str, float | int]:
    total_picks = wins = losses = push = pending = 0
    net_units = 0.0

    for row in rows:
        total_picks += 1
        result = (row["result"] or "PENDING").upper()

        if result == "WIN":
            wins += 1
            odds = _safe_odds(row["best_fair"], row["best_prob"])
            if odds:
                net_units += odds - 1
        elif result == "LOSS":
            losses += 1
            net_units -= 1
        elif result == "PUSH":
            push += 1
        else:
            pending += 1

    decided = wins + losses
    settled = wins + losses + push

    return {
        "total_picks": total_picks,
        "wins": wins,
        "losses": losses,
        "push": push,
        "pending": pending,
        "winrate": round((wins / decided) * 100, 2) if decided else 0.0,
        "roi": round((net_units / settled) * 100, 2) if settled else 0.0,
        "yield": round((net_units / total_picks) * 100, 2) if total_picks else 0.0,
        "net_units": round(net_units, 2),
    }