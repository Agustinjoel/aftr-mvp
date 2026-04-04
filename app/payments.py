from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone, timedelta

import requests as http_requests

from fastapi import APIRouter, Request, Query
from fastapi.responses import RedirectResponse, JSONResponse

from app.auth import get_user_id, get_user_by_id
from app.db import get_conn, put_conn
from app.email_utils import send_premium_welcome_email
from config.settings import settings

router = APIRouter()
logger = logging.getLogger("aftr.payments")

_LS_API_BASE = "https://api.lemonsqueezy.com/v1"


def _ls_headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.lemonsqueezy_api_key}",
        "Accept": "application/vnd.api+json",
        "Content-Type": "application/vnd.api+json",
    }


# ─────────────────────────────────────────────
# Manual activation (testing / admin)
# ─────────────────────────────────────────────

@router.get("/billing/ls-debug", include_in_schema=False)
def ls_debug(request: Request):
    """Endpoint temporal — muestra store_id configurado y stores reales de la cuenta LS."""
    if not settings.lemonsqueezy_api_key:
        return JSONResponse({"error": "no api key"})
    try:
        r = http_requests.get(f"{_LS_API_BASE}/stores", headers=_ls_headers(), timeout=10)
        stores = r.json()
    except Exception as e:
        stores = {"error": str(e)}
    return JSONResponse({
        "configured_store_id": settings.lemonsqueezy_store_id,
        "configured_variant_id": settings.lemonsqueezy_variant_id,
        "api_key_set": bool(settings.lemonsqueezy_api_key),
        "ls_stores_response": stores,
    })


