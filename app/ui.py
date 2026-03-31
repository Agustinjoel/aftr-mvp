from __future__ import annotations

import html as html_lib
import json
import logging
import os
import re
import unicodedata
from datetime import date, datetime, timezone, timedelta
from typing import Any, Callable

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from config.settings import settings
from data.cache import read_json, read_json_with_fallback, read_cache_meta, write_json
from data.providers.football_data import get_unsupported_leagues
from app.routes.matches import group_matches_by_day
from core.poisson import market_priority

from itsdangerous import URLSafeSerializer, BadSignature

from fastapi import Body
from fastapi.responses import JSONResponse
from app.timefmt import AFTR_DISPLAY_TZ, format_match_kickoff_ar, parse_utc_instant
from app.auth import create_user
from app.auth import get_user_id, get_user_by_id
from app.models import get_active_plan
from app.user_helpers import can_see_all_picks, is_admin, is_premium_active
from fastapi import Form

# ── Helpers extraídos a módulos propios ──────────────────────────────────────
from app.ui_helpers import (                                     # noqa: F401
    AUTH_BOOTSTRAP_JS,
    AUTH_BOOTSTRAP_SCRIPT,
    _safe_float,
    _safe_int,
    _parse_utcdate_str,
    _parse_utcdate_maybe,
    _norm_market,
    _pick_market,
    _is_pick_valid,
    _serializer,
    _get_user_id,
    _get_plan_from_cookie,
    _set_plan_cookie,
    _clear_plan_cookie,
    _format_cache_status,
    _pill_bar,
    _home_league_active_code,
)
from app.ui_picks_calc import (                                  # noqa: F401
    _result_norm,
    _suggest_units,
    _unit_delta,
    _pick_stake_units,
    _risk_label_from_conf,
    _pick_score,
    _aftr_score,
    _profit_by_market,
    _roi_spark_points,
    _pick_local_date,
    top_picks_with_variety,
    _label_for_date,
    _WEEKDAY_LABELS,
    group_upcoming_picks_by_day,
    group_picks_recent_by_day_desc,
)
from app.ui_matches import (                                     # noqa: F401
    MATCH_LIVE_STATUSES,
    _match_live_status_token,
    isMatchFinished,
    isMatchLive,
    _live_minute_suffix,
    _format_live_status_line,
)
from app.ui_data import (                                        # noqa: F401
    _extract_score_from_match,
    _extract_score,
    _pick_id_for_card,
    _debug_log_live_match_candidates,
    _load_all_leagues_data,
)
from app.ui_team import (                                        # noqa: F401
    TEAM_LOGO_FALLBACK_PATH,
    LEAGUE_LOGO_PATHS,
    LEAGUE_LOGO_FALLBACK_PATH,
    FEATURED_LEAGUE_CODES,
    HOME_NAV_LEAGUES,
    _team_logo_slug,
    _team_logo_path,
    _team_with_crest,
)
from app.ui_combos import (                                      # noqa: F401
    _combo_leg_kickoff_html,
    _leg_sig,
    _combo_sig,
    _uniq_combos,
    _combo_match_key_for_home,
    _combo_leg_odds_value,
    _build_combo_of_the_day,
    _build_combos_by_tier,
    _build_home_premium_combos,
    _render_home_premium_combo_card,
    _render_combo_of_the_day,
    _render_combo_card,
    _render_combo_box,
)
from app.ui_stats import (                                       # noqa: F401
    _stat_line,
    _wdl_badge,
    _pct_class,
    _market_key,
    _to_pct01,
    _bar_single,
    _chips_from_form,
    _render_back_stats,
)
from app.ui_card import (                                        # noqa: F401
    _finished_card_debug_logged,
    _pick_odds_display_value,
    _pick_odds_home_line_text,
    _locked_card,
    _locked_grid,
    _premium_unlock_card,
    _render_pick_card,
)

from app.ui_home import (                                        # noqa: F401
    _build_home_league_snap_carousel_html,
    home_page,
)
from app.ui_dashboard import (                                   # noqa: F401
    dashboard,
)
from app.ui_account import (                                     # noqa: F401
    _account_header,
    _account_created_display,
    account_page,
)

