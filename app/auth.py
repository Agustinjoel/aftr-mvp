from __future__ import annotations

import collections
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Request, Form, Body
from fastapi.responses import RedirectResponse, JSONResponse
from itsdangerous import URLSafeSerializer, BadSignature
from passlib.hash import bcrypt

import secrets
import hashlib as _hashlib

import psycopg2.errors

from app.db import get_conn, put_conn
from config.settings import settings
from app.email_utils import send_email

router = APIRouter()
logger = logging.getLogger("aftr.auth")

# --- Rate limiting (token-bucket, stdlib only) ---
_rl_lock = threading.Lock()
_rl_attempts: dict[str, collections.deque] = {}
_RL_MAX_ATTEMPTS = 10
_RL_WINDOW_SECONDS = 60


def _rate_limit_check(ip: str) -> bool:
    """Returns True if request is allowed, False if IP exceeded the limit."""
    now = time.monotonic()
    with _rl_lock:
        bucket = _rl_attempts.setdefault(ip, collections.deque())
        while bucket and bucket[0] < now - _RL_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= _RL_MAX_ATTEMPTS:
            return False
        bucket.append(now)
        return True

_AFTR_SESSION_MAX_AGE = 60 * 60 * 24 * 30


def _aftr_session_cookie_flags() -> dict:
    """Consistent Set-Cookie / delete_cookie flags (Secure must match on HTTPS)."""
    sec = bool(getattr(settings, "cookie_secure", False))
    return {
        "path": "/",
        "httponly": True,
        "samesite": "lax",
        "secure": sec,
    }


def _ser():
    return URLSafeSerializer(settings.secret_key, salt="aftr-session")


def set_session(resp: RedirectResponse, user_id: int):
    token = _ser().dumps({"uid": user_id})
    kw = _aftr_session_cookie_flags()
    resp.set_cookie(
        "aftr_session",
        token,
        max_age=_AFTR_SESSION_MAX_AGE,
        **kw,
    )
    logger.info("set_session called: user_id=%s (set_cookie aftr_session)", user_id)


def set_session_on_response(resp, user_id: int):
    """Set session cookie on any response (e.g. JSONResponse) for post-register login."""
    token = _ser().dumps({"uid": user_id})
    kw = _aftr_session_cookie_flags()
    resp.set_cookie(
        "aftr_session",
        token,
        max_age=_AFTR_SESSION_MAX_AGE,
        **kw,
    )
    logger.info("set_session_on_response called: user_id=%s (set_cookie aftr_session)", user_id)


def get_user_id(request: Request) -> Optional[int]:
    cookies = request.cookies if hasattr(request, "cookies") and request.cookies else {}
    raw = cookies.get("aftr_session")
    if not raw:
        logger.debug(
            "get_user_id: no aftr_session cookie; request.cookies keys=%s",
            list(cookies.keys()) if cookies else "None",
        )
        return None
    try:
        data = _ser().loads(raw)
        uid = int(data.get("uid"))
        logger.info("get_user_id: cookie present, decoded uid=%s", uid)
        if get_user_by_id(uid) is None:
            logger.warning("get_user_id: uid=%s not found in DB, treating as no session (caller should clear cookie)", uid)
            return None
        return uid
    except BadSignature as e:
        logger.warning("get_user_id: invalid session signature: %s", e)
        return None
    except (TypeError, ValueError) as e:
        logger.warning("get_user_id: session data malformed: %s", e)
        return None


def clear_session_if_invalid(request: Request, response) -> None:
    """If request has aftr_session cookie but the decoded uid is not in DB, clear the cookie on response."""
    raw = (request.cookies or {}).get("aftr_session")
    if not raw:
        return
    try:
        data = _ser().loads(raw)
        uid = int(data.get("uid"))
    except (BadSignature, TypeError, ValueError):
        return
    if get_user_by_id(uid) is None:
        kw = _aftr_session_cookie_flags()
        response.delete_cookie(
            "aftr_session",
            path=kw["path"],
            secure=kw["secure"],
            httponly=kw["httponly"],
            samesite=kw["samesite"],
        )
        logger.info("clear_session_if_invalid: cleared invalid session cookie for uid=%s", uid)