@router.get("/premium/manual-activate", include_in_schema=False)
def manual_activate(request: Request, plan: str = Query("PREMIUM")):
    uid = get_user_id(request)
    if not uid:
        return RedirectResponse(url="/?msg=need_login", status_code=302)

    plan = (plan or "PREMIUM").upper()
    if plan not in ("PREMIUM", "PRO"):
        plan = "PREMIUM"

    now = datetime.now(timezone.utc)
    exp = now + timedelta(days=30)
    now_iso = now.isoformat()
    exp_iso = exp.isoformat()

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO subscriptions(user_id, plan, expires_at, created_at)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT(user_id) DO UPDATE SET
              plan=EXCLUDED.plan,
              expires_at=EXCLUDED.expires_at
            """,
            (uid, plan, exp_iso, now_iso),
        )
        try:
            cur.execute(
                """
                UPDATE users SET
                  role = 'premium_user',
                  subscription_status = 'active',
                  subscription_start = %s,
                  subscription_end = %s,
                  updated_at = %s
                WHERE id = %s
                """,
                (now_iso, exp_iso, now_iso, uid),
            )
        except Exception:
            logger.exception("manual_activate: error updating user flags uid=%s", uid)
        conn.commit()
    finally:
        put_conn(conn)

    return RedirectResponse(url="/?msg=premium_on", status_code=302)


# ─────────────────────────────────────────────
# Checkout — crea sesión en Lemon Squeezy
# ─────────────────────────────────────────────

@router.post("/billing/create-checkout-session")
def create_checkout_session(request: Request):
    uid = get_user_id(request)
    if not uid:
        return JSONResponse({"ok": False, "error": "need_login"}, status_code=401)

    if not settings.lemonsqueezy_api_key:
        logger.error("Lemon Squeezy API key not configured")
        return JSONResponse({"ok": False, "error": "payments_not_configured"}, status_code=500)

    if not settings.lemonsqueezy_store_id or not settings.lemonsqueezy_variant_id:
        logger.error("Lemon Squeezy store_id or variant_id missing")
        return JSONResponse({"ok": False, "error": "payments_not_configured"}, status_code=500)

    base_url = (
        (getattr(settings, "app_base_url", None) or "").strip().rstrip("/")
        or str(request.base_url).rstrip("/")
    )

    user = get_user_by_id(uid) or {}
    email = user.get("email") or ""

    payload = {
        "data": {
            "type": "checkouts",
            "attributes": {
                "checkout_options": {
                    "dark": True,
                },
                "checkout_data": {
                    "email": email,
                    "custom": {"user_id": str(uid)},
                },
                "product_options": {
                    "redirect_url": f"{base_url}/?msg=premium_activated",
                },
            },
            "relationships": {
                "store": {
                    "data": {"type": "stores", "id": str(settings.lemonsqueezy_store_id)}
                },
                "variant": {
                    "data": {"type": "variants", "id": str(settings.lemonsqueezy_variant_id)}
                },
            },
        }
    }

    logger.info("LS checkout: store_id=%s variant_id=%s uid=%s", settings.lemonsqueezy_store_id, settings.lemonsqueezy_variant_id, uid)
    try:
        r = http_requests.post(
            f"{_LS_API_BASE}/checkouts",
            headers=_ls_headers(),
            json=payload,
            timeout=10,
        )
        if r.status_code not in (200, 201):
            logger.error("LS checkout create failed: %s — %s", r.status_code, r.text[:300])
            return JSONResponse({"ok": False, "error": "checkout_failed"}, status_code=500)
        data = r.json()
        url = data["data"]["attributes"]["url"]
        logger.info("LS checkout created for uid=%s", uid)
        return JSONResponse({"ok": True, "url": url})
    except Exception as e:
        logger.exception("create-checkout-session failed: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ─────────────────────────────────────────────
# Webhook — Lemon Squeezy
# ─────────────────────────────────────────────

@router.post("/webhooks/lemonsqueezy", include_in_schema=False)
async def lemonsqueezy_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("X-Signature", "")
    wh_secret = settings.lemonsqueezy_webhook_secret

    if wh_secret:
        expected = hmac.new(wh_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig_header):
            logger.warning("LS webhook: invalid signature")
            return JSONResponse({"ok": False}, status_code=400)

    try:
        data = json.loads(payload.decode("utf-8"))
    except Exception:
        logger.warning("LS webhook: invalid JSON")
        return JSONResponse({"ok": False}, status_code=400)

    ev_type: str = data.get("meta", {}).get("event_name", "")
    custom_data: dict = data.get("meta", {}).get("custom_data") or {}
    attrs: dict = data.get("data", {}).get("attributes", {})

    logger.info("LS webhook received: %s", ev_type)

    if ev_type in ("subscription_created", "subscription_updated"):
        status = (attrs.get("status") or "").strip().lower()
        ls_subscription_id = str(data.get("data", {}).get("id") or "")
        ls_customer_id = str(attrs.get("customer_id") or "")
        renews_at = attrs.get("renews_at") or attrs.get("trial_ends_at")

        # Resolve user: prefer custom_data.user_id, fallback to DB lookup by subscription id
        uid_int = _uid_from_custom_data(custom_data)
        if uid_int is None and ls_subscription_id:
            uid_int = _uid_from_ls_subscription_id(ls_subscription_id)

        if uid_int:
            if status in ("active", "on_trial"):
                exp_iso = _parse_ls_date(renews_at) or (
                    datetime.now(timezone.utc) + timedelta(days=30)
                ).isoformat()
                _apply_premium_to_user(
                    uid_int,
                    exp_iso,
                    customer_id=ls_customer_id or None,
                    subscription_id=ls_subscription_id or None,
                )
                logger.info("LS %s: premium applied uid=%s exp=%s", ev_type, uid_int, exp_iso)
                # Bienvenida premium solo en subscription_created (no en renewals)
                if ev_type == "subscription_created":
                    try:
                        user = get_user_by_id(uid_int) or {}
                        email = user.get("email") or ""
                        username = user.get("username") or user.get("name") or email.split("@")[0]
                        if email:
                            send_premium_welcome_email(email, username)
                    except Exception:
                        logger.warning("LS: no se pudo enviar email premium uid=%s", uid_int, exc_info=True)
            else:
                _revoke_premium_for_user(uid_int)
                logger.info("LS %s: premium revoked uid=%s status=%s", ev_type, uid_int, status)
        else:
            logger.warning("LS %s: could not resolve user — custom_data=%s sub_id=%s", ev_type, custom_data, ls_subscription_id)

    elif ev_type == "subscription_cancelled":
        ls_subscription_id = str(data.get("data", {}).get("id") or "")
        uid_int = _uid_from_custom_data(custom_data)
        if uid_int is None and ls_subscription_id:
            uid_int = _uid_from_ls_subscription_id(ls_subscription_id)
        if uid_int:
            _revoke_premium_for_user(uid_int)
            logger.info("LS subscription_cancelled: premium revoked uid=%s", uid_int)

    elif ev_type == "order_created":
        # One-time purchase fallback (in case variant is one-time, not subscription)
        status = (attrs.get("status") or "").strip().lower()
        if status == "paid":
            uid_int = _uid_from_custom_data(custom_data)
            if uid_int:
                exp_iso = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
                ls_customer_id = str(attrs.get("customer_id") or "")
                _apply_premium_to_user(uid_int, exp_iso, customer_id=ls_customer_id or None)
                logger.info("LS order_created: premium applied uid=%s", uid_int)
                try:
                    user = get_user_by_id(uid_int) or {}
                    email = user.get("email") or ""
                    username = user.get("username") or user.get("name") or email.split("@")[0]
                    if email:
                        send_premium_welcome_email(email, username)
                except Exception:
                    logger.warning("LS: no se pudo enviar email premium (order) uid=%s", uid_int, exc_info=True)

    return JSONResponse({"ok": True})


# ─────────────────────────────────────────────
# Helpers internos
# ─────────────────────────────────────────────

def _parse_ls_date(date_str: str | None) -> str | None:
    """Convierte fecha ISO de LS (con Z o offset) a ISO string en UTC."""
    if not date_str:
        return None
    try:
        # LS envía "2026-05-03T00:00:00.000000Z"
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def _uid_from_custom_data(custom_data: dict) -> int | None:
    """Extrae user_id del campo custom_data del webhook."""
    raw = custom_data.get("user_id")
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _uid_from_ls_subscription_id(ls_subscription_id: str) -> int | None:
    """Busca user_id por LS subscription ID guardado en stripe_subscription_id."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE stripe_subscription_id = %s", (ls_subscription_id,))
        row = cur.fetchone()
    finally:
        put_conn(conn)
    return int(row["id"]) if row else None


