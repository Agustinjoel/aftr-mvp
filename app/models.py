from __future__ import annotations
from datetime import datetime, timezone

from app.db import get_conn, put_conn


def get_active_plan(user_id: int) -> str:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT plan, expires_at FROM subscriptions WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
    finally:
        put_conn(conn)

    if not row:
        return "FREE"

    exp = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)

    return row["plan"] if exp > datetime.now(timezone.utc) else "FREE"