def create_user(email: str, username: str, password: str) -> int:
    email = (email or "").strip().lower()
    username = (username or "").strip()
    if len((password or "").encode("utf-8")) > 72:
        raise ValueError("La contraseña es demasiado larga. Usa una de hasta 72 bytes.")
    pw_hash = bcrypt.hash(password)
    now = datetime.now(timezone.utc).isoformat()

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO users(
                email, username, password_hash, role, subscription_status,
                created_at, updated_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (email, username or None, pw_hash, "free_user", "inactive", now, now),
        )
        uid_int = int(cur.fetchone()["id"])
        conn.commit()
        logger.info("create_user: INSERT done, id=%s (returning this uid)", uid_int)
        return uid_int
    finally:
        put_conn(conn)

def clear_session(resp: RedirectResponse):
    kw = _aftr_session_cookie_flags()
    resp.delete_cookie(
        "aftr_session",
        path=kw["path"],
        secure=kw["secure"],
        httponly=kw["httponly"],
        samesite=kw["samesite"],
    )

def get_user_by_email(email: str) -> dict | None:
    """Look up user by email only. Returns row dict with id, email, password_hash, etc., or None."""
    email = (email or "").strip().lower()
    if not email:
        return None
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE email = %s", (email,))
        row = cur.fetchone()
    finally:
        put_conn(conn)
    if not row:
        return None
    return dict(row)


