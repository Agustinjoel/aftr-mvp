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


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            _pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=10,
                dsn=DATABASE_URL,
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

        # Indexes (IF NOT EXISTS is safe to re-run)
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
