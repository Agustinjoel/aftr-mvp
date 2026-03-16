from __future__ import annotations

import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse
from itsdangerous import URLSafeSerializer, BadSignature
from passlib.hash import bcrypt

from config.settings import settings
from app.db import get_conn
import hashlib

router = APIRouter()
logger = logging.getLogger("aftr.auth")

def _ser():
    return URLSafeSerializer(settings.secret_key, salt="aftr-session")

def set_session(resp: RedirectResponse, user_id: int):
    token = _ser().dumps({"uid": user_id})
    resp.set_cookie(
        "aftr_session", token,
        max_age=60*60*24*30,
        httponly=True,
        samesite="lax",
        path="/",
        secure=False,
    )

# auth.py

from typing import Optional
import sqlite3
import secrets
import hashlib as _hashlib
from fastapi import APIRouter, Request, Form, Body
from fastapi.responses import RedirectResponse, JSONResponse
from passlib.hash import bcrypt
from datetime import datetime, timezone, timedelta

from app.db import get_conn
from config.settings import settings
from itsdangerous import URLSafeSerializer, BadSignature
from app.email_utils import send_email

router = APIRouter()

def _ser():
    return URLSafeSerializer(settings.secret_key, salt="aftr-session")

def set_session(resp: RedirectResponse, user_id: int):
    token = _ser().dumps({"uid": user_id})
    resp.set_cookie(
        "aftr_session", token,
        max_age=60*60*24*30,
        httponly=True,
        samesite="lax",
        path="/",
        secure=False,
    )
    logger.info("set_session called: user_id=%s (set_cookie aftr_session)", user_id)

def set_session_on_response(resp, user_id: int):
    """Set session cookie on any response (e.g. JSONResponse) for post-register login."""
    token = _ser().dumps({"uid": user_id})
    resp.set_cookie(
        "aftr_session", token,
        max_age=60*60*24*30,
        httponly=True,
        samesite="lax",
        path="/",
        secure=False,
    )
    logger.info("set_session_on_response called: user_id=%s (set_cookie aftr_session)", user_id)