def get_user_by_id(user_id: int) -> dict | None:
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, email, username, role, subscription_status,
               subscription_start, subscription_end, created_at, updated_at
               FROM users WHERE id = %s""",
            (user_id,),
        )
        row = cur.fetchone()
    finally:
        put_conn(conn)
    if not row:
        return None
    user = dict(row)
    from app.models import get_active_plan
    plan = get_active_plan(user_id)
    if plan and plan.upper() in ("PREMIUM", "PRO"):
        user["role"] = "premium_user"
        user["subscription_status"] = "active"
    return user

BCRYPT_MAX_PASSWORD_BYTES = 72

def _email_valid(e: str) -> bool:
    e = (e or "").strip().lower()
    return bool(e and "@" in e and "." in e and len(e) > 5)

def _password_too_long(password: str) -> bool:
    return len((password or "").encode("utf-8")) > BCRYPT_MAX_PASSWORD_BYTES

@router.post("/auth/register")
def register(request: Request, payload: dict = Body(...)):
    client_ip = request.client.host if request.client else "unknown"
    if not _rate_limit_check(client_ip):
        logger.warning("rate_limit: /auth/register blocked ip=%s", client_ip)
        return JSONResponse({"ok": False, "error": "demasiados_intentos"}, status_code=429)
    logger.debug("register: endpoint hit")
    try:
        email = (payload.get("email") or "").strip().lower()
        username = (payload.get("username") or "").strip()
        password = payload.get("password") or ""
        confirm = payload.get("confirm_password") or ""

        if not _email_valid(email):
            return JSONResponse(content={"ok": False, "error": "email_invalido"}, status_code=400)
        if not username:
            return JSONResponse(content={"ok": False, "error": "username_requerido"}, status_code=400)
        if not password:
            return JSONResponse(content={"ok": False, "error": "password_requerido"}, status_code=400)
        if password != confirm:
            return JSONResponse(content={"ok": False, "error": "password_no_coincide"}, status_code=400)
        if _password_too_long(password):
            return JSONResponse(content={"ok": False, "error": "password_demasiado_larga"}, status_code=400)

        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM users WHERE email = %s", (email,))
            if cur.fetchone():
                return JSONResponse(content={"ok": False, "error": "email_ya_registrado"}, status_code=400)
            cur.execute("SELECT id FROM users WHERE username = %s", (username,))
            if cur.fetchone():
                return JSONResponse(content={"ok": False, "error": "username_ya_usado"}, status_code=400)
        finally:
            put_conn(conn)

        uid = create_user(email, username, password)
        logger.info("register: create_user returned uid=%s, passing to set_session_on_response", uid)
        resp = JSONResponse(content={"ok": True, "username": username})
        set_session_on_response(resp, uid)
        return resp
    except ValueError as e:
        logger.warning("register: validation error: %s", e)
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "La contraseña es demasiado larga. Usá una más corta."},
        )
    except Exception:
        logger.exception("register: unexpected error")
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": "Error interno del servidor."},
        )

@router.post("/auth/signup")
def signup_lead(payload: dict = Body(...)):
    """Create user account with email + password. Always stores password_hash (bcrypt)."""
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    username = (payload.get("username") or email or "").strip() or email

    if not email or "@" not in email:
        return JSONResponse(content={"ok": False, "error": "email_invalido"}, status_code=400)
    if not password or not str(password).strip():
        return JSONResponse(content={"ok": False, "error": "password_requerido"}, status_code=400)
    if _password_too_long(password):
        return JSONResponse(content={"ok": False, "error": "password_demasiado_larga"}, status_code=400)

    try:
        pw_hash = bcrypt.hash(password)
    except Exception as e:
        logger.warning("signup: bcrypt hash failed: %s", e)
        return JSONResponse(
            content={"ok": False, "error": "Error al procesar la contraseña."},
            status_code=500,
        )

    logger.info("signup: password_hash generated OK")

    now = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    new_uid: int | None = None
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO users(
                email, username, password_hash, role, subscription_status,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (email, username or None, pw_hash, "free_user", "inactive", now, now),
        )
        new_uid = int(cur.fetchone()["id"])
        conn.commit()
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return JSONResponse(content={"ok": False, "error": "email_ya_registrado"}, status_code=400)
    finally:
        put_conn(conn)

    resp = JSONResponse(content={"ok": True})
    if new_uid is not None:
        set_session_on_response(resp, new_uid)
    return resp

@router.post("/auth/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    """Form login (browser). Looks up user by email only. Sets aftr_session cookie and redirects."""
    client_ip = request.client.host if request.client else "unknown"
    if not _rate_limit_check(client_ip):
        logger.warning("rate_limit: /auth/login blocked ip=%s", client_ip)
        return RedirectResponse(url="/?msg=demasiados_intentos", status_code=302)
    logger.info("LOGIN ENDPOINT HIT: method=POST path=/auth/login")
    # Temporary debug logs for POST /auth/login code path
    email_normalized = (email or "").strip().lower()
    logger.info("login DEBUG: submitted form email value=%r, password_len=%s", email_normalized, len(password or ""))

    if not email_normalized:
        logger.info("login: empty email, redirecting login_fail")
        return RedirectResponse(url="/?msg=login_fail", status_code=302)
    if _password_too_long(password or ""):
        logger.info("login: password too long, redirecting login_fail")
        return RedirectResponse(url="/?msg=login_fail", status_code=302)

    row = get_user_by_email(email_normalized)
    logger.info("login DEBUG: user lookup by email only, found=%s", bool(row))
    if row:
        logger.info("login DEBUG: fetched row id=%s email=%r username=%r password_hash_null=%s",
                    row.get("id"), row.get("email"), row.get("username"), row.get("password_hash") is None)

    if not row:
        logger.info("login: no user found for email=%r, redirecting login_fail", email_normalized)
        return RedirectResponse(url="/?msg=login_fail", status_code=302)

    has_hash = "password_hash" in row and bool(row["password_hash"])
    logger.info("login DEBUG: password_hash column present=%s", has_hash)

    verify_ok = False
    if has_hash:
        try:
            verify_ok = bcrypt.verify(password, row["password_hash"])
            logger.info("login DEBUG: bcrypt.verify result=%s", verify_ok)
        except Exception as exc:
            logger.exception("login DEBUG: password verification exception: %s", exc)
            verify_ok = False
    else:
        logger.info("login DEBUG: skip verify (no hash), verify_ok=False")

    if not verify_ok:
        logger.info("login: password verification failed, redirecting login_fail (branch=fail)")
        return RedirectResponse(url="/?msg=login_fail", status_code=302)

    uid = int(row["id"])
    logger.info("login: set_session with uid=row[id]=%s (no reuse of any other uid)", uid)
    resp = RedirectResponse(url="/?msg=login_ok", status_code=302)
    set_session(resp, uid)
    logger.info("login success: user_id=%s, email=%r (branch=success)", uid, email_normalized)
    return resp


@router.post("/auth/login/json")
def login_json(request: Request, payload: dict = Body(...)):
    """JSON login (API/Android). Looks up user by email only. Sets aftr_session cookie and returns user info."""
    client_ip = request.client.host if request.client else "unknown"
    if not _rate_limit_check(client_ip):
        logger.warning("rate_limit: /auth/login/json blocked ip=%s", client_ip)
        return JSONResponse({"ok": False, "error": "demasiados_intentos"}, status_code=429)
    email_raw = payload.get("email") or ""
    email_normalized = email_raw.strip().lower()
    password = payload.get("password") or ""

    if not email_normalized:
        return JSONResponse({"ok": False, "error": "email_invalido"}, status_code=400)
    if _password_too_long(password):
        return JSONResponse({"ok": False, "error": "password_demasiado_larga"}, status_code=400)

    row = get_user_by_email(email_normalized)
    try:
        if not row or not row.get("password_hash") or not bcrypt.verify(password, row["password_hash"]):
            return JSONResponse({"ok": False, "error": "credenciales_invalidas"}, status_code=401)
    except Exception as exc:
        logger.exception("login_json error during password verification: %s", exc)
        return JSONResponse({"ok": False, "error": "credenciales_invalidas"}, status_code=401)

    uid = int(row["id"])
    user = get_user_by_id(uid) or {}
    from app.models import get_active_plan
    plan = get_active_plan(uid)
    resp = JSONResponse({
        "ok": True,
        "user": {
            "id": user.get("id"),
            "email": user.get("email"),
            "username": user.get("username"),
            "role": user.get("role"),
            "subscription_status": user.get("subscription_status"),
            "plan": plan,
        },
    })
    set_session_on_response(resp, uid)
    return resp

@router.get("/auth/logout")
def logout():
    resp = RedirectResponse(url="/?msg=bye", status_code=302)
    clear_session(resp)
    return resp


@router.get("/auth/me")
def me(request: Request):
    """Current user info (id, email, username, role, plan). 401 if not logged in."""
    uid = get_user_id(request)
    if not uid:
        return JSONResponse({"ok": False, "error": "not_authenticated"}, status_code=401)
    user = get_user_by_id(uid)
    if not user:
        return JSONResponse({"ok": False, "error": "user_not_found"}, status_code=401)
    from app.models import get_active_plan
    plan = get_active_plan(uid)
    return JSONResponse({
        "ok": True,
        "user": {
            "id": user.get("id"),
            "email": user.get("email"),
            "username": user.get("username"),
            "role": user.get("role"),
            "subscription_status": user.get("subscription_status"),
            "plan": plan,
        },
    })


# ----- Forgot / Reset password (token stored in DB; email delivery can be wired later) -----
RESET_TOKEN_EXPIRY_HOURS = 24

def _token_hash(token: str) -> str:
    return _hashlib.sha256(token.encode("utf-8")).hexdigest()

def _create_reset_token(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=RESET_TOKEN_EXPIRY_HOURS)
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO password_reset_tokens (token_hash, user_id, expires_at, created_at)
               VALUES (%s, %s, %s, %s)""",
            (_token_hash(token), user_id, expires.isoformat(), now.isoformat()),
        )
        conn.commit()
        return token
    finally:
        put_conn(conn)

