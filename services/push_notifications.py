"""
AFTR Push Notifications — envía notificaciones web push a usuarios suscritos.
Se llama desde el refresh worker cuando una pick empieza en <= NOTIFY_BEFORE_MIN minutos.
"""
from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("aftr.push")


def _vapid_private_key_pem() -> str:
    """
    Convierte VAPID_PRIVATE_KEY a PEM string que pywebpush puede parsear.
    Soporta: PEM directo, DER en base64/base64url, escalar raw de 32 bytes.
    """
    from config.settings import VAPID_PRIVATE_KEY
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PrivateFormat, NoEncryption, load_der_private_key,
    )

    key = (VAPID_PRIVATE_KEY or "").strip()
    if not key:
        raise ValueError("VAPID_PRIVATE_KEY no configurada")
    if "-----BEGIN" in key:
        return key  # ya es PEM

    # Normalizar a base64 estándar y decodificar
    key_std = key.replace("-", "+").replace("_", "/")
    pad = (4 - len(key_std) % 4) % 4
    raw = base64.b64decode(key_std + "=" * pad)

    # Intento 1: los bytes decodificados son en realidad un PEM (base64 del PEM)
    try:
        decoded_str = raw.decode("utf-8").strip()
        if "-----BEGIN" in decoded_str:
            return decoded_str
    except Exception:
        pass

    # Intento 2: DER directo (~121 bytes, formato pywebpush --gen)
    try:
        private_key = load_der_private_key(raw, password=None, backend=default_backend())
        pem = private_key.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption())
        return pem.decode("utf-8")
    except Exception:
        pass

    # Intento 3: escalar raw de 32 bytes (formato web-push Node.js)
    try:
        from cryptography.hazmat.primitives.asymmetric.ec import derive_private_key, SECP256R1
        if len(raw) == 32:
            d = int.from_bytes(raw, "big")
            private_key = derive_private_key(d, SECP256R1(), default_backend())
            pem = private_key.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption())
            return pem.decode("utf-8")
    except Exception:
        pass

    raise ValueError(f"VAPID_PRIVATE_KEY no reconocida ({len(raw)} bytes). "
                     "Debe ser PEM, DER en base64 o escalar EC de 32 bytes.")

NOTIFY_BEFORE_MIN = 60  # notificar 60 min antes del kick-off
_notified_cache: set[str] = set()  # evitar duplicados en memoria (pick_id+uid)
_NOTIFIED_CACHE_MAX = 5000  # cap para evitar memory leak en workers de larga duración


