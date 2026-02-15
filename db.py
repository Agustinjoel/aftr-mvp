import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple
import json
from datetime import datetime

DB_PATH = os.getenv("DB_PATH", "aftr.db")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            league TEXT NOT NULL,
            match_id INTEGER NOT NULL,
            utcDate TEXT,
            status TEXT,
            home TEXT,
            away TEXT,
            home_goals INTEGER,
            away_goals INTEGER,
            last_updated TEXT,
            PRIMARY KEY (league, match_id)
        );
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS picks (
            league TEXT NOT NULL,
            match_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            xg_home REAL,
            xg_away REAL,
            xg_total REAL,
            probs_json TEXT,
            candidates_json TEXT,
            best_market TEXT,
            best_prob REAL,
            best_fair REAL,
            result TEXT,           -- WIN / LOSS / PUSH / PENDING
            result_reason TEXT,    -- texto corto
            PRIMARY KEY (league, match_id)
        );
        """)

        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_matches_status ON matches(status);
        """)

        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_matches_date ON matches(utcDate);
        """)


def upsert_match(
    league: str,
    match_id: int,
    utcDate: str,
    status: str,
    home: str,
    away: str,
    home_goals: Optional[int],
    away_goals: Optional[int],
):
    with get_conn() as conn:
        conn.execute("""
        INSERT INTO matches (league, match_id, utcDate, status, home, away, home_goals, away_goals, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(league, match_id) DO UPDATE SET
            utcDate=excluded.utcDate,
            status=excluded.status,
            home=excluded.home,
            away=excluded.away,
            home_goals=excluded.home_goals,
            away_goals=excluded.away_goals,
            last_updated=excluded.last_updated
        """, (
            league, match_id, utcDate, status, home, away,
            home_goals, away_goals,
            datetime.utcnow().isoformat()
        ))


def upsert_pick(
    league: str,
    match_id: int,
    xg_home: float,
    xg_away: float,
    xg_total: float,
    probs: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    best_market: Optional[str],
    best_prob: Optional[float],
    best_fair: Optional[float],
):
    created_at = datetime.utcnow().isoformat()
    probs_json = json.dumps(probs, ensure_ascii=False)
    candidates_json = json.dumps(candidates, ensure_ascii=False)

    with get_conn() as conn:
        conn.execute("""
        INSERT INTO picks (
            league, match_id, created_at,
            xg_home, xg_away, xg_total,
            probs_json, candidates_json,
            best_market, best_prob, best_fair,
            result, result_reason
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', NULL)
        ON CONFLICT(league, match_id) DO UPDATE SET
            created_at=excluded.created_at,
            xg_home=excluded.xg_home,
            xg_away=excluded.xg_away,
            xg_total=excluded.xg_total,
            probs_json=excluded.probs_json,
            candidates_json=excluded.candidates_json,
            best_market=excluded.best_market,
            best_prob=excluded.best_prob,
            best_fair=excluded.best_fair
        """, (
            league, match_id, created_at,
            xg_home, xg_away, xg_total,
            probs_json, candidates_json,
            best_market, best_prob, best_fair
        ))


def _evaluate_market(market: str, hg: int, ag: int) -> Tuple[str, str]:
    total = (hg or 0) + (ag or 0)
    m = (market or "").lower().strip()

    if "under 2.5" in m:
        if total <= 2:
            return ("WIN", f"Total {total} (<=2)")
        return ("LOSS", f"Total {total} (>=3)")

    if "over 2.5" in m:
        if total >= 3:
            return ("WIN", f"Total {total} (>=3)")
        return ("LOSS", f"Total {total} (<=2)")

    if "btts yes" in m or "ambos marcan" in m:
        if (hg or 0) >= 1 and (ag or 0) >= 1:
            return ("WIN", f"HG {hg} / AG {ag}")
        return ("LOSS", f"HG {hg} / AG {ag}")

    if "btts no" in m:
        if (hg or 0) == 0 or (ag or 0) == 0:
            return ("WIN", f"HG {hg} / AG {ag}")
        return ("LOSS", f"HG {hg} / AG {ag}")

    if "home win" in m or "local" in m:
        if (hg or 0) > (ag or 0):
            return ("WIN", f"{hg}-{ag}")
        return ("LOSS", f"{hg}-{ag}")

    if "away win" in m or "visitante" in m:
        if (ag or 0) > (hg or 0):
            return ("WIN", f"{hg}-{ag}")
        return ("LOSS", f"{hg}-{ag}")

    if "draw" in m or "empate" in m:
        if (hg or 0) == (ag or 0):
            return ("WIN", f"{hg}-{ag}")
        return ("LOSS", f"{hg}-{ag}")

    # Si algún mercado raro, no lo penalizamos, lo dejamos pendiente
    return ("PUSH", "Market not supported")


def evaluate_finished_picks():
    """
    Busca picks PENDING cuya match esté FINISHED y calcula WIN/LOSS.
    """
    with get_conn() as conn:
        rows = conn.execute("""
        SELECT p.league, p.match_id, p.best_market, m.home_goals, m.away_goals, m.status
        FROM picks p
        JOIN matches m ON m.league = p.league AND m.match_id = p.match_id
        WHERE p.result = 'PENDING' AND m.status = 'FINISHED'
        """).fetchall()

        for r in rows:
            league = r["league"]
            match_id = r["match_id"]
            market = r["best_market"] or ""
            hg = r["home_goals"]
            ag = r["away_goals"]

            res, reason = _evaluate_market(market, hg or 0, ag or 0)

            conn.execute("""
            UPDATE picks
            SET result = ?, result_reason = ?
            WHERE league = ? AND match_id = ?
            """, (res, reason, league, match_id))


def fetch_sections(league: str) -> Dict[str, List[Dict[str, Any]]]:
    """
    Devuelve LIVE / UPCOMING / RECENT con info de pick (si existe).
    """
    with get_conn() as conn:
        live = conn.execute("""
        SELECT m.*, p.best_market, p.best_prob, p.best_fair, p.result, p.result_reason,
               p.xg_home, p.xg_away, p.xg_total
        FROM matches m
        LEFT JOIN picks p ON p.league = m.league AND p.match_id = m.match_id
        WHERE m.league = ? AND m.status IN ('IN_PLAY', 'PAUSED')
        ORDER BY m.utcDate ASC
        """, (league,)).fetchall()

        upcoming = conn.execute("""
        SELECT m.*, p.best_market, p.best_prob, p.best_fair, p.result, p.result_reason,
               p.xg_home, p.xg_away, p.xg_total
        FROM matches m
        LEFT JOIN picks p ON p.league = m.league AND p.match_id = m.match_id
        WHERE m.league = ? AND m.status IN ('SCHEDULED', 'TIMED')
        ORDER BY m.utcDate ASC
        LIMIT 50
        """, (league,)).fetchall()

        recent = conn.execute("""
        SELECT m.*, p.best_market, p.best_prob, p.best_fair, p.result, p.result_reason,
               p.xg_home, p.xg_away, p.xg_total
        FROM matches m
        LEFT JOIN picks p ON p.league = m.league AND p.match_id = m.match_id
        WHERE m.league = ? AND m.status = 'FINISHED'
        ORDER BY m.utcDate DESC
        LIMIT 30
        """, (league,)).fetchall()

    def to_list(rs):
        out = []
        for r in rs:
            out.append({k: r[k] for k in r.keys()})
        return out

    return {"live": to_list(live), "upcoming": to_list(upcoming), "recent": to_list(recent)}


def get_last_updated() -> Optional[str]:
    with get_conn() as conn:
        r = conn.execute("SELECT MAX(last_updated) AS lu FROM matches").fetchone()
        return r["lu"] if r and r["lu"] else None
