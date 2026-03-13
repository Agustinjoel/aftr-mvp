from __future__ import annotations
from datetime import datetime, timezone

from app.db import get_conn

def get_active_plan(user_id: int) -> str:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT plan, expires_at FROM subscriptions WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()

    if not row:
        return "FREE"

    exp = datetime.fromisoformat(row["expires_at"])
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)

    return row["plan"] if exp > datetime.now(timezone.utc) else "FREE"