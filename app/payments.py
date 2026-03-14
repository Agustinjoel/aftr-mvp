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
    try:
        uid = get_user_id(request)
        authenticated = uid is not None
        has_secret = bool(settings.stripe_secret_key)
        has_price_id = bool(settings.stripe_price_id)
        has_app_base = bool(getattr(settings, "app_base_url", None) and (settings.app_base_url or "").strip())

        logger.info(
            "create-checkout-session: pre-check",
            extra={
                "authenticated": authenticated,
                "STRIPE_SECRET_KEY_exists": has_secret,
                "STRIPE_PRICE_ID_exists": has_price_id,
                "APP_BASE_URL_exists": has_app_base,
            },
        )

        if not uid:
            logger.info("Checkout session denied: not authenticated")
            return JSONResponse(content={"ok": False, "error": "need_login"}, status_code=401)

        if not stripe or not settings.stripe_secret_key or not settings.stripe_price_id:
            logger.error(
                "Stripe not configured; cannot create checkout session",
                extra={
                    "has_stripe": bool(stripe),
                    "has_secret": has_secret,
                    "has_price_id": has_price_id,
                },
            )
            return JSONResponse(content={"ok": False, "error": "stripe_not_configured"}, status_code=500)

        base_url = (getattr(settings, "app_base_url", None) or "").strip().rstrip("/") or str(request.base_url).rstrip("/")
        success_url = f"{base_url}/billing/success"
        cancel_url = base_url

        logger.info(
            "create-checkout-session: resolved URLs",
            extra={"success_url": success_url, "cancel_url": cancel_url},
        )

        stripe.api_key = settings.stripe_secret_key
        user = get_user_by_id(uid) or {}
        email = user.get("email") or None

        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": settings.stripe_price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            client_reference_id=str(uid),
            customer_email=email,
        )
        logger.info(
            "Stripe checkout session created",
            extra={"user_id": uid, "session_id": getattr(session, "id", None)},
        )
        return JSONResponse(content={"ok": True, "url": session.url})
    except Exception as e:
        logger.exception("create-checkout-session failed: %s", e)
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e)},
        )


@router.get("/billing/success", include_in_schema=False)
def billing_success():
    """After Stripe Checkout success: redirect to dashboard with premium_activated so the success animation shows."""
    return RedirectResponse(url="/?msg=premium_activated", status_code=302)


def _apply_premium_to_user(uid_int: int, expires_at_iso: str, customer_id: str | None = None, subscription_id: str | None = None) -> None:
    """Write PREMIUM into subscriptions and users for given user_id and expiry."""
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO subscriptions(user_id, plan, expires_at, created_at)
        VALUES (?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET plan=excluded.plan, expires_at=excluded.expires_at
        """,
        (uid_int, "PREMIUM", expires_at_iso, now_iso),
    )
    updates = [
        "role = 'premium_user'",
        "subscription_status = 'active'",
        "subscription_start = COALESCE(subscription_start, ?)",
        "subscription_end = ?",
        "updated_at = ?",
    ]
    args = [now_iso, expires_at_iso, now_iso]
    if customer_id is not None:
        updates.append("stripe_customer_id = COALESCE(stripe_customer_id, ?)")
        args.append(customer_id)
    if subscription_id is not None:
        updates.append("stripe_subscription_id = COALESCE(stripe_subscription_id, ?)")
        args.append(subscription_id)
    args.append(uid_int)
    cur.execute(
        "UPDATE users SET " + ", ".join(updates) + " WHERE id = ?",
        tuple(args),
    )
    conn.commit()
    conn.close()


def _revoke_premium_for_user(uid_int: int) -> None:
    """Set subscription to expired/inactive in DB."""
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE subscriptions SET plan = 'FREE', expires_at = ? WHERE user_id = ?",
        (now_iso, uid_int),
    )
    cur.execute(
        """
        UPDATE users SET role = 'free_user', subscription_status = 'inactive', updated_at = ?
        WHERE id = ?
        """,
        (now_iso, uid_int),
    )
    conn.commit()
    conn.close()


def _uid_from_subscription_id(stripe_subscription_id: str) -> int | None:
    """Resolve user_id from users.stripe_subscription_id."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE stripe_subscription_id = ?", (stripe_subscription_id,))
    row = cur.fetchone()
    conn.close()
    return int(row["id"]) if row else None


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

    ev_type = event["type"]

    if ev_type == "checkout.session.completed":
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
                exp_iso = (now + timedelta(days=30)).isoformat()
                if subscription_id and wh_secret:
                    try:
                        sub = stripe.Subscription.retrieve(subscription_id)
                        if getattr(sub, "current_period_end", None):
                            exp_iso = datetime.fromtimestamp(sub.current_period_end, tz=timezone.utc).isoformat()
                    except Exception:
                        pass
                _apply_premium_to_user(uid_int, exp_iso, customer_id=customer_id, subscription_id=subscription_id)

    elif ev_type == "customer.subscription.updated":
        sub = event["data"]["object"]
        sub_id = sub.get("id")
        status = (sub.get("status") or "").strip().lower()
        uid_int = _uid_from_subscription_id(sub_id) if sub_id else None
        if uid_int:
            if status in ("active", "trialing"):
                exp_ts = sub.get("current_period_end")
                exp_iso = datetime.fromtimestamp(exp_ts, tz=timezone.utc).isoformat() if exp_ts else (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
                _apply_premium_to_user(uid_int, exp_iso)
            else:
                _revoke_premium_for_user(uid_int)

    elif ev_type == "customer.subscription.deleted":
        sub = event["data"]["object"]
        sub_id = sub.get("id")
        uid_int = _uid_from_subscription_id(sub_id) if sub_id else None
        if uid_int:
            _revoke_premium_for_user(uid_int)

    return JSONResponse({"ok": True})