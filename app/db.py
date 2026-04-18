"""
AFTR user storage — PostgreSQL via psycopg2.

Tables: users, user_favorites, user_picks, subscriptions, password_reset_tokens.
Connection pool: ThreadedConnectionPool (min=1, max=10).
"""
from __future__ import annotations
import logging
import threading

import psycopg2
import psycopg2.extras
import psycopg2.pool
import psycopg2.errors

from config.settings import DATABASE_URL

logger = logging.getLogger("aftr.db")

# ---------------------------------------------------------------------------
# Connection pool (lazy-initialized once)
# ---------------------------------------------------------------------------
_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def _ensure_sslmode(url: str) -> str:
    """Agrega sslmode=require si no está presente. Obligatorio en Render Postgres."""
    if not url or "sslmode" in url:
        return url
    sep = "&" if "?" in url else "?"
    return url + sep + "sslmode=require"


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            dsn = _ensure_sslmode(DATABASE_URL)
            _pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=10,
                dsn=dsn,
                cursor_factory=psycopg2.extras.RealDictCursor,
            )
            logger.info("psycopg2 connection pool created (max=10) | %s", DATABASE_URL.split("@")[-1])
    return _pool


def get_conn() -> psycopg2.extensions.connection:
    """Borrow a connection from the pool. Caller MUST call put_conn(conn) or conn.close()."""
    return _get_pool().getconn()


def put_conn(conn: psycopg2.extensions.connection) -> None:
    """Return a borrowed connection to the pool."""
    try:
        _get_pool().putconn(conn)
    except Exception as e:
        logger.warning("put_conn: error returning connection: %s", e)


# ---------------------------------------------------------------------------
# Schema init
# ---------------------------------------------------------------------------

