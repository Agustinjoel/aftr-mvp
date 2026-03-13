from __future__ import annotations
from datetime import datetime, timezone, timedelta
import json
import logging

from fastapi import APIRouter, Request, Query
from fastapi.responses import RedirectResponse, JSONResponse

from app.auth import get_user_id, get_user_by_id
from app.db import get_conn
from config.settings import settings

router = APIRouter()
logger = logging.getLogger("aftr.payments")

try:
    import stripe  # type: ignore
    if settings.stripe_secret_key:
        stripe.api_key = settings.stripe_secret_key
except Exception:  # pragma: no cover - optional dependency
    stripe = None  # type: ignore
    logger.warning("Stripe SDK not available; billing endpoints will be disabled.")


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
    cur = conn.cursor()
    cur.execute(
        """
      INSERT INTO subscriptions(user_id, plan, expires_at, created_at)
      VALUES (?,?,?,?)
      ON CONFLICT(user_id) DO UPDATE SET
        plan=excluded.plan,
        expires_at=excluded.expires_at
    """,
        (uid, plan, exp_iso, now_iso),
    )
    try:
        cur.execute(
            """
          UPDATE users SET
            role = 'premium_user',
            subscription_status = 'active',
            subscription_start = ?,
            subscription_end = ?,
            updated_at = ?
          WHERE id = ?
        """,
            (now_iso, exp_iso, now_iso, uid),
        )
    except Exception:
        logger.exception("Error updating user premium flags in manual_activate")
    conn.commit()
    conn.close()

    return RedirectResponse(url="/?msg=premium_on", status_code=302)


@router.post("/billing/create-checkout-session")
def create_checkout_session(request: Request):
    uid = get_user_id(request)
    if not uid:
        return JSONResponse({"ok": False, "error": "need_login"}, status_code=401)

    if not stripe or not settings.stripe_secret_key or not settings.stripe_price_id:
        logger.error("Stripe not configured; cannot create checkout session")
        return JSONResponse({"ok": False, "error": "stripe_not_configured"}, status_code=500)

    user = get_user_by_id(uid) or {}
    email = user.get("email") or None

    base_url = (settings.app_base_url or str(request.base_url)).rstrip("/")
    success_url = f"{base_url}/billing/success"
    cancel_url = base_url

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": settings.stripe_price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            client_reference_id=str(uid),
            customer_email=email,
        )
        return JSONResponse({"ok": True, "url": session.url})
    except Exception as e:  # pragma: no cover - network / Stripe-side
        logger.error("Error creating Stripe checkout session", exc_info=True)
        return JSONResponse({"ok": False, "error": "stripe_error"}, status_code=500)


@router.get("/billing/success", include_in_schema=False)
def billing_success():
    """After Stripe Checkout success: redirect to dashboard with premium_activated so the success animation shows."""
    return RedirectResponse(url="/?msg=premium_activated", status_code=302)


@router.post("/webhooks/stripe", include_in_schema=False)
async def stripe_webhook(request: Request):
    if not stripe or not settings.stripe_secret_key:
        logger.error("Stripe not configured; ignoring webhook")
        return JSONResponse({"ok": False}, status_code=500)

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    wh_secret = settings.stripe_webhook_secret

    try:
        if wh_secret:
            event = stripe.Webhook.construct_event(payload, sig_header, wh_secret)
        else:
            data = json.loads(payload.decode("utf-8"))
            event = stripe.Event.construct_from(data, stripe.api_key)
    except Exception:  # pragma: no cover - signature / JSON issues
        logger.warning("Invalid Stripe webhook", exc_info=True)
        return JSONResponse({"ok": False}, status_code=400)

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        uid = session.get("client_reference_id")
        customer_id = session.get("customer")
        subscription_id = session.get("subscription")
        if uid:
            try:
                uid_int = int(uid)
            except (TypeError, ValueError):
                uid_int = None
            if uid_int:
                now = datetime.now(timezone.utc)
                exp = now + timedelta(days=30)
                now_iso = now.isoformat()
                exp_iso = exp.isoformat()
                conn = get_conn()
                cur = conn.cursor()
                # update subscriptions table
                cur.execute(
                    """
                  INSERT INTO subscriptions(user_id, plan, expires_at, created_at)
                  VALUES (?,?,?,?)
                  ON CONFLICT(user_id) DO UPDATE SET
                    plan=excluded.plan,
                    expires_at=excluded.expires_at
                """,
                    (uid_int, "PREMIUM", exp_iso, now_iso),
                )
                # update user flags + stripe ids
                try:
                    cur.execute(
                        """
                      UPDATE users SET
                        role = 'premium_user',
                        subscription_status = 'active',
                        subscription_start = COALESCE(subscription_start, ?),
                        subscription_end = ?,
                        stripe_customer_id = COALESCE(stripe_customer_id, ?),
                        stripe_subscription_id = COALESCE(stripe_subscription_id, ?),
                        updated_at = ?
                      WHERE id = ?
                    """,
                        (
                            now_iso,
                            exp_iso,
                            customer_id,
                            subscription_id,
                            now_iso,
                            uid_int,
                        ),
                    )
                except Exception:
                    logger.exception("Error updating user premium flags from Stripe webhook")
                conn.commit()
                conn.close()
    return JSONResponse({"ok": True})