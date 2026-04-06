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
from app.ui_rendimiento import build_rendimiento_page
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
from app.ui_tracker import tracker_page                          # noqa: F401

router = APIRouter()
logger = logging.getLogger("aftr.ui")
# _finished_card_debug_logged → importado de app.ui_card
HOME_VISIBLE_SNAPSHOT_FILE = "home_visible_picks_snapshot.json"

# Registrar rutas de módulos extraídos en el router principal
router.get("/account", response_class=HTMLResponse)(account_page)
router.get("/tracker", response_class=HTMLResponse)(tracker_page)


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

@router.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    """Admin control center — sistema, métricas, picks del día, usuarios."""
    from app.db import get_conn, put_conn
    from data.cache import read_cache_meta, read_json_with_fallback
    from app.ui_picks_calc import _aftr_score, _pick_score, _result_norm, _unit_delta, _pick_stake_units
    from app.ui_data import _load_all_leagues_data

    uid = get_user_id(request)
    acting_user = get_user_by_id(uid) if uid else None
    if not is_admin(acting_user, request):
        return RedirectResponse(url="/", status_code=302)

    # ── Sistema ──────────────────────────────────────────────────────────────
    cache_meta = read_cache_meta()
    last_upd   = cache_meta.get("last_updated") or ""
    refreshing = bool(cache_meta.get("refresh_running"))
    try:
        last_upd_fmt = datetime.fromisoformat(str(last_upd).replace("Z", "+00:00")).strftime("%d/%m %H:%M")
        from datetime import timezone as _tz
        age_min = int((datetime.now(_tz.utc) - datetime.fromisoformat(str(last_upd).replace("Z", "+00:00"))).total_seconds() / 60)
    except Exception:
        last_upd_fmt = last_upd[:16] if last_upd else "—"
        age_min = 9999

    leagues_cache: dict[str, dict] = {}
    total_picks_cached = 0
    total_matches_cached = 0
    for code in settings.league_codes():
        picks = read_json_with_fallback(f"daily_picks_{code}.json")
        matches = read_json_with_fallback(f"daily_matches_{code}.json")
        np = len(picks) if isinstance(picks, list) else 0
        nm = len(matches) if isinstance(matches, list) else 0
        if np or nm:
            tiers = {"elite": 0, "strong": 0, "risky": 0, "pass": 0}
            best_tier = None
            if isinstance(picks, list):
                for _p in picks:
                    t = ((_p.get("tier") or "pass") if isinstance(_p, dict) else "pass").lower()
                    if t in tiers:
                        tiers[t] += 1
                for _t in ("elite", "strong", "risky"):
                    if tiers[_t] > 0:
                        best_tier = _t
                        break
            leagues_cache[code] = {
                "picks": np, "matches": nm,
                "name": settings.leagues.get(code, code),
                "tiers": tiers, "best_tier": best_tier,
            }
        total_picks_cached += np
        total_matches_cached += nm

    sys_ok = not refreshing and age_min < 120 and total_picks_cached > 0
    sys_warn = not sys_ok and total_picks_cached > 0
    sys_dot = "#22c55e" if sys_ok else ("#eab308" if sys_warn else "#ef4444")
    sys_label = "Sistema operativo" if sys_ok else ("Actualizando / datos viejos" if sys_warn else "Sin datos en cache")

    # ── DB stats ─────────────────────────────────────────────────────────────
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, email, username, role, subscription_status, created_at FROM users ORDER BY id DESC")
        all_users = cur.fetchall()
        cur.execute("SELECT COUNT(*) AS n FROM user_picks")
        total_follows = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM user_picks WHERE created_at::timestamptz >= NOW() - INTERVAL '24 hours'")
        follows_today = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM user_picks WHERE created_at::timestamptz >= NOW() - INTERVAL '7 days'")
        follows_7d = cur.fetchone()["n"]
        cur.execute("""
            SELECT pick_id, home_team, away_team, market, COUNT(*) AS n
            FROM user_picks GROUP BY pick_id, home_team, away_team, market
            ORDER BY n DESC LIMIT 8
        """)
        most_followed = cur.fetchall()
        cur.execute("SELECT COUNT(*) AS n FROM users WHERE created_at::timestamptz >= NOW() - INTERVAL '7 days'")
        new_7d = cur.fetchone()["n"]
        cur.execute("SELECT result, COUNT(*) AS n FROM user_picks WHERE result IN ('WIN','LOSS','PUSH') GROUP BY result")
        result_rows = {r["result"]: r["n"] for r in cur.fetchall()}
    finally:
        put_conn(conn)

    total_users   = len(all_users)
    premium_users = sum(1 for r in all_users if str(r.get("role","")) in ("premium_user","admin") or str(r.get("subscription_status","")) == "active")
    free_users    = total_users - premium_users

    wins   = result_rows.get("WIN",  0)
    losses = result_rows.get("LOSS", 0)
    pushes = result_rows.get("PUSH", 0)
    wr     = round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else None

    # ── Top picks ─────────────────────────────────────────────────────────────
    try:
        _, _, _, all_upcoming, _, _ = _load_all_leagues_data()
        top_admin = sorted(all_upcoming, key=lambda p: (-(p.get("aftr_score") or 0), -_pick_score(p)))[:12]
    except Exception:
        top_admin = []

    # ── HTML helpers ──────────────────────────────────────────────────────────
    def kpi(value, label, color="#eaf2ff"):
        return f'<div class="adm-kpi"><span class="adm-kpi-val" style="color:{color};">{html_lib.escape(str(value))}</span><span class="adm-kpi-lbl">{html_lib.escape(label)}</span></div>'

    def badge(text, color):
        return f'<span style="background:rgba(255,255,255,.06);color:{color};border-radius:5px;padding:2px 9px;font-size:11px;font-weight:700;">{html_lib.escape(text)}</span>'

    # Stat KPIs
    kpis_system = "".join([
        kpi(f"{total_picks_cached} picks", "En cache"),
        kpi(f"{len(leagues_cache)} ligas", "Con datos"),
        kpi(f"{last_upd_fmt}", "Última act."),
        kpi("Refreshing..." if refreshing else f"{age_min}m", "Hace"),
    ])

    kpis_users = "".join([
        kpi(total_users, "Usuarios"),
        kpi(premium_users, "Premium", "#eab308"),
        kpi(free_users, "Free", "#94a3b8"),
        kpi(new_7d, "Nuevos 7d", "#22c55e"),
    ])

    kpis_activity = "".join([
        kpi(total_follows, "Follows totales"),
        kpi(follows_today, "Follows hoy", "#38bdf8"),
        kpi(follows_7d, "Follows 7d"),
    ])

    kpis_perf = "".join([
        kpi(wins, "Wins", "#22c55e"),
        kpi(losses, "Losses", "#ef4444"),
        kpi(pushes, "Pushes", "#94a3b8"),
        kpi(f"{wr}%" if wr is not None else "—", "Winrate", "#38bdf8"),
    ])

    # League cards
    def _league_card(v: dict) -> str:
        tiers = v.get("tiers", {})
        total = v["picks"] or 1
        best = v.get("best_tier")
        badge_color = {"elite": "#FFD700", "strong": "#22c55e", "risky": "#f97316"}.get(best or "", "#475569")
        badge_label = {"elite": "ELITE", "strong": "STRONG", "risky": "RISKY"}.get(best or "", "—")
        # tier bar segments
        def seg(key, color):
            pct = round(tiers.get(key, 0) / total * 100)
            return f'<span title="{key} {tiers.get(key,0)}" style="flex:{tiers.get(key,0)};background:{color};height:4px;border-radius:2px;min-width:{2 if tiers.get(key,0) else 0}px;"></span>'
        bar = (
            seg("elite", "#FFD700") +
            seg("strong", "#22c55e") +
            seg("risky", "#f97316") +
            seg("pass", "rgba(255,255,255,.08)")
        )
        badge_html = (
            f'<span style="font-size:10px;font-weight:700;color:{badge_color};'
            f'background:rgba(255,255,255,.05);border-radius:4px;padding:1px 5px;">{badge_label}</span>'
        ) if best else ''
        return (
            f'<div class="adm-league-card">'
            f'<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;">'
            f'<span style="font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:130px;" title="{html_lib.escape(v["name"])}">{html_lib.escape(v["name"])}</span>'
            f'{badge_html}'
            f'</div>'
            f'<div style="display:flex;gap:2px;margin-bottom:6px;">{bar}</div>'
            f'<div style="display:flex;gap:8px;font-size:11px;color:#475569;">'
            f'<span title="elite" style="color:#FFD700;">{tiers.get("elite",0)}e</span>'
            f'<span title="strong" style="color:#22c55e;">{tiers.get("strong",0)}s</span>'
            f'<span title="risky" style="color:#f97316;">{tiers.get("risky",0)}r</span>'
            f'<span style="margin-left:auto;">{v["picks"]}p</span>'
            f'</div>'
            f'</div>'
        )
    league_pills = "".join(_league_card(v) for v in leagues_cache.values())

    # Top picks table rows
    def tier_clr(t):
        return {"elite": "#FFD700", "strong": "#22c55e", "risky": "#FF9800"}.get((t or "").lower(), "#64748b")

    pick_rows = []
    for p in top_admin:
        lc   = html_lib.escape(settings.leagues.get(p.get("_league",""), p.get("_league","—")))
        home = html_lib.escape(str(p.get("home") or "—"))
        away = html_lib.escape(str(p.get("away") or "—"))
        mkt  = html_lib.escape(str(p.get("best_market") or "—"))
        sc   = p.get("aftr_score")
        try: sc = int(round(float(sc))) if sc is not None else _aftr_score(p)
        except: sc = _aftr_score(p)
        tier = str(p.get("tier") or "—").lower()
        edge = p.get("edge")
        try: edge_s = f"{float(edge)*100:+.1f}%" if edge is not None else "—"
        except: edge_s = "—"
        pick_rows.append(
            f'<tr><td>{lc}</td><td>{home} vs {away}</td>'
            f'<td>{mkt}</td>'
            f'<td style="color:#38bdf8;font-weight:700;">{sc}</td>'
            f'<td style="color:{tier_clr(tier)};font-weight:700;">{html_lib.escape(tier.upper())}</td>'
            f'<td style="color:{"#22c55e" if edge and float(edge if edge else 0)>0 else "#94a3b8"};">{html_lib.escape(edge_s)}</td>'
            f'</tr>'
        )
    picks_table = "\n".join(pick_rows) if pick_rows else '<tr><td colspan="6" style="color:#64748b;padding:16px;">Sin picks en cache</td></tr>'

    # Most followed rows
    followed_rows = "".join(
        f'<tr><td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;font-size:11px;color:#64748b;">'
        f'{html_lib.escape(str(r.get("home_team") or ""))} vs {html_lib.escape(str(r.get("away_team") or ""))}</td>'
        f'<td>{html_lib.escape(str(r.get("market") or "—"))}</td>'
        f'<td style="color:#38bdf8;font-weight:700;">{r["n"]}</td></tr>'
        for r in most_followed
    ) or '<tr><td colspan="3" style="color:#64748b;padding:12px;">Sin datos</td></tr>'

    # User management rows
    user_rows_html = []
    for r in list(reversed(all_users)):
        u_id = r["id"]
        email = html_lib.escape(str(r.get("email") or ""))
        uname = html_lib.escape(str(r.get("username") or "—"))
        role  = str(r.get("role") or "free_user")
        sub   = str(r.get("subscription_status") or "inactive")
        created = str(r.get("created_at") or "")[:10]
        is_prem = role in ("premium_user","admin") or sub == "active"
        is_adm  = role == "admin"
        plan_badge = badge("ADMIN","#a78bfa") if is_adm else (badge("PREMIUM","#eab308") if is_prem else badge("FREE","#64748b"))
        btn_prem = (
            f'<button onclick="setRole({u_id},\'free\')" class="adm-btn adm-btn--danger">− Premium</button>'
            if is_prem else
            f'<button onclick="setRole({u_id},\'premium\')" class="adm-btn adm-btn--success">+ Premium</button>'
        )
        user_rows_html.append(
            f'<tr><td style="color:#475569;">{u_id}</td>'
            f'<td>{email}</td><td style="color:#94a3b8;">{uname}</td>'
            f'<td>{plan_badge}</td>'
            f'<td style="color:#475569;">{created}</td>'
            f'<td>{btn_prem}</td></tr>'
        )

    msg = (request.query_params.get("msg") or "").strip()
    msg_html = ""
    if msg == "ok":
        msg_html = '<div class="adm-alert adm-alert--ok">✓ Usuario actualizado.</div>'
    elif msg == "err":
        msg_html = '<div class="adm-alert adm-alert--err">✗ Error al actualizar.</div>'
    elif msg == "refresh_ok":
        msg_html = '<div class="adm-alert adm-alert--ok">✓ Refresh lanzado en background.</div>'

    def section(title, content, extra_header=""):
        return f'''<section class="adm-section">
          <div class="adm-section-head"><span class="adm-section-title">{title}</span>{extra_header}</div>
          {content}
        </section>'''

    body = f"""
    <div class="adm-root">
      <!-- Topbar -->
      <div class="adm-topbar">
        <div class="adm-topbar-brand">
          <img src="/static/logo_aftr.png" class="adm-logo" alt="AFTR">
          <div>
            <div class="adm-topbar-title">Control Center</div>
            <div class="adm-topbar-sub">Panel de administración</div>
          </div>
        </div>
        <div class="adm-topbar-actions">
          <form method="POST" action="/admin/trigger-refresh" style="display:inline;">
            <button type="submit" class="adm-btn adm-btn--primary">⟳ Forzar refresh</button>
          </form>
          <a href="/" class="adm-btn">← Inicio</a>
        </div>
      </div>

      {msg_html}

      <!-- Sistema -->
      {section("Sistema",
        f'<div class="adm-sys-bar">'
        f'<span class="adm-sys-dot" style="background:{sys_dot};box-shadow:0 0 8px {sys_dot};"></span>'
        f'<span class="adm-sys-label">{html_lib.escape(sys_label)}</span>'
        f'</div>'
        f'<div class="adm-kpi-row">{kpis_system}</div>'
        f'<div class="adm-league-pills">{league_pills if league_pills else "<span style=\'color:#64748b;\'>Sin ligas con datos</span>"}</div>'
      )}

      <!-- Métricas de usuarios -->
      {section("Usuarios", f'<div class="adm-kpi-row">{kpis_users}</div>')}

      <!-- Actividad -->
      {section("Actividad",
        f'<div class="adm-kpi-row">{kpis_activity}</div>'
        f'<div class="adm-subsection-title">Picks más seguidos</div>'
        f'<div class="adm-table-wrap"><table class="adm-table">'
        f'<thead><tr><th>Partido</th><th>Mercado</th><th>Follows</th></tr></thead>'
        f'<tbody>{followed_rows}</tbody></table></div>'
      )}

      <!-- Rendimiento del modelo -->
      {section("Rendimiento del modelo (user_picks)",
        f'<div class="adm-kpi-row">{kpis_perf}</div>'
      )}

      <!-- Picks de hoy -->
      {section("Picks activos / próximos",
        f'<div class="adm-table-wrap"><table class="adm-table adm-table--picks">'
        f'<thead><tr><th>Liga</th><th>Partido</th><th>Mercado</th><th>AFTR</th><th>Tier</th><th>Edge</th></tr></thead>'
        f'<tbody>{picks_table}</tbody></table></div>'
      )}

      <!-- Gestión de usuarios -->
      {section("Gestión de usuarios",
        f'{msg_html}'
        f'<div class="adm-table-wrap"><table class="adm-table">'
        f'<thead><tr><th>ID</th><th>Email</th><th>Usuario</th><th>Plan</th><th>Creado</th><th>Acción</th></tr></thead>'
        f'<tbody>{"".join(user_rows_html)}</tbody></table></div>'
      )}

    </div>
    <script>
    function setRole(uid, role) {{
      if (!confirm('¿Confirmar cambio de plan para usuario #' + uid + '?')) return;
      fetch('/admin/set-role', {{
        method:'POST', headers:{{'Content-Type':'application/json'}},
        body: JSON.stringify({{user_id:uid, role:role}})
      }}).then(r=>r.json()).then(d=>{{
        window.location.href = '/admin?msg=' + (d.ok ? 'ok' : 'err');
      }}).catch(()=>{{ window.location.href='/admin?msg=err'; }});
    }}
    </script>
    """
    return HTMLResponse(_admin_page_html("Admin — AFTR", body))