def get_user_id(request: Request) -> Optional[int]:
    cookies = request.cookies if hasattr(request, "cookies") and request.cookies else {}
    raw = cookies.get("aftr_session")
    if not raw:
        logger.info("get_user_id: no aftr_session cookie; request.cookies keys=%s", list(cookies.keys()) if cookies else "None")
        return None
    try:
        data = _ser().loads(raw)
        uid = int(data.get("uid"))
        logger.info("get_user_id: cookie present, decoded uid=%s", uid)
        return uid
    except (BadSignature, Exception) as e:
        logger.warning("get_user_id: aftr_session cookie present but decode failed: %s", e)
        return None

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
            ) VALUES (?,?,?,?,?,?,?)""",
            (email, username or None, pw_hash, "free_user", "inactive", now, now),
        )
        uid = cur.lastrowid
        conn.commit()
        return int(uid)
    finally:
        conn.close()

def clear_session(resp: RedirectResponse):
    resp.delete_cookie("aftr_session", path="/")

def get_user_by_id(user_id: int) -> dict | None:
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """SELECT id, email, username, role, subscription_status,
               subscription_start, subscription_end, created_at, updated_at
               FROM users WHERE id=?""",
            (user_id,),
        )
    except sqlite3.OperationalError:
        cur.execute("SELECT id, email, created_at FROM users WHERE id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    user = dict(row)
    if "username" not in user:
        user["username"] = None
    for key in ("role", "subscription_status", "subscription_start", "subscription_end", "updated_at"):
        if key not in user:
            user[key] = "free_user" if key == "role" else ("inactive" if key == "subscription_status" else None)
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
def register(payload: dict = Body(...)):
    print("REGISTER PAYLOAD:", payload)
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
            cur.execute("SELECT id FROM users WHERE email=?", (email,))
            if cur.fetchone():
                return JSONResponse(content={"ok": False, "error": "email_ya_registrado"}, status_code=400)
            cur.execute("SELECT id FROM users WHERE username=?", (username,))
            if cur.fetchone():
                return JSONResponse(content={"ok": False, "error": "username_ya_usado"}, status_code=400)
        finally:
            conn.close()

        uid = create_user(email, username, password)
        resp = JSONResponse(content={"ok": True, "username": username})
        set_session_on_response(resp, uid)
        return resp
    except ValueError as e:
        print("REGISTER ERROR:", e)
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "La contraseña es demasiado larga. Usa una más corta."},
        )
    except Exception as e:
        print("REGISTER ERROR:", e)
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e)},
        )

@router.post("/auth/signup")
def signup_lead(payload: dict = Body(...)):
    email = (payload.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return JSONResponse({"ok": False, "error": "email_invalido"}, status_code=400)

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS leads (email TEXT PRIMARY KEY, created_at TEXT)")
        cur.execute(
            "INSERT OR IGNORE INTO leads(email, created_at) VALUES (?, ?)",
            (email, datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
    finally:
        conn.close()

    resp = JSONResponse({"ok": True})
    resp.set_cookie("aftr_user", email, max_age=60*60*24*365, samesite="lax", path="/")
    return resp

@router.post("/auth/login")
def login(email: str = Form(...), password: str = Form(...)):
    """Form login (browser). Sets aftr_session cookie and redirects.

    The `email` field may contain either the user's email or username.
    """
    identifier = (email or "").strip()
    if not identifier:
        return RedirectResponse(url="/?msg=login_fail", status_code=302)
    if _password_too_long(password or ""):
        return RedirectResponse(url="/?msg=login_fail", status_code=302)

    try:
        conn = get_conn()
        cur = conn.cursor()
        # Allow login by email (normalized to lowercase) OR username.
        cur.execute(
            "SELECT id, password_hash FROM users WHERE email = ? OR username = ?",
            ((identifier or "").strip().lower(), identifier),
        )
        row = cur.fetchone()
    except Exception as exc:
        logger.exception("login error while querying user: %s", exc)
        row = None
    finally:
        try:
            conn.close()  # type: ignore[has-type]
        except Exception:
            pass

    try:
        if not row or not row["password_hash"] or not bcrypt.verify(password, row["password_hash"]):
            return RedirectResponse(url="/?msg=login_fail", status_code=302)
    except Exception as exc:
        logger.exception("login error during password verification: %s", exc)
        return RedirectResponse(url="/?msg=login_fail", status_code=302)

    uid = int(row["id"])
    resp = RedirectResponse(url="/?msg=login_ok", status_code=302)
    set_session(resp, uid)
    logger.info("login success: user_id=%s, set_cookie called, redirecting to /?msg=login_ok", uid)
    return resp


@router.post("/auth/login/json")
def login_json(payload: dict = Body(...)):
    """JSON login (API/Android). Sets aftr_session cookie and returns user info.

    The `email` field may contain either the user's email or username.
    """
    identifier_raw = payload.get("email") or ""
    identifier = identifier_raw.strip()
    password = payload.get("password") or ""

    if not identifier:
        return JSONResponse({"ok": False, "error": "email_invalido"}, status_code=400)
    if _password_too_long(password):
        return JSONResponse({"ok": False, "error": "password_demasiado_larga"}, status_code=400)

    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, password_hash, email, username FROM users WHERE email = ? OR username = ?",
            (identifier.lower(), identifier),
        )
        row = cur.fetchone()
    except Exception as exc:
        logger.exception("login_json error while querying user: %s", exc)
        row = None
    finally:
        try:
            conn.close()  # type: ignore[has-type]
        except Exception:
            pass

    try:
        if not row or not row["password_hash"] or not bcrypt.verify(password, row["password_hash"]):
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
               VALUES (?, ?, ?, ?)""",
            (_token_hash(token), user_id, expires.isoformat(), now.isoformat()),
        )
        conn.commit()
        return token
    finally:
        conn.close()

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
               WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?""",
            (h, datetime.now(timezone.utc).isoformat()),
        )
        row = cur.fetchone()
        if not row:
            return None
        user_id = int(row["user_id"])
        cur.execute(
            "UPDATE password_reset_tokens SET used_at = ? WHERE token_hash = ?",
            (datetime.now(timezone.utc).isoformat(), h),
        )
        conn.commit()
        return user_id
    finally:
        conn.close()


@router.post("/auth/forgot-password")
def forgot_password(request: Request, payload: dict = Body(...)):
    email = (payload.get("email") or "").strip().lower()
    if not _email_valid(email):
        return JSONResponse({"ok": False, "error": "email_invalido"}, status_code=400)

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE email = ?", (email,))
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
        conn.close()


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
            "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (pw_hash, now, user_id),
        )
        conn.commit()
    finally:
        conn.close()

    resp = JSONResponse({"ok": True, "message": "Contraseña actualizada. Ya podés iniciar sesión."})
    return resp