router = APIRouter()
logger = logging.getLogger("aftr.ui")
# _finished_card_debug_logged → importado de app.ui_card
HOME_VISIBLE_SNAPSHOT_FILE = "home_visible_picks_snapshot.json"

# Registrar rutas de módulos extraídos en el router principal
router.get("/account", response_class=HTMLResponse)(account_page)


# AUTH_BOOTSTRAP_JS, AUTH_BOOTSTRAP_SCRIPT, _serializer, _get_user_id,
# _get_plan_from_cookie, _norm_market, _pick_market → importados de app.ui_helpers


# _profit_by_market, _set_plan_cookie, _clear_plan_cookie → importados de app.ui_helpers / app.ui_picks_calc


@router.post("/auth/lead")
def signup(data: dict = Body(...)):
    email = (data.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return JSONResponse({"ok": False, "error": "email_invalido"}, status_code=400)

    # guarda en leads (sin password)
    from app.db import get_conn
    from datetime import datetime, timezone

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

@router.get("/premium/activate", include_in_schema=False)
def premium_activate(request: Request, plan: str = "PREMIUM"):
    plan = (plan or "PREMIUM").upper()
    if plan not in (settings.plan_premium, settings.plan_pro):
        plan = settings.plan_premium

    resp = RedirectResponse(url="/", status_code=302)
    _set_plan_cookie(resp, plan)
    return resp


@router.get("/premium/logout", include_in_schema=False)
def premium_logout(request: Request):
    resp = RedirectResponse(url="/", status_code=302)
    _clear_plan_cookie(resp)
    return resp

# _pick_score, _aftr_score → importados de app.ui_picks_calc


# =========================================================
# Helpers UI
# =========================================================
# _team_with_crest → importado de app.ui_team

# _unit_delta, _pick_stake_units, _roi_spark_points, _suggest_units → importados de app.ui_picks_calc
# _safe_float, _safe_int → importados de app.ui_helpers


# _is_pick_valid, _format_cache_status, _parse_utcdate_str → importados de app.ui_helpers
# _pick_local_date, _pill_bar, _home_league_active_code → importados de app.ui_helpers / app.ui_picks_calc



# _build_home_league_snap_carousel_html → importado de app.ui_home
# home_page → importado de app.ui_home


@router.get("/ui", response_class=HTMLResponse, include_in_schema=False)
def ui_same(request: Request, league: str = Query(settings.default_league)):
    return dashboard(request, league)

# _combo_leg_kickoff_html, _leg_sig, _combo_sig, _uniq_combos,
# _combo_match_key_for_home, _combo_leg_odds_value, _build_combo_of_the_day,
# _build_combos_by_tier, _build_home_premium_combos,
# _render_home_premium_combo_card, _render_combo_of_the_day,
# _render_combo_card, _render_combo_box → importados de app.ui_combos
#
# FEATURED_LEAGUE_CODES, LEAGUE_LOGO_PATHS, LEAGUE_LOGO_FALLBACK_PATH,
# TEAM_LOGO_FALLBACK_PATH, HOME_NAV_LEAGUES,
# _team_logo_slug, _team_logo_path, _team_with_crest → importados de app.ui_team


# _debug_log_live_match_candidates, _load_all_leagues_data → importados de app.ui_data


@router.get("/", response_class=HTMLResponse)
def index_or_league(request: Request, league: str | None = Query(None)):
    """Show global home when no league query; otherwise show league dashboard."""
    if league is None or (isinstance(league, str) and league.strip() == ""):
        return home_page(request)
    return dashboard(request, league.strip())



# dashboard → importado de app.ui_dashboard

# _account_header, _account_created_display, account_page → importados de app.ui_account

@router.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request):
    """Admin-only: user management panel with premium/role toggle."""
    user, _, plan_badge = _account_header(request)
    if not user:
        return RedirectResponse(url="/?auth=login", status_code=302)
    if not is_admin(user, request):
        return RedirectResponse(url="/", status_code=302)
    from app.db import get_conn, put_conn
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, email, username, role, subscription_status,
                      subscription_start, subscription_end, created_at
               FROM users ORDER BY id"""
        )
        rows = cur.fetchall()
    finally:
        put_conn(conn)

    msg = (request.query_params.get("msg") or "").strip()
    msg_html = ""
    if msg == "ok":
        msg_html = '<div style="background:rgba(34,197,94,.15);border:1px solid rgba(34,197,94,.3);color:#22c55e;padding:10px 14px;border-radius:8px;margin-bottom:16px;font-size:13px;">✓ Usuario actualizado.</div>'
    elif msg == "err":
        msg_html = '<div style="background:rgba(239,68,68,.15);border:1px solid rgba(239,68,68,.3);color:#ef4444;padding:10px 14px;border-radius:8px;margin-bottom:16px;font-size:13px;">✗ Error al actualizar.</div>'

    user_rows = []
    for r in rows:
        uid = r["id"]
        email = html_lib.escape(str(r.get("email") or ""))
        username = html_lib.escape(str(r.get("username") or "—"))
        role = str(r.get("role") or "free_user")
        sub = str(r.get("subscription_status") or "inactive")
        created = str(r.get("created_at") or "—")[:10]

        is_prem = role in ("premium_user", "admin") or sub == "active"
        is_adm = role == "admin"

        prem_badge = (
            '<span style="background:rgba(234,179,8,.2);color:#eab308;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:700;">PREMIUM</span>'
            if is_prem else
            '<span style="background:rgba(255,255,255,.06);color:#94a3b8;padding:2px 8px;border-radius:999px;font-size:11px;">FREE</span>'
        )
        adm_badge = (
            '<span style="background:rgba(139,92,246,.2);color:#a78bfa;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:700;">ADMIN</span>'
            if is_adm else ""
        )

        toggle_prem = (
            f'<button onclick="setRole({uid},\'free\')" style="background:rgba(239,68,68,.15);color:#ef4444;border:1px solid rgba(239,68,68,.3);padding:4px 10px;border-radius:6px;cursor:pointer;font-size:12px;">Quitar premium</button>'
            if is_prem else
            f'<button onclick="setRole({uid},\'premium\')" style="background:rgba(34,197,94,.15);color:#22c55e;border:1px solid rgba(34,197,94,.3);padding:4px 10px;border-radius:6px;cursor:pointer;font-size:12px;">Dar premium</button>'
        )
        toggle_adm = (
            f'<button onclick="setRole({uid},\'free\')" style="background:rgba(239,68,68,.1);color:#f87171;border:1px solid rgba(239,68,68,.2);padding:4px 10px;border-radius:6px;cursor:pointer;font-size:12px;">Quitar admin</button>'
            if is_adm else
            f'<button onclick="setRole({uid},\'admin\')" style="background:rgba(139,92,246,.15);color:#a78bfa;border:1px solid rgba(139,92,246,.3);padding:4px 10px;border-radius:6px;cursor:pointer;font-size:12px;">Hacer admin</button>'
        )

        user_rows.append(f"""
        <tr style="border-bottom:1px solid rgba(255,255,255,.06);">
          <td style="padding:10px 8px;color:#64748b;font-size:12px;">{uid}</td>
          <td style="padding:10px 8px;font-size:13px;">{email}</td>
          <td style="padding:10px 8px;font-size:13px;color:#94a3b8;">{username}</td>
          <td style="padding:10px 8px;">{prem_badge} {adm_badge}</td>
          <td style="padding:10px 8px;font-size:12px;color:#64748b;">{created}</td>
          <td style="padding:10px 8px;display:flex;gap:6px;flex-wrap:wrap;">{toggle_prem} {toggle_adm}</td>
        </tr>""")

    table_body = "\n".join(user_rows)
    total = len(rows)
    premium_count = sum(1 for r in rows if str(r.get("role") or "") in ("premium_user", "admin") or str(r.get("subscription_status") or "") == "active")

    body = f"""
    <div style="background:var(--bg-deep,#05070c);min-height:100vh;padding:24px;">
    <div style="max-width:1000px;margin:0 auto;">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:24px;flex-wrap:wrap;gap:12px;">
        <div style="display:flex;align-items:center;gap:12px;">
          <img src="/static/logo_aftr.png" style="width:32px;height:32px;" alt="AFTR"/>
          <div>
            <div style="font-weight:800;font-size:18px;">Panel de Admin</div>
            <div style="font-size:12px;color:#64748b;">Gestión de usuarios</div>
          </div>
        </div>
        <div style="display:flex;gap:10px;">
          <a href="/" style="color:#38bdf8;font-size:13px;text-decoration:none;">← Inicio</a>
          <a href="/account" style="color:#64748b;font-size:13px;text-decoration:none;">Mi cuenta</a>
        </div>
      </div>

      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:24px;">
        <div style="background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:16px;">
          <div style="font-size:24px;font-weight:800;">{total}</div>
          <div style="font-size:12px;color:#64748b;margin-top:2px;">Usuarios totales</div>
        </div>
        <div style="background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:16px;">
          <div style="font-size:24px;font-weight:800;color:#eab308;">{premium_count}</div>
          <div style="font-size:12px;color:#64748b;margin-top:2px;">Premium / Admin</div>
        </div>
        <div style="background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:16px;">
          <div style="font-size:24px;font-weight:800;color:#94a3b8;">{total - premium_count}</div>
          <div style="font-size:12px;color:#64748b;margin-top:2px;">Free</div>
        </div>
      </div>

      {msg_html}

      <div style="background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.08);border-radius:14px;overflow:hidden;">
        <div style="padding:14px 16px;border-bottom:1px solid rgba(255,255,255,.06);font-size:13px;font-weight:700;">Usuarios</div>
        <div style="overflow-x:auto;">
          <table style="width:100%;border-collapse:collapse;font-size:13px;">
            <thead>
              <tr style="border-bottom:1px solid rgba(255,255,255,.08);">
                <th style="text-align:left;padding:10px 8px;font-size:11px;color:#64748b;font-weight:600;">ID</th>
                <th style="text-align:left;padding:10px 8px;font-size:11px;color:#64748b;font-weight:600;">EMAIL</th>
                <th style="text-align:left;padding:10px 8px;font-size:11px;color:#64748b;font-weight:600;">USUARIO</th>
                <th style="text-align:left;padding:10px 8px;font-size:11px;color:#64748b;font-weight:600;">PLAN</th>
                <th style="text-align:left;padding:10px 8px;font-size:11px;color:#64748b;font-weight:600;">CREADO</th>
                <th style="text-align:left;padding:10px 8px;font-size:11px;color:#64748b;font-weight:600;">ACCIONES</th>
              </tr>
            </thead>
            <tbody>{table_body}</tbody>
          </table>
        </div>
      </div>
    </div>
    </div>
    <script>
    function setRole(uid, role) {{
      var labels = {{premium: 'dar Premium', admin: 'hacer Admin', free: 'quitar plan'}};
      if (!confirm('¿Confirmar: ' + (labels[role] || role) + ' al usuario #' + uid + '?')) return;
      fetch('/admin/set-role', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{user_id: uid, role: role}})
      }}).then(function(r){{ return r.json(); }}).then(function(d){{
        if (d.ok) window.location.href = '/admin/users?msg=ok';
        else window.location.href = '/admin/users?msg=err';
      }}).catch(function(){{ window.location.href = '/admin/users?msg=err'; }});
    }}
    </script>
    """
    return _simple_page("Admin — AFTR", body)


@router.post("/admin/set-role")
async def admin_set_role(request: Request):
    """Admin-only: update user role and subscription_status."""
    from fastapi.responses import JSONResponse
    from app.db import get_conn, put_conn
    from app.auth import get_user_id, get_user_by_id
    uid = get_user_id(request)
    acting_user = get_user_by_id(uid) if uid else None
    if not is_admin(acting_user, request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
        target_id = int(body["user_id"])
        role = str(body["role"]).strip().lower()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad_request"}, status_code=400)

    if role == "premium":
        new_role = "premium_user"
        new_sub = "active"
    elif role == "admin":
        new_role = "admin"
        new_sub = "active"
    elif role == "free":
        new_role = "free_user"
        new_sub = "inactive"
    else:
        return JSONResponse({"ok": False, "error": "invalid_role"}, status_code=400)

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET role=%s, subscription_status=%s, updated_at=%s WHERE id=%s",
            (new_role, new_sub, datetime.now(timezone.utc).isoformat(), target_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        put_conn(conn)
        return JSONResponse({"ok": False, "error": "db_error"}, status_code=500)
    finally:
        put_conn(conn)
    return JSONResponse({"ok": True})


def _simple_page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>{html_lib.escape(title)}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="stylesheet" href="/static/style.css?v=22">
  <link rel="icon" type="image/png" href="/static/logo_aftr.png">
</head>
<body>
  {body}
  """ + AUTH_BOOTSTRAP_SCRIPT + """
</body>
</html>"""


