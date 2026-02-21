import os
import sqlite3
from typing import Iterable

from fastapi import APIRouter, Query

from config.settings import settings
from data.cache import read_json

router = APIRouter()


@router.get("/picks")
def get_picks(league: str = Query(settings.default_league)):
    league = league if settings.is_valid_league(league) else settings.default_league
    return read_json(f"daily_picks_{league}.json")


def _safe_odds(fair: float | None, prob: float | None) -> float | None:
    if fair and fair > 1:
        return float(fair)
    if prob and prob > 0:
        return 1 / float(prob)
    return None


def _read_pick_rows(league: str, db_path: str) -> tuple[list[sqlite3.Row], str]:
    """
    Returns rows + source tag.
    source: sqlite | json
    """
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            table_info = conn.execute("PRAGMA table_info(picks)").fetchall()
            if not table_info:
                return [], "json"

            cols = {row["name"] for row in table_info}
            if "result" in cols:
                rows = conn.execute(
                    "SELECT result, best_fair, best_prob FROM picks WHERE league = ?",
                    (league,),
                ).fetchall()
                return rows, "sqlite"

            if "status" in cols:
                rows = conn.execute(
                    "SELECT status AS result, fair AS best_fair, prob AS best_prob FROM picks WHERE league = ?",
                    (league,),
                ).fetchall()
                return rows, "sqlite"
    except sqlite3.Error:
        pass

    return [], "json"


def _compute_metrics(rows: Iterable[sqlite3.Row]) -> dict[str, float | int]:
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


@router.get("/stats/summary")
def get_stats_summary(league: str = Query(settings.default_league)):
    league = league if settings.is_valid_league(league) else settings.default_league
    db_path = settings.db_path

    rows, source = _read_pick_rows(league, db_path)
    metrics = _compute_metrics(rows)

    # If DB returned no rows, mark fallback source as JSON.
    # This keeps semantics consistent even if JSON payload is missing/invalid,
    # and avoids depending on metric-calculation internals.
    if not rows:
        source = "json"
        picks = read_json(f"daily_picks_{league}.json")
        if isinstance(picks, list):
            metrics["total_picks"] = len(picks)
            metrics["pending"] = len(picks)

    return {
        "league": league,
        "source": source,
        **metrics,
    }