def _consume_reset_token(token: str) -> Optional[int]:
    """Validate token, mark used, return user_id or None."""
    if not token or not token.strip():
        return None
    h = _token_hash(token.strip())
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT user_id FROM password_reset_tokens
               WHERE token_hash = %s AND used_at IS NULL AND expires_at > %s""",
            (h, datetime.now(timezone.utc).isoformat()),
        )
        row = cur.fetchone()
        if not row:
            return None
        user_id = int(row["user_id"])
        cur.execute(
            "UPDATE password_reset_tokens SET used_at = %s WHERE token_hash = %s",
            (datetime.now(timezone.utc).isoformat(), h),
        )
        conn.commit()
        return user_id
    finally:
        put_conn(conn)


@router.post("/auth/forgot-password")
def forgot_password(request: Request, payload: dict = Body(...)):
    client_ip = request.client.host if request.client else "unknown"
    if not _rate_limit_check(client_ip):
        logger.warning("rate_limit: /auth/forgot-password blocked ip=%s", client_ip)
        return JSONResponse({"ok": True, "message": "Si el email existe, recibirás instrucciones."})
    email = (payload.get("email") or "").strip().lower()
    if not _email_valid(email):
        return JSONResponse({"ok": False, "error": "email_invalido"}, status_code=400)

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        row = cur.fetchone()
        if row:
            user_id = int(row["id"])
            token = _create_reset_token(user_id)
            base_url = (settings.app_base_url or str(request.base_url)).rstrip("/")
            reset_link = f"{base_url}/auth/reset-password?token={token}"
            subject = "AFTR Picks - Reset your password"
            html_body = f"""
            <html>
              <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; color:#111; line-height:1.5;">
                <p>Hello,</p>
                <p>You requested a password reset for your AFTR account.</p>
                <p>Click the link below to set a new password:</p>
                <p><a href="{reset_link}" style="color:#0b5ed7;">Reset your password</a></p>
                <p>If the button does not work, copy and paste this URL in your browser:</p>
                <p style="font-size:13px; color:#555;">{reset_link}</p>
                <p>If you did not request this, you can safely ignore this email.</p>
                <p>AFTR Picks</p>
              </body>
            </html>
            """
            # Fire-and-forget; errors are logged inside send_email
            send_email(email, subject, html_body)
        # Always return success to avoid leaking whether the email exists
        return JSONResponse({"ok": True, "message": "Si el email existe, recibirás instrucciones."})
    finally:
        put_conn(conn)


@router.get("/auth/reset-password")
def reset_password_page(request: Request):
    """Serve the reset-password form page (token in query)."""
    from fastapi.responses import HTMLResponse
    token = request.query_params.get("token") or ""
    # Page is rendered in UI; we just redirect to home with token so the form can be shown
    if not token.strip():
        return RedirectResponse(url="/?msg=reset_token_invalido", status_code=302)
    return RedirectResponse(url=f"/reset-password?token={request.query_params.get('token', '')}", status_code=302)


@router.post("/auth/reset-password")
def reset_password_submit(payload: dict = Body(...)):
    token = (payload.get("token") or "").strip()
    password = payload.get("password") or ""
    confirm = payload.get("confirm_password") or ""

    if not token:
        return JSONResponse({"ok": False, "error": "token_requerido"}, status_code=400)
    if not password:
        return JSONResponse({"ok": False, "error": "password_requerido"}, status_code=400)
    if password != confirm:
        return JSONResponse({"ok": False, "error": "password_no_coincide"}, status_code=400)
    if _password_too_long(password):
        return JSONResponse({"ok": False, "error": "password_demasiado_larga"}, status_code=400)

    user_id = _consume_reset_token(token)
    if not user_id:
        return JSONResponse({"ok": False, "error": "token_invalido_o_expirado"}, status_code=400)

    pw_hash = bcrypt.hash(password)
    now = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET password_hash = %s, updated_at = %s WHERE id = %s",
            (pw_hash, now, user_id),
        )
        conn.commit()
    finally:
        put_conn(conn)

    resp = JSONResponse({"ok": True, "message": "Contraseña actualizada. Ya podés iniciar sesión."})
    return resp