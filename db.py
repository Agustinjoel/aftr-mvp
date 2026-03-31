from contextlib import contextmanager
from typing import Any, Dict, List, Optional
import json
from datetime import datetime

import psycopg2
import psycopg2.extras

from config.settings import DATABASE_URL
from core.evaluation import evaluate_market


@contextmanager
def get_conn():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        cur = conn.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            league       TEXT NOT NULL,
            match_id     INTEGER NOT NULL,
            "utcDate"    TEXT,
            status       TEXT,
            home         TEXT,
            away         TEXT,
            home_goals   INTEGER,
            away_goals   INTEGER,
            last_updated TEXT,
            PRIMARY KEY (league, match_id)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS picks (
            league          TEXT NOT NULL,
            match_id        INTEGER NOT NULL,
            created_at      TEXT NOT NULL,
            xg_home         REAL,
            xg_away         REAL,
            xg_total        REAL,
            probs_json      TEXT,
            candidates_json TEXT,
            best_market     TEXT,
            best_prob       REAL,
            best_fair       REAL,
            result          TEXT,
            result_reason   TEXT,
            PRIMARY KEY (league, match_id)
        )
        """)

        cur.execute("CREATE INDEX IF NOT EXISTS idx_matches_status ON matches(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_matches_date ON matches(\"utcDate\")")


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
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO matches (league, match_id, "utcDate", status, home, away, home_goals, away_goals, last_updated)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(league, match_id) DO UPDATE SET
                "utcDate"    = EXCLUDED."utcDate",
                status       = EXCLUDED.status,
                home         = EXCLUDED.home,
                away         = EXCLUDED.away,
                home_goals   = EXCLUDED.home_goals,
                away_goals   = EXCLUDED.away_goals,
                last_updated = EXCLUDED.last_updated
            """,
            (
                league,
                match_id,
                utcDate,
                status,
                home,
                away,
                home_goals,
                away_goals,
                datetime.utcnow().isoformat(),
            ),
        )


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
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO picks (
                league, match_id, created_at,
                xg_home, xg_away, xg_total,
                probs_json, candidates_json,
                best_market, best_prob, best_fair,
                result, result_reason
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'PENDING', NULL)
            ON CONFLICT(league, match_id) DO UPDATE SET
                created_at      = EXCLUDED.created_at,
                xg_home         = EXCLUDED.xg_home,
                xg_away         = EXCLUDED.xg_away,
                xg_total        = EXCLUDED.xg_total,
                probs_json      = EXCLUDED.probs_json,
                candidates_json = EXCLUDED.candidates_json,
                best_market     = EXCLUDED.best_market,
                best_prob       = EXCLUDED.best_prob,
                best_fair       = EXCLUDED.best_fair
            """,
            (
                league,
                match_id,
                created_at,
                xg_home,
                xg_away,
                xg_total,
                probs_json,
                candidates_json,
                best_market,
                best_prob,
                best_fair,
            ),
        )


def evaluate_finished_picks():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT p.league, p.match_id, p.best_market, m.home_goals, m.away_goals, m.status
            FROM picks p
            JOIN matches m ON m.league = p.league AND m.match_id = p.match_id
            WHERE p.result = 'PENDING' AND m.status = 'FINISHED'
        """)
        rows = cur.fetchall()

        for r in rows:
            league = r["league"]
            match_id = r["match_id"]
            market = r["best_market"] or ""
            hg = r["home_goals"]
            ag = r["away_goals"]

            res, reason = evaluate_market(market, hg or 0, ag or 0)

            cur.execute(
                """
                UPDATE picks SET result = %s, result_reason = %s
                WHERE league = %s AND match_id = %s
                """,
                (res, reason, league, match_id),
            )


def fetch_sections(league: str) -> Dict[str, List[Dict[str, Any]]]:
    with get_conn() as conn:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT m.*, p.best_market, p.best_prob, p.best_fair, p.result, p.result_reason,
                   p.xg_home, p.xg_away, p.xg_total
            FROM matches m
            LEFT JOIN picks p ON p.league = m.league AND p.match_id = m.match_id
            WHERE m.league = %s AND m.status IN ('IN_PLAY', 'PAUSED')
            ORDER BY m."utcDate" ASC
            """,
            (league,),
        )
        live = cur.fetchall()

        cur.execute(
            """
            SELECT m.*, p.best_market, p.best_prob, p.best_fair, p.result, p.result_reason,
                   p.xg_home, p.xg_away, p.xg_total
            FROM matches m
            LEFT JOIN picks p ON p.league = m.league AND p.match_id = m.match_id
            WHERE m.league = %s AND m.status IN ('SCHEDULED', 'TIMED')
            ORDER BY m."utcDate" ASC
            LIMIT 50
            """,
            (league,),
        )
        upcoming = cur.fetchall()

        cur.execute(
            """
            SELECT m.*, p.best_market, p.best_prob, p.best_fair, p.result, p.result_reason,
                   p.xg_home, p.xg_away, p.xg_total
            FROM matches m
            LEFT JOIN picks p ON p.league = m.league AND p.match_id = m.match_id
            WHERE m.league = %s AND m.status = 'FINISHED'
            ORDER BY m."utcDate" DESC
            LIMIT 30
            """,
            (league,),
        )
        recent = cur.fetchall()

    def to_list(rs):
        return [dict(r) for r in rs]

    return {
        "live": to_list(live),
        "upcoming": to_list(upcoming),
        "recent": to_list(recent),
    }


def get_last_updated() -> Optional[str]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT MAX(last_updated) AS lu FROM matches")
        r = cur.fetchone()
        return r["lu"] if r and r["lu"] else None
