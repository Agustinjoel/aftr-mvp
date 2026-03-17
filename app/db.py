"""
AFTR user storage (no password hashes exposed outside this module/auth).

- Database file: path from env AFTR_DB_PATH (fallback: DB_PATH, then base_dir/aftr.db) via config.settings.
- Table: users
- Columns: id, email, username, password_hash, role, subscription_status,
  subscription_start, subscription_end, created_at, updated_at,
  stripe_customer_id, stripe_subscription_id
"""
from __future__ import annotations
import sqlite3

from config.settings import DB_PATH

USERS_TABLE = "users"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL UNIQUE,
        username TEXT UNIQUE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'free_user',
        subscription_status TEXT NOT NULL DEFAULT 'inactive',
        subscription_start TEXT,
        subscription_end TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT,
        stripe_customer_id TEXT,
        stripe_subscription_id TEXT
    )
    """)

    # Migration: ensure password_hash exists (old DBs may have been created without it)
    table_info = cur.execute("PRAGMA table_info(users)").fetchall()
    column_names = [row[1] for row in table_info]
    if "password_hash" not in column_names:
        cur.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")

    for col_def in [
        ("username", "TEXT"),
        ("role", "TEXT NOT NULL DEFAULT 'free_user'"),
        ("subscription_status", "TEXT NOT NULL DEFAULT 'inactive'"),
        ("subscription_start", "TEXT"),
        ("subscription_end", "TEXT"),
        ("updated_at", "TEXT"),
        ("stripe_customer_id", "TEXT"),
        ("stripe_subscription_id", "TEXT"),
    ]:
        try:
            cur.execute(
                "ALTER TABLE users ADD COLUMN " + col_def[0] + " " + col_def[1]
            )
        except sqlite3.OperationalError:
            pass

    try:
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username) WHERE username IS NOT NULL"
        )
    except sqlite3.OperationalError:
        pass

    cur.execute("""
    CREATE TABLE IF NOT EXISTS subscriptions (
        user_id INTEGER PRIMARY KEY,
        plan TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS password_reset_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token_hash TEXT NOT NULL UNIQUE,
        user_id INTEGER NOT NULL,
        expires_at TEXT NOT NULL,
        used_at TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )
    """)

    # Phase 1 user system: favorites and followed picks
    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_favorites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        pick_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id),
        UNIQUE(user_id, pick_id)
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_picks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        pick_id TEXT NOT NULL,
        action TEXT NOT NULL,
        result TEXT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )
    """)
    try:
        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_favorites_user_id ON user_favorites(user_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_picks_user_id ON user_picks(user_id)")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()