def _cache_add(key: str) -> None:
    if len(_notified_cache) >= _NOTIFIED_CACHE_MAX:
        # Eliminar la mitad más vieja (set no tiene orden, así que simplemente vaciamos la mitad)
        to_remove = list(_notified_cache)[:_NOTIFIED_CACHE_MAX // 2]
        for k in to_remove:
            _notified_cache.discard(k)
    _notified_cache.add(key)


def _get_vapid_claims() -> dict:
    from config.settings import VAPID_EMAIL
    return {"sub": VAPID_EMAIL}


def _send_one(endpoint: str, p256dh: str, auth: str, payload: dict) -> bool:
    """Envía una notificación a una suscripción. Retorna True si fue exitoso."""
    try:
        from pywebpush import webpush, WebPushException
        from config.settings import VAPID_PRIVATE_KEY
        webpush(
            subscription_info={"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}},
            data=json.dumps(payload),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims=_get_vapid_claims(),
        )
        return True
    except Exception as e:
        logger.warning("push_send error: %s", e)
        return False


def send_to_user(user_id: int, payload: dict) -> int:
    """Envía a todas las suscripciones activas de un usuario. Retorna cantidad enviadas."""
    from app.db import get_conn, put_conn
    conn = get_conn()
    subs = []
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = %s",
            (user_id,),
        )
        subs = list(cur.fetchall())
    finally:
        put_conn(conn)

    sent = 0
    dead_endpoints = []
    for row in subs:
        ok = _send_one(row["endpoint"], row["p256dh"], row["auth"], payload)
        if ok:
            sent += 1
        else:
            dead_endpoints.append(row["endpoint"])

    # Limpiar suscripciones muertas (endpoint ya no válido)
    if dead_endpoints:
        conn = get_conn()
        try:
            cur = conn.cursor()
            for ep in dead_endpoints:
                cur.execute("DELETE FROM push_subscriptions WHERE endpoint = %s", (ep,))
            conn.commit()
        except Exception:
            conn.rollback()
        finally:
            put_conn(conn)

    return sent


def notify_upcoming_picks(picks: list[dict], user_follows: dict[str, list[int]]) -> None:
    """
    Revisa picks próximas y notifica a los usuarios que las siguen.
    picks: lista de dicts con utcDate, home_team/home, away_team/away, best_market, pick_id/id
    user_follows: {pick_id: [user_id, ...]} — quién sigue cada pick
    """
    if not picks:
        return

    now = datetime.now(timezone.utc)
    notify_window_start = now
    notify_window_end   = now + timedelta(minutes=NOTIFY_BEFORE_MIN)

    for pick in picks:
        if not isinstance(pick, dict):
            continue

        utc_raw = pick.get("utcDate") or pick.get("utc_date") or ""
        if not utc_raw:
            continue

        try:
            # Parse ISO datetime
            dt_str = str(utc_raw).replace("Z", "+00:00")
            kickoff = datetime.fromisoformat(dt_str)
            if kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        if not (notify_window_start <= kickoff <= notify_window_end):
            continue

        pick_id = str(pick.get("pick_id") or pick.get("id") or pick.get("match_id") or "")
        if not pick_id:
            continue

        followers = user_follows.get(pick_id, [])
        if not followers:
            continue

        home = pick.get("home_team") or pick.get("home") or "Local"
        away = pick.get("away_team") or pick.get("away") or "Visitante"
        market = pick.get("best_market") or ""
        mins_left = int((kickoff - now).total_seconds() / 60)

        payload = {
            "title": f"{home} vs {away}",
            "body": f"Tu pick empieza en {mins_left} min" + (f" — {market}" if market else ""),
            "tag": f"pick-{pick_id}",
            "url": "/",
        }

        for uid in followers:
            cache_key = f"{pick_id}:{uid}"
            if cache_key in _notified_cache:
                continue
            sent = send_to_user(uid, payload)
            if sent > 0:
                _cache_add(cache_key)
                logger.info("push sent pick=%s user=%s", pick_id, uid)


def notify_tracker_bets() -> None:
    """
    Revisa bet_legs con kickoff_time en los próximos NOTIFY_BEFORE_MIN minutos
    y notifica al dueño de la apuesta.
    """
    from app.db import get_conn, put_conn
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(minutes=NOTIFY_BEFORE_MIN)

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT bl.id, bl.home_team, bl.away_team, bl.market,
                      bl.kickoff_time, ub.user_id, ub.id AS bet_id, ub.bet_type
               FROM bet_legs bl
               JOIN user_bets ub ON bl.bet_id = ub.id
               WHERE bl.status = 'PENDING'
                 AND bl.kickoff_time IS NOT NULL
                 AND bl.kickoff_time BETWEEN %s AND %s""",
            (now, window_end),
        )
        rows = list(cur.fetchall())
    finally:
        put_conn(conn)

    for row in rows:
        uid = row["user_id"]
        leg_id = row["id"]
        kickoff = row["kickoff_time"]
        if kickoff.tzinfo is None:
            kickoff = kickoff.replace(tzinfo=timezone.utc)
        mins_left = max(1, int((kickoff - now).total_seconds() / 60))

        cache_key = f"tracker:{leg_id}:{uid}"
        if cache_key in _notified_cache:
            continue

        home = row["home_team"]
        away = row["away_team"]
        market = row["market"] or ""
        bet_type = row["bet_type"]

        payload = {
            "title": f"{home} vs {away}",
            "body": f"Tu {'combinada' if bet_type == 'combinada' else 'apuesta'} empieza en {mins_left} min"
                    + (f" — {market}" if market else ""),
            "tag": f"tracker-leg-{leg_id}",
            "url": "/tracker",
        }

        sent = send_to_user(uid, payload)
        if sent > 0:
            _cache_add(cache_key)
            logger.info("push tracker leg=%s user=%s", leg_id, uid)


def notify_trial_expiring() -> None:
    """
    Envía push a usuarios cuyo trial expira en las próximas 48 horas.
    Evita duplicados: solo notifica una vez por usuario por ejecución del proceso.
    """
    from app.db import get_conn, put_conn
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(hours=48)

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, username, email FROM users
               WHERE subscription_status = 'trial'
                 AND subscription_end IS NOT NULL
                 AND subscription_end::timestamptz > %s
                 AND subscription_end::timestamptz <= %s""",
            (now, window_end),
        )
        users = list(cur.fetchall())
    finally:
        put_conn(conn)

    for row in users:
        uid = row["id"]
        cache_key = f"trial_expiring:{uid}"
        if cache_key in _notified_cache:
            continue

        sub_end_raw = None
        conn2 = get_conn()
        try:
            cur2 = conn2.cursor()
            cur2.execute("SELECT subscription_end FROM users WHERE id = %s", (uid,))
            r = cur2.fetchone()
            sub_end_raw = r["subscription_end"] if r else None
        finally:
            put_conn(conn2)

        if not sub_end_raw:
            continue
        try:
            if hasattr(sub_end_raw, "year"):
                end_dt = sub_end_raw
            else:
                end_dt = datetime.fromisoformat(str(sub_end_raw).replace("Z", "+00:00"))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            hours_left = int((end_dt - now).total_seconds() / 3600)
        except Exception:
            continue

        if hours_left <= 24:
            body = "Tu prueba Premium vence hoy. Activá tu plan para no perder los picks."
        else:
            body = f"Tu prueba Premium vence en {hours_left // 24} día{'s' if hours_left // 24 != 1 else ''}. Activá tu plan."

        payload = {
            "title": "⭐ Tu prueba AFTR Premium está por vencer",
            "body": body,
            "tag": "trial-expiring",
            "url": "/?auth=premium",
        }

        sent = send_to_user(uid, payload)
        if sent > 0:
            _cache_add(cache_key)
            logger.info("push trial_expiring sent uid=%s hours_left=%s", uid, hours_left)

        # Email de expiración (independiente del push, cache key propia)
        email_addr = (row.get("email") or "").strip()
        uname = (row.get("username") or email_addr.split("@")[0] or "").strip()
        email_cache_key = f"trial_expiring_email:{uid}"
        if email_addr and email_cache_key not in _notified_cache:
            from app.email_utils import send_trial_expiring_email
            days_left = max(1, hours_left // 24) if hours_left > 0 else 0
            ok = send_trial_expiring_email(email_addr, uname, days_left)
            if ok:
                _cache_add(email_cache_key)
                logger.info("email trial_expiring sent uid=%s days_left=%s", uid, days_left)


def load_user_follows_index() -> dict[str, list[int]]:
    """
    Construye {pick_id: [user_id, ...]} desde la tabla user_picks (seguidas).
    Solo picks con status!=settled para no notificar lo ya resuelto.
    """
    from app.db import get_conn, put_conn
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT user_id, pick_id FROM user_picks
               WHERE action = 'follow' AND (result IS NULL OR result = 'PENDING')"""
        )
        index: dict[str, list[int]] = {}
        for row in cur.fetchall():
            index.setdefault(str(row["pick_id"]), []).append(row["user_id"])
        return index
    finally:
        put_conn(conn)