def _apply_premium_to_user(
    uid_int: int,
    expires_at_iso: str,
    customer_id: str | None = None,
    subscription_id: str | None = None,
) -> None:
    """Activa PREMIUM en subscriptions y users para el user_id dado."""
    now_iso = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO subscriptions(user_id, plan, expires_at, created_at)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT(user_id) DO UPDATE SET plan=EXCLUDED.plan, expires_at=EXCLUDED.expires_at
            """,
            (uid_int, "PREMIUM", expires_at_iso, now_iso),
        )
        updates = [
            "role = 'premium_user'",
            "subscription_status = 'active'",
            "subscription_start = COALESCE(subscription_start, %s)",
            "subscription_end = %s",
            "updated_at = %s",
        ]
        args: list = [now_iso, expires_at_iso, now_iso]
        if customer_id is not None:
            updates.append("stripe_customer_id = COALESCE(stripe_customer_id, %s)")
            args.append(customer_id)
        if subscription_id is not None:
            updates.append("stripe_subscription_id = COALESCE(stripe_subscription_id, %s)")
            args.append(subscription_id)
        args.append(uid_int)
        cur.execute(
            "UPDATE users SET " + ", ".join(updates) + " WHERE id = %s",
            tuple(args),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("_apply_premium_to_user: rolled back uid=%s", uid_int)
        raise
    finally:
        put_conn(conn)


def _revoke_premium_for_user(uid_int: int) -> None:
    """Revoca el plan premium del usuario."""
    now_iso = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE subscriptions SET plan = 'FREE', expires_at = %s WHERE user_id = %s",
            (now_iso, uid_int),
        )
        cur.execute(
            """
            UPDATE users SET role = 'free_user', subscription_status = 'inactive', updated_at = %s
            WHERE id = %s
            """,
            (now_iso, uid_int),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("_revoke_premium_for_user: rolled back uid=%s", uid_int)
        raise
    finally:
        put_conn(conn)