@router.get("/reset-password", response_class=HTMLResponse)
def reset_password_form_page(request: Request, token: str = Query("")):
    """Reset password form (token in query)."""
    if not token or not token.strip():
        return RedirectResponse(url="/?msg=reset_token_invalido", status_code=302)
    tok = html_lib.escape(token)
    body = f"""
    <div class="top top-pro">
      <div class="brand">
        <img src="/static/logo_aftr.png" class="logo-aftr" alt="AFTR" />
        <div class="brand-text">
          <div class="brand-title">AFTR</div>
          <div class="brand-tag">Nueva contraseña</div>
        </div>
      </div>
    </div>
    <div class="page" style="max-width: 400px; margin: 24px auto;">
      <h2>Nueva contraseña</h2>
      <div id="reset-error" class="modal-line" style="color:#c00; display:none;"></div>
      <form id="reset-form" onsubmit="return submitReset(event);">
        <input type="hidden" name="token" value="{tok}">
        <div class="modal-line">
          <input type="password" id="reset-password" class="email-input" placeholder="Nueva contraseña" required autocomplete="new-password">
        </div>
        <div class="modal-line">
          <input type="password" id="reset-confirm" class="email-input" placeholder="Confirmar contraseña" required autocomplete="new-password">
        </div>
        <button type="submit" class="pill modal-cta" style="width:100%;">Actualizar contraseña</button>
      </form>
      <p class="muted" style="margin-top: 16px;"><a href="/">Volver al inicio</a></p>
    </div>
    <script>
    function submitReset(e) {{
      e.preventDefault();
      var token = document.querySelector('input[name=token]').value;
      var password = document.getElementById('reset-password').value;
      var confirm = document.getElementById('reset-confirm').value;
      var errEl = document.getElementById('reset-error');
      errEl.style.display = 'none';
      if (!password) {{ errEl.textContent = 'La contraseña es obligatoria.'; errEl.style.display = 'block'; return false; }}
      if (password !== confirm) {{ errEl.textContent = 'Las contraseñas no coinciden.'; errEl.style.display = 'block'; return false; }}
      fetch((window.location.origin || (window.location.protocol + '//' + window.location.host)) + '/auth/reset-password', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        credentials: 'include',
        body: JSON.stringify({{ token: token, password: password, confirm_password: confirm }})
      }})
      .then(function(r) {{ return r.json().then(function(d) {{ return {{ ok: r.ok, data: d }}; }}); }})
      .then(function(result) {{
        if (result.ok && result.data.ok) {{
          window.location.href = '/?msg=password_actualizada';
        }} else {{
          var msg = result.data.error || 'Error al actualizar.';
          if (result.data.error === 'token_invalido_o_expirado') msg = 'El enlace expiró o ya fue usado. Pedí uno nuevo.';
          else if (result.data.error === 'password_demasiado_larga') msg = 'La contraseña es demasiado larga. Usá hasta 72 caracteres.';
          errEl.textContent = msg;
          errEl.style.display = 'block';
        }}
      }})
      .catch(function() {{ errEl.textContent = 'Error de conexión.'; errEl.style.display = 'block'; }});
      return false;
    }}
    </script>
    """
    return _simple_page("Nueva contraseña — AFTR", body)