def _admin_page_html(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8"/>
  <title>{html_lib.escape(title)}</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <link rel="icon" type="image/png" href="/static/logo_aftr.png">
  <style>
    *{{box-sizing:border-box;margin:0;padding:0;}}
    body{{background:#070a10;color:#eaf2ff;font-family:system-ui,-apple-system,sans-serif;min-height:100vh;}}
    a{{color:#38bdf8;text-decoration:none;}} a:hover{{filter:brightness(1.15);}}
    .adm-root{{max-width:1200px;margin:0 auto;padding:24px 20px 60px;}}
    /* Topbar */
    .adm-topbar{{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;margin-bottom:28px;padding-bottom:20px;border-bottom:1px solid rgba(255,255,255,.08);}}
    .adm-topbar-brand{{display:flex;align-items:center;gap:12px;}}
    .adm-logo{{width:34px;height:34px;border-radius:8px;}}
    .adm-topbar-title{{font-size:1.2rem;font-weight:800;}}
    .adm-topbar-sub{{font-size:.75rem;color:#475569;}}
    .adm-topbar-actions{{display:flex;gap:10px;flex-wrap:wrap;}}
    /* Buttons */
    .adm-btn{{background:rgba(255,255,255,.07);color:#eaf2ff;border:1px solid rgba(255,255,255,.12);border-radius:8px;padding:7px 14px;font-size:.82rem;font-weight:600;cursor:pointer;transition:background .18s;}}
    .adm-btn:hover{{background:rgba(255,255,255,.12);}}
    .adm-btn--primary{{background:#38bdf8;color:#000;border-color:#38bdf8;box-shadow:0 0 12px rgba(56,189,248,.25);}}
    .adm-btn--primary:hover{{filter:brightness(1.1);}}
    .adm-btn--success{{background:rgba(34,197,94,.15);color:#22c55e;border-color:rgba(34,197,94,.3);}}
    .adm-btn--danger{{background:rgba(239,68,68,.12);color:#ef4444;border-color:rgba(239,68,68,.25);}}
    /* Alerts */
    .adm-alert{{padding:10px 14px;border-radius:8px;font-size:13px;margin-bottom:16px;}}
    .adm-alert--ok{{background:rgba(34,197,94,.12);border:1px solid rgba(34,197,94,.3);color:#22c55e;}}
    .adm-alert--err{{background:rgba(239,68,68,.12);border:1px solid rgba(239,68,68,.3);color:#ef4444;}}
    /* Sections */
    .adm-section{{background:rgba(255,255,255,.02);border:1px solid rgba(255,255,255,.07);border-radius:14px;padding:18px 20px;margin-bottom:18px;}}
    .adm-section-head{{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;}}
    .adm-section-title{{font-size:.75rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#475569;}}
    /* System bar */
    .adm-sys-bar{{display:flex;align-items:center;gap:10px;margin-bottom:14px;}}
    .adm-sys-dot{{width:10px;height:10px;border-radius:50%;flex-shrink:0;}}
    .adm-sys-label{{font-size:.9rem;font-weight:600;}}
    .adm-league-pills{{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px;margin-top:12px;}}
    .adm-league-card{{background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:10px 12px;transition:border-color .15s;}}
    .adm-league-card:hover{{border-color:rgba(255,255,255,.16);}}
    /* KPIs */
    .adm-kpi-row{{display:flex;gap:12px;flex-wrap:wrap;}}
    .adm-kpi{{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:12px 16px;min-width:110px;}}
    .adm-kpi-val{{display:block;font-size:1.4rem;font-weight:800;}}
    .adm-kpi-lbl{{display:block;font-size:.72rem;color:#475569;margin-top:2px;}}
    /* Tables */
    .adm-subsection-title{{font-size:.72rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#334155;margin:14px 0 8px;}}
    .adm-table-wrap{{overflow-x:auto;border-radius:8px;border:1px solid rgba(255,255,255,.06);}}
    .adm-table{{width:100%;border-collapse:collapse;font-size:13px;}}
    .adm-table thead tr{{border-bottom:1px solid rgba(255,255,255,.08);}}
    .adm-table th{{text-align:left;padding:9px 10px;font-size:11px;color:#475569;font-weight:600;letter-spacing:.04em;text-transform:uppercase;}}
    .adm-table td{{padding:9px 10px;border-bottom:1px solid rgba(255,255,255,.04);white-space:nowrap;}}
    .adm-table tbody tr:last-child td{{border-bottom:none;}}
    .adm-table tbody tr:hover td{{background:rgba(255,255,255,.02);}}
    .adm-table--picks td:nth-child(2){{max-width:200px;overflow:hidden;text-overflow:ellipsis;}}
  </style>
</head>
<body>{body}</body>
</html>"""


@router.post("/admin/trigger-refresh", response_class=HTMLResponse)
async def admin_trigger_refresh(request: Request):
    """Admin-only: fire a full tiered refresh in background."""
    uid = get_user_id(request)
    acting_user = get_user_by_id(uid) if uid else None
    if not is_admin(acting_user, request):
        return RedirectResponse(url="/", status_code=302)
    import threading
    from services.tiered_refresh import run_tiered_refresh
    threading.Thread(target=run_tiered_refresh, daemon=True).start()
    return RedirectResponse(url="/admin?msg=refresh_ok", status_code=303)


@router.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request):
    """Redirect to unified admin dashboard."""
    return RedirectResponse(url="/admin", status_code=302)


@router.get("/admin/users-legacy", response_class=HTMLResponse)
def admin_users_page_legacy(request: Request):
    """Admin-only: user management panel with premium/role toggle (legacy)."""
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


@router.get("/admin/picks-debug")
def admin_picks_debug(request: Request):
    """Admin-only: raw picks/matches counts for diagnostics."""
    from fastapi.responses import JSONResponse as _JSONResponse
    from app.db import get_conn, put_conn as _put
    uid = get_user_id(request)
    acting_user = get_user_by_id(uid) if uid else None
    if not is_admin(acting_user, request):
        return _JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM picks")
        total_picks = cur.fetchone()["n"]
        cur.execute("SELECT result, COUNT(*) AS n FROM picks GROUP BY result ORDER BY n DESC")
        by_result = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT COUNT(*) AS n FROM matches")
        total_matches = cur.fetchone()["n"]
        cur.execute("""
            SELECT p.result, COUNT(*) AS n
            FROM picks p
            LEFT JOIN matches m ON m.league=p.league AND m.match_id=p.match_id
            WHERE m.match_id IS NULL
            GROUP BY p.result
        """)
        no_match = [dict(r) for r in cur.fetchall()]
        cur.execute("""
            SELECT p.league, p.match_id, p.result, p.best_market, p.best_fair,
                   m."utcDate", m.status, m.home_goals, m.away_goals, m.home, m.away
            FROM picks p
            LEFT JOIN matches m ON m.league=p.league AND m.match_id=p.match_id
            LIMIT 10
        """)
        sample = [dict(r) for r in cur.fetchall()]
        cur.execute("""
            SELECT m.status, COUNT(*) AS n
            FROM picks p
            JOIN matches m ON m.league=p.league AND m.match_id=p.match_id
            GROUP BY m.status ORDER BY n DESC
        """)
        matches_status = [dict(r) for r in cur.fetchall()]
        return _JSONResponse({
            "total_picks": total_picks,
            "total_matches": total_matches,
            "picks_by_result": by_result,
            "picks_without_match": no_match,
            "matches_status_for_picks": matches_status,
            "sample_10": sample,
        })
    finally:
        _put(conn)


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
  <link rel="stylesheet" href="/static/style.css?v=35">
  <link rel="icon" type="image/png" href="/static/logo_aftr.png">
  <link rel="manifest" href="/static/manifest.json">
  <link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
  <meta name="theme-color" content="#0d1117">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="apple-mobile-web-app-title" content="AFTR">
</head>
<body>
  {body}
  """ + AUTH_BOOTSTRAP_SCRIPT + """
  <script src="/static/aftr-premium.js?v=1" defer></script>
</body>
</html>"""


@router.get("/rendimiento", response_class=HTMLResponse)
def rendimiento_page(request: Request):
    """Página de rendimiento histórico de picks desde la DB."""
    return HTMLResponse(build_rendimiento_page(request))


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


@router.get("/terminos", response_class=HTMLResponse)
def terminos_page(request: Request):
    body = """
    <div class="page" style="max-width:720px;margin:40px auto;padding:24px 20px 64px;">
      <a href="/" style="color:rgba(180,220,255,.8);font-size:.85rem;">← Volver al inicio</a>
      <h1 style="margin:24px 0 4px;font-size:1.6rem;">Términos de Uso</h1>
      <p class="muted" style="margin:0 0 32px;font-size:.82rem;">Última actualización: Abril 2026 · AFTR</p>

      <h2 style="font-size:1.05rem;margin:28px 0 8px;">1. Descripción del servicio</h2>
      <p class="muted">AFTR es una plataforma de análisis estadístico deportivo. Los picks y predicciones son el resultado de modelos cuantitativos (distribuciones de Poisson, valor esperado, edge vs cuota de mercado) y <strong>no constituyen asesoramiento financiero ni garantía de resultados</strong>. AFTR no es una casa de apuestas ni opera como tal.</p>

      <h2 style="font-size:1.05rem;margin:28px 0 8px;">2. Uso responsable</h2>
      <p class="muted">El usuario es el único responsable de sus decisiones de apuesta. AFTR no se hace responsable por pérdidas económicas derivadas del uso del servicio. Las apuestas deportivas implican riesgo económico real — apostá solo lo que podés permitirte perder. Si considerás que tenés un problema con el juego, buscá ayuda profesional.</p>

      <h2 style="font-size:1.05rem;margin:28px 0 8px;">3. Cuentas y acceso</h2>
      <p class="muted">Para usar funcionalidades personalizadas es necesario crear una cuenta con email y contraseña. Sos responsable de mantener la confidencialidad de tus credenciales. Nos reservamos el derecho de suspender cuentas que violen estos términos.</p>

      <h2 style="font-size:1.05rem;margin:28px 0 8px;">4. Prueba gratuita y plan Premium</h2>
      <p class="muted">Los nuevos usuarios reciben <strong>7 días de acceso Premium gratuito</strong> al registrarse. Al vencer la prueba, la cuenta pasa automáticamente al plan gratuito con acceso limitado. El plan Premium es una suscripción mensual de renovación automática procesada por <strong>Mercado Pago</strong>. Podés cancelarla en cualquier momento desde tu cuenta en Mercado Pago. No ofrecemos reembolsos por períodos ya transcurridos salvo lo que exija la ley aplicable.</p>

      <h2 style="font-size:1.05rem;margin:28px 0 8px;">5. Contenido y propiedad intelectual</h2>
      <p class="muted">Todo el contenido, marca, código y modelos de AFTR son propiedad de sus creadores. Está prohibida la reproducción, distribución o uso comercial sin autorización expresa. No podés hacer scraping de los picks ni redistribuirlos.</p>

      <h2 style="font-size:1.05rem;margin:28px 0 8px;">6. Modificaciones del servicio</h2>
      <p class="muted">Nos reservamos el derecho de modificar, suspender o discontinuar el servicio o sus precios con aviso previo razonable. Los cambios en precios no afectan suscripciones activas hasta su próximo ciclo de renovación.</p>

      <h2 style="font-size:1.05rem;margin:28px 0 8px;">7. Limitación de responsabilidad</h2>
      <p class="muted">En la máxima medida permitida por la ley, AFTR no será responsable por daños indirectos, incidentales o consecuentes derivados del uso o la imposibilidad de uso del servicio.</p>

      <h2 style="font-size:1.05rem;margin:28px 0 8px;">8. Contacto</h2>
      <p class="muted">Consultas y reclamos: <a href="mailto:aftrapp@outlook.com" style="color:rgba(180,220,255,.8);">aftrapp@outlook.com</a></p>
    </div>
    """
    return _simple_page("Términos de Uso — AFTR", body)


@router.get("/privacidad", response_class=HTMLResponse)
def privacidad_page(request: Request):
    body = """
    <div class="page" style="max-width:720px;margin:40px auto;padding:24px 20px 64px;">
      <a href="/" style="color:rgba(180,220,255,.8);font-size:.85rem;">← Volver al inicio</a>
      <h1 style="margin:24px 0 4px;font-size:1.6rem;">Política de Privacidad</h1>
      <p class="muted" style="margin:0 0 32px;font-size:.82rem;">Última actualización: Abril 2026 · AFTR</p>

      <h2 style="font-size:1.05rem;margin:28px 0 8px;">1. Responsable del tratamiento</h2>
      <p class="muted">AFTR es el responsable del tratamiento de tus datos personales. Contacto: <a href="mailto:aftrapp@outlook.com" style="color:rgba(180,220,255,.8);">aftrapp@outlook.com</a></p>

      <h2 style="font-size:1.05rem;margin:28px 0 8px;">2. Datos que recopilamos</h2>
      <p class="muted">Recopilamos únicamente los datos necesarios para operar el servicio:</p>
      <ul class="muted" style="padding-left:20px;line-height:1.8;">
        <li><strong>Cuenta:</strong> email y nombre de usuario al registrarse</li>
        <li><strong>Actividad:</strong> picks guardados, seguidos e historial personal</li>
        <li><strong>Pagos:</strong> gestionados íntegramente por Mercado Pago — AFTR no almacena datos de tarjetas ni información bancaria</li>
        <li><strong>Notificaciones push:</strong> endpoint de suscripción del navegador (si das permiso)</li>
        <li><strong>Técnicos:</strong> IP de acceso (para rate limiting y seguridad), logs de errores</li>
      </ul>

      <h2 style="font-size:1.05rem;margin:28px 0 8px;">3. Uso de los datos</h2>
      <p class="muted">Usamos tus datos exclusivamente para: operar y personalizar el servicio, enviar notificaciones que vos activás (push, email transaccional), procesar pagos y prevenir fraude. <strong>No vendemos ni compartimos datos personales con terceros con fines publicitarios.</strong></p>

      <h2 style="font-size:1.05rem;margin:28px 0 8px;">4. Cookies</h2>
      <p class="muted">Usamos una única cookie de sesión (<code>aftr_session</code>), necesaria para mantener tu sesión iniciada. Es una cookie HttpOnly y no puede ser leída por JavaScript de terceros. No usamos cookies de rastreo, analytics de terceros ni publicidad comportamental.</p>

      <h2 style="font-size:1.05rem;margin:28px 0 8px;">5. Retención de datos</h2>
      <p class="muted">Conservamos tus datos mientras tu cuenta esté activa. Si eliminás tu cuenta, borramos tus datos personales en un plazo de 30 días, excepto los que debamos conservar por obligaciones legales.</p>

      <h2 style="font-size:1.05rem;margin:28px 0 8px;">6. Tus derechos</h2>
      <p class="muted">Tenés derecho a acceder, rectificar, eliminar y portar tus datos. Para ejercerlos o para solicitar la eliminación de tu cuenta, escribí a <a href="mailto:aftrapp@outlook.com" style="color:rgba(180,220,255,.8);">aftrapp@outlook.com</a>. Respondemos en un plazo de 15 días hábiles.</p>

      <h2 style="font-size:1.05rem;margin:28px 0 8px;">7. Seguridad</h2>
      <p class="muted">Aplicamos medidas técnicas de seguridad razonables: contraseñas almacenadas con bcrypt, sesiones por cookie HttpOnly, comunicaciones sobre HTTPS, y monitoreo de accesos. Ningún sistema es 100% seguro — en caso de brecha que afecte tus datos, te notificaremos sin demora indebida.</p>

      <h2 style="font-size:1.05rem;margin:28px 0 8px;">8. Cambios a esta política</h2>
      <p class="muted">Podemos actualizar esta política. Los cambios significativos se comunicarán por email o mediante aviso en la plataforma.</p>

      <h2 style="font-size:1.05rem;margin:28px 0 8px;">9. Contacto</h2>
      <p class="muted"><a href="mailto:aftrapp@outlook.com" style="color:rgba(180,220,255,.8);">aftrapp@outlook.com</a></p>
    </div>
    """
    return _simple_page("Privacidad — AFTR", body)