def init_db() -> None:
    logger.info("init_db: connecting to %s", DATABASE_URL.split("@")[-1])
    conn = get_conn()
    try:
        cur = conn.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id                   SERIAL PRIMARY KEY,
            email                TEXT NOT NULL UNIQUE,
            username             TEXT UNIQUE,
            password_hash        TEXT NOT NULL,
            role                 TEXT NOT NULL DEFAULT 'free_user',
            subscription_status  TEXT NOT NULL DEFAULT 'inactive',
            subscription_start   TEXT,
            subscription_end     TEXT,
            created_at           TEXT NOT NULL,
            updated_at           TEXT,
            stripe_customer_id   TEXT,
            stripe_subscription_id TEXT
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id    INTEGER PRIMARY KEY REFERENCES users(id),
            plan       TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id         SERIAL PRIMARY KEY,
            token_hash TEXT NOT NULL UNIQUE,
            user_id    INTEGER NOT NULL REFERENCES users(id),
            expires_at TEXT NOT NULL,
            used_at    TEXT,
            created_at TEXT NOT NULL
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS user_favorites (
            id         SERIAL PRIMARY KEY,
            user_id    INTEGER NOT NULL REFERENCES users(id),
            pick_id    TEXT NOT NULL,
            created_at TEXT NOT NULL,
            market     TEXT,
            aftr_score REAL,
            tier       TEXT,
            edge       REAL,
            home_team  TEXT,
            away_team  TEXT,
            UNIQUE(user_id, pick_id)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS user_picks (
            id         SERIAL PRIMARY KEY,
            user_id    INTEGER NOT NULL REFERENCES users(id),
            pick_id    TEXT NOT NULL,
            action     TEXT NOT NULL,
            result     TEXT,
            created_at TEXT NOT NULL,
            market     TEXT,
            aftr_score REAL,
            tier       TEXT,
            edge       REAL,
            home_team  TEXT,
            away_team  TEXT,
            settled_at TEXT,
            score_home INTEGER,
            score_away INTEGER,
            status     TEXT,
            UNIQUE(user_id, pick_id)
        )
        """)

        # Favorite team columns (added after initial schema — safe to re-run)
        for col, coltype in [
            ("favorite_team_id",    "TEXT"),
            ("favorite_team_name",  "TEXT"),
            ("favorite_team_crest", "TEXT"),
        ]:
            try:
                cur.execute(f"ALTER TABLE users ADD COLUMN {col} {coltype}")
                conn.commit()
            except Exception:
                conn.rollback()  # column already exists — ignore

        cur.execute("""
        CREATE TABLE IF NOT EXISTS bankroll_settings (
            user_id        INTEGER PRIMARY KEY REFERENCES users(id),
            initial_amount REAL    NOT NULL DEFAULT 10000,
            stake_per_unit REAL    NOT NULL DEFAULT 1000,
            currency       TEXT    NOT NULL DEFAULT 'ARS',
            created_at     TEXT    NOT NULL,
            updated_at     TEXT
        )
        """)
        # current_bankroll: snapshot persistente, actualizado en cada liquidación
        try:
            cur.execute("ALTER TABLE bankroll_settings ADD COLUMN current_bankroll REAL")
            conn.commit()
        except Exception:
            conn.rollback()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS bankroll_movements (
            id            SERIAL PRIMARY KEY,
            user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            bet_id        INTEGER REFERENCES user_bets(id) ON DELETE SET NULL,
            delta         NUMERIC(12,2) NOT NULL,
            balance_after NUMERIC(12,2) NOT NULL,
            movement_type TEXT NOT NULL,
            note          TEXT,
            created_at    TIMESTAMPTZ DEFAULT NOW()
        )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_bk_mvmt_user "
            "ON bankroll_movements(user_id, created_at DESC)"
        )

        # Indexes (IF NOT EXISTS is safe to re-run)
        # Tracker tables
        cur.execute("""
        CREATE TABLE IF NOT EXISTS user_bets (
            id               SERIAL PRIMARY KEY,
            user_id          INTEGER NOT NULL REFERENCES users(id),
            bet_type         TEXT NOT NULL DEFAULT 'simple',
            stake            NUMERIC(12,2) NOT NULL,
            total_odds       NUMERIC(8,3),
            potential_payout NUMERIC(12,2),
            status           TEXT NOT NULL DEFAULT 'PENDING',
            note             TEXT,
            created_at       TIMESTAMPTZ DEFAULT NOW(),
            settled_at       TIMESTAMPTZ
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS bet_legs (
            id           SERIAL PRIMARY KEY,
            bet_id       INTEGER NOT NULL REFERENCES user_bets(id) ON DELETE CASCADE,
            home_team    TEXT NOT NULL,
            away_team    TEXT NOT NULL,
            market       TEXT NOT NULL,
            odds         NUMERIC(8,3) NOT NULL,
            status       TEXT NOT NULL DEFAULT 'PENDING',
            sort_order   INTEGER DEFAULT 0,
            resolved_at  TIMESTAMPTZ,
            kickoff_time TIMESTAMPTZ
        )
        """)
        # Add kickoff_time to existing tables (safe to re-run)
        try:
            cur.execute("ALTER TABLE bet_legs ADD COLUMN kickoff_time TIMESTAMPTZ")
            conn.commit()
        except Exception:
            conn.rollback()

        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_bets_user_id ON user_bets(user_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bet_legs_bet_id ON bet_legs(bet_id)")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id         SERIAL PRIMARY KEY,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            endpoint   TEXT NOT NULL UNIQUE,
            p256dh     TEXT NOT NULL,
            auth       TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_push_subs_user_id ON push_subscriptions(user_id)")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS settled_picks_history (
            id           SERIAL PRIMARY KEY,
            match_id     BIGINT NOT NULL UNIQUE,
            league_code  TEXT,
            market       TEXT,
            decimal_odds NUMERIC(8,3),
            is_win       BOOLEAN NOT NULL,
            settled_at   TIMESTAMPTZ DEFAULT NOW()
        )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_sph_settled_at ON settled_picks_history(settled_at DESC)"
        )

        # match_id en bet_legs para sync preciso de combinadas
        try:
            cur.execute("ALTER TABLE bet_legs ADD COLUMN match_id BIGINT")
            conn.commit()
        except Exception:
            conn.rollback()

        # Picks publicados — fuente de verdad para persistencia más allá del JSON
        cur.execute("""
        CREATE TABLE IF NOT EXISTS published_picks (
            match_id     BIGINT PRIMARY KEY,
            league_code  TEXT NOT NULL,
            best_market  TEXT NOT NULL,
            best_prob    NUMERIC(6,4),
            best_fair    NUMERIC(6,3),
            utc_date     TEXT,
            pick_json    TEXT,
            published_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at   TIMESTAMPTZ DEFAULT NOW()
        )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_pp_league ON published_picks(league_code)"
        )

        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_favorites_user_id ON user_favorites(user_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_picks_user_id ON user_picks(user_id)")
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username) "
            "WHERE username IS NOT NULL"
        )

        conn.commit()
        logger.info("init_db: schema ready")
    except Exception:
        conn.rollback()
        logger.exception("init_db: error initializing schema")
        raise
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# Published picks — persistencia de picks que pasaron el filtro 1.60
# ---------------------------------------------------------------------------

