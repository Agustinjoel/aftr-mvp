#!/usr/bin/env python3
"""
Migración de datos: SQLite (aftr.db) → PostgreSQL.

Cómo usar:
    1. Asegurate de que PostgreSQL esté corriendo (docker compose up db -d)
    2. Desde la raíz del proyecto:
       python scripts/migrate_sqlite_to_pg.py

El script es idempotente: usa ON CONFLICT DO NOTHING / UPDATE para no duplicar datos.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

# ── Configuración ────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parents[1]

# Importar settings para obtener DATABASE_URL y DB_PATH
sys.path.insert(0, str(BASE_DIR))
from config.settings import DATABASE_URL, DB_PATH

SQLITE_PATH = DB_PATH
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("migrate")


# ── Helpers ──────────────────────────────────────────────────────────────────

def sqlite_rows(conn: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def pg_exec(cur, sql: str, params=()) -> None:
    cur.execute(sql, params)


# ── Tablas a migrar ──────────────────────────────────────────────────────────

def migrate_users(sl: sqlite3.Connection, pg_cur) -> int:
    rows = sqlite_rows(sl, "SELECT * FROM users")
    count = 0
    for r in rows:
        pg_exec(pg_cur,
            """
            INSERT INTO users
              (id, email, username, password_hash, role, subscription_status,
               subscription_start, subscription_end, created_at, updated_at,
               stripe_customer_id, stripe_subscription_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(email) DO UPDATE SET
              username             = EXCLUDED.username,
              password_hash        = EXCLUDED.password_hash,
              role                 = EXCLUDED.role,
              subscription_status  = EXCLUDED.subscription_status,
              subscription_start   = EXCLUDED.subscription_start,
              subscription_end     = EXCLUDED.subscription_end,
              updated_at           = EXCLUDED.updated_at,
              stripe_customer_id   = EXCLUDED.stripe_customer_id,
              stripe_subscription_id = EXCLUDED.stripe_subscription_id
            """,
            (
                r.get("id"), r.get("email"), r.get("username"),
                r.get("password_hash") or "",
                r.get("role") or "free_user",
                r.get("subscription_status") or "inactive",
                r.get("subscription_start"), r.get("subscription_end"),
                r.get("created_at") or "", r.get("updated_at"),
                r.get("stripe_customer_id"), r.get("stripe_subscription_id"),
            ),
        )
        count += 1
    return count


def migrate_subscriptions(sl: sqlite3.Connection, pg_cur) -> int:
    rows = sqlite_rows(sl, "SELECT * FROM subscriptions")
    count = 0
    for r in rows:
        pg_exec(pg_cur,
            """
            INSERT INTO subscriptions (user_id, plan, expires_at, created_at)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT(user_id) DO UPDATE SET
              plan       = EXCLUDED.plan,
              expires_at = EXCLUDED.expires_at
            """,
            (r["user_id"], r["plan"], r["expires_at"], r["created_at"]),
        )
        count += 1
    return count


def migrate_password_reset_tokens(sl: sqlite3.Connection, pg_cur) -> int:
    rows = sqlite_rows(sl, "SELECT * FROM password_reset_tokens")
    count = 0
    for r in rows:
        pg_exec(pg_cur,
            """
            INSERT INTO password_reset_tokens
              (id, token_hash, user_id, expires_at, used_at, created_at)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT(token_hash) DO NOTHING
            """,
            (r["id"], r["token_hash"], r["user_id"],
             r["expires_at"], r.get("used_at"), r["created_at"]),
        )
        count += 1
    return count


def migrate_user_favorites(sl: sqlite3.Connection, pg_cur) -> int:
    rows = sqlite_rows(sl, "SELECT * FROM user_favorites")
    count = 0
    for r in rows:
        pg_exec(pg_cur,
            """
            INSERT INTO user_favorites
              (user_id, pick_id, created_at, market, aftr_score, tier, edge, home_team, away_team)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(user_id, pick_id) DO NOTHING
            """,
            (
                r["user_id"], r["pick_id"], r["created_at"],
                r.get("market"), r.get("aftr_score"), r.get("tier"),
                r.get("edge"), r.get("home_team"), r.get("away_team"),
            ),
        )
        count += 1
    return count


def migrate_user_picks(sl: sqlite3.Connection, pg_cur) -> int:
    rows = sqlite_rows(sl, "SELECT * FROM user_picks")
    count = 0
    for r in rows:
        pg_exec(pg_cur,
            """
            INSERT INTO user_picks
              (user_id, pick_id, action, result, created_at, market, aftr_score,
               tier, edge, home_team, away_team, settled_at, score_home, score_away, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(user_id, pick_id) DO NOTHING
            """,
            (
                r["user_id"], r["pick_id"], r.get("action") or "follow",
                r.get("result"), r["created_at"],
                r.get("market"), r.get("aftr_score"), r.get("tier"), r.get("edge"),
                r.get("home_team"), r.get("away_team"),
                r.get("settled_at"), r.get("score_home"), r.get("score_away"),
                r.get("status"),
            ),
        )
        count += 1
    return count


def migrate_matches(sl: sqlite3.Connection, pg_cur) -> int:
    rows = sqlite_rows(sl, "SELECT * FROM matches")
    count = 0
    for r in rows:
        pg_exec(pg_cur,
            """
            INSERT INTO matches (league, match_id, "utcDate", status, home, away,
                                 home_goals, away_goals, last_updated)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(league, match_id) DO UPDATE SET
              "utcDate"    = EXCLUDED."utcDate",
              status       = EXCLUDED.status,
              home_goals   = EXCLUDED.home_goals,
              away_goals   = EXCLUDED.away_goals,
              last_updated = EXCLUDED.last_updated
            """,
            (
                r["league"], r["match_id"], r.get("utcDate"), r.get("status"),
                r.get("home"), r.get("away"),
                r.get("home_goals"), r.get("away_goals"), r.get("last_updated"),
            ),
        )
        count += 1
    return count


def migrate_picks(sl: sqlite3.Connection, pg_cur) -> int:
    rows = sqlite_rows(sl, "SELECT * FROM picks")
    count = 0
    for r in rows:
        pg_exec(pg_cur,
            """
            INSERT INTO picks (league, match_id, created_at, xg_home, xg_away, xg_total,
                               probs_json, candidates_json, best_market, best_prob, best_fair,
                               result, result_reason)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(league, match_id) DO UPDATE SET
              created_at      = EXCLUDED.created_at,
              xg_home         = EXCLUDED.xg_home,
              xg_away         = EXCLUDED.xg_away,
              xg_total        = EXCLUDED.xg_total,
              probs_json      = EXCLUDED.probs_json,
              candidates_json = EXCLUDED.candidates_json,
              best_market     = EXCLUDED.best_market,
              best_prob       = EXCLUDED.best_prob,
              best_fair       = EXCLUDED.best_fair,
              result          = EXCLUDED.result,
              result_reason   = EXCLUDED.result_reason
            """,
            (
                r["league"], r["match_id"], r.get("created_at") or "",
                r.get("xg_home"), r.get("xg_away"), r.get("xg_total"),
                r.get("probs_json"), r.get("candidates_json"),
                r.get("best_market"), r.get("best_prob"), r.get("best_fair"),
                r.get("result"), r.get("result_reason"),
            ),
        )
        count += 1
    return count


# ── Sincronizar secuencias SERIAL ────────────────────────────────────────────

def sync_sequences(pg_cur) -> None:
    """Resetea las secuencias SERIAL al máximo id actual para evitar conflictos."""
    for table, col in [
        ("users", "id"),
        ("password_reset_tokens", "id"),
        ("user_favorites", "id"),
        ("user_picks", "id"),
    ]:
        pg_cur.execute(
            f"SELECT setval(pg_get_serial_sequence('{table}', '{col}'), "
            f"COALESCE(MAX({col}), 1)) FROM {table}"
        )
    log.info("Secuencias SERIAL sincronizadas")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    sl_path = Path(SQLITE_PATH)
    if not sl_path.exists():
        log.error("SQLite DB no encontrada: %s", sl_path)
        sys.exit(1)

    log.info("SQLite source: %s", sl_path)
    log.info("PostgreSQL target: %s", DATABASE_URL.split("@")[-1])

    sl = sqlite3.connect(str(sl_path))
    pg = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    pg_cur = pg.cursor()

    # Ensure schema exists
    from app.db import init_db as init_user_db
    from db import init_db as init_picks_db
    log.info("Inicializando schema en PostgreSQL...")
    init_user_db()
    init_picks_db()

    log.info("Migrando datos...")

    tables = [
        ("users",                  migrate_users),
        ("subscriptions",          migrate_subscriptions),
        ("password_reset_tokens",  migrate_password_reset_tokens),
        ("user_favorites",         migrate_user_favorites),
        ("user_picks",             migrate_user_picks),
    ]

    # Matches/picks from SQLite (same file)
    matches_tables = [
        ("matches", migrate_matches),
        ("picks",   migrate_picks),
    ]

    for name, fn in tables:
        try:
            n = fn(sl, pg_cur)
            pg.commit()
            log.info("  %-30s → %d filas migradas", name, n)
        except Exception as e:
            pg.rollback()
            log.error("  ERROR en %s: %s", name, e)

    for name, fn in matches_tables:
        cur = sl.cursor()
        cur.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{name}'")
        if not cur.fetchone():
            log.info("  %-30s → tabla no encontrada en SQLite, saltando", name)
            continue
        try:
            n = fn(sl, pg_cur)
            pg.commit()
            log.info("  %-30s → %d filas migradas", name, n)
        except Exception as e:
            pg.rollback()
            log.error("  ERROR en %s: %s", name, e)

    sync_sequences(pg_cur)
    pg.commit()

    sl.close()
    pg_cur.close()
    pg.close()
    log.info("Migración completada.")


if __name__ == "__main__":
    main()