import json as _json_mod


def upsert_published_pick(pick: dict, league_code: str) -> None:
    """Guarda o actualiza un pick publicado en Postgres. Best-effort: no lanza excepción."""
    try:
        mid = int(pick.get("match_id") or pick.get("id") or 0)
        if not mid:
            return
        market = pick.get("best_market") or ""
        if not market:
            return
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO published_picks
                    (match_id, league_code, best_market, best_prob, best_fair, utc_date, pick_json, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (match_id) DO UPDATE SET
                    best_market = EXCLUDED.best_market,
                    best_prob   = EXCLUDED.best_prob,
                    best_fair   = EXCLUDED.best_fair,
                    pick_json   = EXCLUDED.pick_json,
                    updated_at  = NOW()
                """,
                (
                    mid,
                    league_code,
                    market,
                    pick.get("best_prob"),
                    pick.get("best_fair"),
                    pick.get("utcDate"),
                    _json_mod.dumps(pick, default=str),
                ),
            )
            conn.commit()
        finally:
            put_conn(conn)
    except Exception as e:
        logger.debug("upsert_published_pick match_id=%s: %s", pick.get("match_id"), e)


def get_published_pick(match_id: int) -> dict | None:
    """Recupera un pick publicado por match_id. Devuelve el pick_json deserializado o None."""
    try:
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT pick_json FROM published_picks WHERE match_id = %s",
                (int(match_id),),
            )
            row = cur.fetchone()
            if row and row["pick_json"]:
                return _json_mod.loads(row["pick_json"])
        finally:
            put_conn(conn)
    except Exception as e:
        logger.debug("get_published_pick match_id=%s: %s", match_id, e)
    return None


def get_all_published_picks() -> list[dict]:
    """Recupera TODOS los picks publicados de todas las ligas. Para la home page."""
    try:
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT league_code, pick_json FROM published_picks ORDER BY updated_at DESC")
            rows = cur.fetchall()
            out = []
            for r in rows:
                try:
                    if r["pick_json"]:
                        p = _json_mod.loads(r["pick_json"])
                        if isinstance(p, dict):
                            p.setdefault("_league", r["league_code"])
                            out.append(p)
                except Exception:
                    pass
            return out
        finally:
            put_conn(conn)
    except Exception as e:
        logger.debug("get_all_published_picks: %s", e)
    return []


def maintenance_reset(*, clear_picks: bool = True) -> dict:
    """
    Mantenimiento: limpia published_picks y resetea flags de lock en cache_meta.
    Devuelve un dict con las acciones realizadas.
    """
    result: dict = {}
    if clear_picks:
        try:
            conn = get_conn()
            try:
                cur = conn.cursor()
                cur.execute("DELETE FROM published_picks")
                result["picks_deleted"] = cur.rowcount
                conn.commit()
                logger.info("maintenance_reset: deleted %d published_picks", cur.rowcount)
            finally:
                put_conn(conn)
        except Exception as e:
            result["picks_error"] = str(e)
            logger.warning("maintenance_reset picks error: %s", e)

    # Reset cache_meta refresh_running
    try:
        from data.cache import release_refresh_running_meta
        release_refresh_running_meta()
        result["refresh_running_reset"] = True
    except Exception as e:
        result["refresh_running_error"] = str(e)

    return result


def get_published_picks_for_league(league_code: str) -> list[dict]:
    """Recupera todos los picks publicados de una liga. Devuelve lista de dicts."""
    try:
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT pick_json FROM published_picks WHERE league_code = %s",
                (league_code,),
            )
            rows = cur.fetchall()
            out = []
            for r in rows:
                try:
                    if r["pick_json"]:
                        out.append(_json_mod.loads(r["pick_json"]))
                except Exception:
                    pass
            return out
        finally:
            put_conn(conn)
    except Exception as e:
        logger.debug("get_published_picks_for_league %s: %s", league_code, e)
    return []
