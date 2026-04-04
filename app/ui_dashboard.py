from __future__ import annotations

import html as html_lib
import json
import logging
import re
from datetime import date, datetime, timezone, timedelta
from typing import Any

from fastapi import Request

from config.settings import settings
from data.cache import read_json, read_json_with_fallback, read_cache_meta
from data.providers.football_data import get_unsupported_leagues
from app.routes.matches import group_matches_by_day
from app.timefmt import AFTR_DISPLAY_TZ, format_match_kickoff_ar, parse_utc_instant
from app.auth import get_user_id, get_user_by_id
from app.models import get_active_plan
from app.user_helpers import can_see_all_picks, is_admin, is_premium_active

from app.ui_helpers import (
    AUTH_BOOTSTRAP_JS, AUTH_BOOTSTRAP_SCRIPT,
    _safe_float, _safe_int, _parse_utcdate_str, _parse_utcdate_maybe,
    _norm_market, _pick_market, _is_pick_valid, _serializer,
    _get_user_id, _get_plan_from_cookie, _format_cache_status,
    _pill_bar, _home_league_active_code,
)
from app.ui_picks_calc import (
    _result_norm, _suggest_units, _unit_delta, _pick_stake_units,
    _risk_label_from_conf, _pick_score, _aftr_score, _profit_by_market,
    _roi_spark_points, _pick_local_date, top_picks_with_variety,
    _label_for_date, _WEEKDAY_LABELS,
    group_upcoming_picks_by_day, group_picks_recent_by_day_desc,
)
from app.ui_matches import (
    MATCH_LIVE_STATUSES, _match_live_status_token,
    isMatchFinished, isMatchLive, _live_minute_suffix, _format_live_status_line,
)
from app.ui_data import (
    _extract_score_from_match, _extract_score, _pick_id_for_card,
    _debug_log_live_match_candidates, _load_all_leagues_data,
)
from app.ui_team import (
    TEAM_LOGO_FALLBACK_PATH, LEAGUE_LOGO_PATHS, LEAGUE_LOGO_FALLBACK_PATH,
    FEATURED_LEAGUE_CODES, HOME_NAV_LEAGUES,
    _team_logo_slug, _team_logo_path, _team_with_crest,
)
from app.ui_combos import (
    _combo_leg_kickoff_html, _leg_sig, _combo_sig, _uniq_combos,
    _combo_match_key_for_home, _combo_leg_odds_value,
    _build_combo_of_the_day, _build_combos_by_tier, _build_home_premium_combos,
    _render_home_premium_combo_card, _render_combo_of_the_day,
    _render_combo_card, _render_combo_box,
)
from app.ui_stats import (
    _stat_line, _wdl_badge, _pct_class, _market_key, _to_pct01,
    _bar_single, _chips_from_form, _render_back_stats,
)
from app.ui_card import (
    _finished_card_debug_logged, _pick_odds_display_value,
    _pick_odds_home_line_text, _locked_card, _locked_grid,
    _premium_unlock_card, _render_pick_card, _render_live_match_card,
)
from app.ui_home import _build_home_league_snap_carousel_html

logger = logging.getLogger("aftr.ui")

def dashboard(request: Request, league: str):
    league = league if settings.is_valid_league(league) else settings.default_league
    unsupported = get_unsupported_leagues()
    unsupported_football = {c for c in unsupported if getattr(settings, "league_sport", {}).get(c) != "basketball"}
    if league in unsupported_football:
        league = settings.default_league

    auth_param = (request.query_params.get("auth") or "").strip().lower()
    signup_modal_style = "display:flex" if auth_param == "register" else "display:none"
    login_modal_style = "display:flex" if auth_param == "login" else "display:none"

    uid = get_user_id(request)
    user = get_user_by_id(uid) if uid else None
    if uid and not user:
        # broken cookie: uid not in DB; treat as logged out (middleware clears cookie)
        uid, user = None, None

    auth_html = ""
    if user:
        display_name = html_lib.escape((user.get("username") or user.get("email") or ""))
        auth_html = (
            f'<span class="plan-badge">{display_name}</span>'
            f'<a class="plan-logout" href="/account">Mi cuenta</a>'
            f'<a class="plan-logout" href="/auth/logout">Salir</a>'
        )
    else:
        # On league dashboard, also route to auth pages so the modal logic can run via ?auth=...
        auth_html = (
            '<a class="pill" href="/?auth=login">Entrar</a>'
            '<a class="pill" href="/?auth=register">Crear cuenta</a>'
        )

    is_admin_user = is_admin(user, request)
    can_see_all_picks_val = can_see_all_picks(user, request)
    plan = get_active_plan(uid) if uid else settings.plan_free
    is_free_mode = league in settings.free_leagues

    plan_badge = auth_html
    if is_admin_user:
        plan_badge = '<span class="plan-badge admin">ADMIN</span>' + auth_html
    elif plan == settings.plan_pro:
        plan_badge = '<span class="plan-badge pro">PRO</span>' + auth_html
    elif is_premium_active(user) or plan == settings.plan_premium:
        plan_badge = '<span class="plan-badge premium">PREMIUM</span>' + auth_html

    user_premium = bool(uid and (is_premium_active(user) or plan == settings.plan_premium))

    league_carousel_dashboard_html = (
        '<div class="home-carousel-strip" role="navigation" aria-label="Elegir liga">'
        + _build_home_league_snap_carousel_html(
            request,
            unsupported_football,
            carousel_id="leagueCarouselDash",
            active_league=league,
        )
        + "</div>"
    )

    welcome_banner = ""
    if request.query_params.get("msg") == "cuenta_creada" and user:
        name = user.get("username") or user.get("email") or ""
        welcome_banner = f'<div class="welcome-banner">Cuenta creada con éxito. Bienvenido, {html_lib.escape(name)}.</div>'

    admin_users_link = '<a href="/admin/users">Usuarios</a>' if is_admin_user else ''

    cache_meta = read_cache_meta()
    cache_status_html = _format_cache_status(cache_meta)

    raw_matches = read_json_with_fallback(f"daily_matches_{league}.json") or []
    raw_picks = read_json_with_fallback(f"daily_picks_{league}.json") or []
    matches = [m for m in raw_matches if isinstance(m, dict)]
    raw_picks_list = [p for p in raw_picks if isinstance(p, dict)]
    picks = [p for p in raw_picks_list if _is_pick_valid(p)]
    if len(raw_picks_list) > 0 and len(picks) == 0:
        logger.warning(
            "dashboard: %s had %s raw picks but 0 after _is_pick_valid; using raw picks as fallback",
            league, len(raw_picks_list),
        )
        picks = raw_picks_list
    # Force canonical league code on every pick so combo date windows + match_by_key align
    # (cached picks may carry a different _league/league string — setdefault was not enough for PL).
    for p in picks:
        if isinstance(p, dict):
            p["_league"] = league
    logger.info(
        "dashboard: league=%s raw_matches=%s raw_picks=%s after_filter_picks=%s",
        league, len(matches), len(raw_picks_list), len(picks),
    )
    if len(raw_picks_list) > 0 and len(picks) == 0:
        logger.warning("dashboard: had raw_picks but 0 after filter; fallback applied.")
    elif len(picks) == 0:
        logger.warning("dashboard: no picks (raw was empty for league=%s).", league)

    match_by_id: dict[int, dict] = {}
    for m in matches:
        if not isinstance(m, dict):
            continue
        mid = m.get("match_id")
        if mid is None:
            mid = m.get("id")
        mid_i = _safe_int(mid)
        if mid_i is not None:
            match_by_id[mid_i] = m

    upcoming_picks: list[dict] = []
    settled_picks: list[dict] = []
    for p in picks:
        mid = _safe_int(p.get("match_id") or p.get("id"))
        match_obj = match_by_id.get(mid) if mid is not None else None
        finished = isMatchFinished(p) or (isMatchFinished(match_obj) if isinstance(match_obj, dict) else False)
        if finished:
            settled_picks.append(p)
        else:
            upcoming_picks.append(p)

    def _model_rank(p: dict) -> int:
        return 0 if (p.get("model") or "").strip().upper() == "B" else 1

    upcoming_sorted = sorted(
        upcoming_picks,
        key=lambda p: (_model_rank(p), -_pick_score(p)),
    )
    selections = top_picks_with_variety(upcoming_sorted, top_n=10, max_repeats_per_market=3)

    days_with_matches = group_matches_by_day(matches, days=7)
    upcoming_picks_by_day = group_upcoming_picks_by_day(upcoming_picks, days=7)
    matches_by_date = {str(b.get("date", "")): b for b in days_with_matches}
    picks_by_date = {str(b.get("date", "")): b for b in upcoming_picks_by_day}
    today_iso = datetime.now().astimezone().date().isoformat()
    all_dates = sorted(set(matches_by_date.keys()) | set(picks_by_date.keys()))
    upcoming_days = []
    for date_str in all_dates:
        m_block = matches_by_date.get(date_str) or {}
        p_block = picks_by_date.get(date_str) or {}
        label = m_block.get("label") or p_block.get("label") or date_str
        upcoming_days.append({
            "date": date_str,
            "label": label,
            "matches": m_block.get("matches") or [],
            "picks": p_block.get("picks") or [],
        })
    default_upcoming_day = today_iso if any(d.get("date") == today_iso for d in upcoming_days) else (upcoming_days[0].get("date") if upcoming_days else "ALL")

    settled_sorted = sorted(settled_picks, key=lambda p: _parse_utcdate_str(p.get("utcDate")), reverse=True)
    settled_groups = group_picks_recent_by_day_desc(settled_sorted, days=7)
    spark_points = _roi_spark_points(settled_groups)
    market_rows = _profit_by_market(settled_picks)

    # Performance strip (above chart): ROI, acumulado chart, último día
    league_last_spark = spark_points[-1] if spark_points else {}
    league_perf_accum = round(float(league_last_spark.get("v", 0) or 0), 2)
    league_perf_day = round(float(league_last_spark.get("day", 0) or 0), 2)
    _lr = [p for p in settled_picks if _result_norm(p) in ("WIN", "LOSS", "PUSH")]
    _lb = _lr if _lr else list(settled_picks)
    _lts = sum(_pick_stake_units(p) for p in _lb) if _lb else 0.0
    if _lb and _lts > 0:
        _ltp = sum(_unit_delta(p) for p in _lb)
        league_strip_roi_pct = round((_ltp / _lts) * 100.0, 1)
        league_strip_roi_str = f"{league_strip_roi_pct:+.1f}%"
    else:
        league_strip_roi_pct = None
        league_strip_roi_str = "—"
    if league_strip_roi_pct is None:
        league_perf_trend = "neutral"
    elif league_strip_roi_pct > 0:
        league_perf_trend = "up"
    elif league_strip_roi_pct < 0:
        league_perf_trend = "down"
    else:
        league_perf_trend = "flat"
    league_arrow_up = league_perf_trend == "up"
    league_arrow_down = league_perf_trend == "down"
    league_accum_pos = league_perf_accum > 0
    league_accum_neg = league_perf_accum < 0
    league_day_pos = league_perf_day > 0
    league_day_neg = league_perf_day < 0
    has_league_chart = bool(spark_points)
    if has_league_chart:
        league_roi_chart_body = (
            '<canvas id="roiSpark" aria-hidden="true"></canvas>\n'
            '          <div id="roiTip" class="roi-tip" style="display:none;"></div>'
        )
    else:
        league_roi_chart_body = (
            '<div class="perf-chart-empty-state" role="status">'
            '<p class="perf-chart-empty-title">Sin datos suficientes todavía</p>'
            '<p class="perf-chart-empty-sub muted">La curva aparece cuando haya picks resueltos en los últimos días.</p>'
            "</div>"
        )
    _lp = []
    if league_strip_roi_pct is not None:
        if league_strip_roi_pct > 0:
            _lp.append("perf-stat-tile--pos")
        elif league_strip_roi_pct < 0:
            _lp.append("perf-stat-tile--neg")
        else:
            _lp.append("perf-stat-tile--flat")
    else:
        _lp.append("perf-stat-tile--neutral")
    league_primary_tile_class = " ".join(_lp)
    league_arrow_up_style = "display:inline" if league_arrow_up else "display:none"
    league_arrow_down_style = "display:inline" if league_arrow_down else "display:none"

    # Premium combos UI (same component as homepage)
    # - match_by_key keyed by (league, match_id); _league forced above on all picks

    match_by_key = {}
    for mid_i, m in match_by_id.items():
        match_by_key[(league, mid_i)] = m

    premium_combos = _build_home_premium_combos(
        upcoming_picks,
        match_by_key,
        log_context=f"league={league}",
    )
    combos_section_html = "\n".join(_render_home_premium_combo_card(c) for c in premium_combos)
    league_combos_html = f"""
    <section class="home-section">
      <h2 class="home-h2">Combos de Hoy</h2>
      <div class="combos-car" data-combos-carousel>
        <div class="combos-car__viewport">
          <div class="combos-car__track">
            {combos_section_html}
          </div>
        </div>
        <div class="combos-car__controls">
          <button type="button" class="combos-car__btn combos-car__btn--prev" aria-label="Combo anterior">&#8249;</button>
          <div class="combos-car__dots"></div>
          <button type="button" class="combos-car__btn combos-car__btn--next" aria-label="Siguiente combo">&#8250;</button>
        </div>
      </div>
    </section>
    """

    page_html = f"""
    <html>
    <head>
      <meta charset="utf-8"/>
      <title>AFTR Pick</title>
      <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
      <link rel="stylesheet" href="/static/style.css?v=24">
      <link rel="icon" type="image/png" href="/static/logo_aftr.png">

      <link rel="manifest" href="/static/manifest.webmanifest">
      <meta name="theme-color" content="#0b0f14">

      <!-- iOS -->
      <meta name="apple-mobile-web-app-capable" content="yes">
      <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
      <meta name="apple-mobile-web-app-title" content="AFTR">
      <link rel="apple-touch-icon" href="/static/pwa/icon-192.png">
      <link rel="apple-touch-startup-image" href="/static/pwa/splash-1290x2796.png" media="(device-width: 430px) and (device-height: 932px) and (-webkit-device-pixel-ratio: 3)">
      <link rel="apple-touch-startup-image" href="/static/pwa/splash-1179x2556.png" media="(device-width: 393px) and (device-height: 852px) and (-webkit-device-pixel-ratio: 3)">
      <link rel="apple-touch-startup-image" href="/static/pwa/splash-1242x2688.png" media="(device-width: 414px) and (device-height: 896px) and (-webkit-device-pixel-ratio: 3)">
    </head>

    <body>
    """ + AUTH_BOOTSTRAP_SCRIPT + f"""

      <!-- Premium Modal (afuera de .page) -->
      <div id="premium-modal" class="modal-backdrop" style="display:none;">
        <div class="modal">
          <div class="modal-head">
            <div class="modal-title">⭐ AFTR Premium</div>
            <button class="modal-x" onclick="closePremium()">✕</button>
          </div>

          <div class="modal-body">
            <p class="modal-subtitle">Desbloqueá el motor de apuestas con IA</p>
            <div class="modal-section">Qué incluye</div>
            <ul class="modal-list">
              <li>Todos los picks del día</li>
              <li>Picks con alto AFTR Score</li>
              <li>Apuestas de valor con ventaja positiva</li>
              <li>Picks de todas las ligas</li>
              <li>Análisis avanzado de partidos</li>
              <li>Combinadas de valor inteligentes</li>
              <li>Acceso anticipado a picks</li>
            </ul>

            <div class="modal-price">
              <span class="price-main">$9.99</span>
              <span class="price-sub">/ mes</span>
            </div>
            <p class="modal-cancel">Cancelá cuando quieras</p>

            """ + ('<div class="premium-badge">⭐ Premium activo</div>' if user_premium else '<button class="pill modal-cta" onclick="activatePremium(\'PREMIUM\')">Activar Premium</button>') + """
          </div>
        </div>
      </div>

      <!-- ✅ CONTENIDO CENTRADO -->
      <div class="page">
    <div id="signup-modal" class="modal-backdrop" style="{signup_modal_style}">
      <div class="modal">
        <div class="modal-head">
          <div class="modal-title">Crear cuenta</div>
          <button class="modal-x" onclick="closeSignupModal()">✕</button>
        </div>

        <div class="modal-body">
          <div id="signup-error" class="modal-line" style="color:#c00; display:none;"></div>
          <div class="modal-line">
            <input type="email" id="signup-email" class="email-input" placeholder="Email" required>
          </div>
          <div class="modal-line">
            <input type="text" id="signup-username" class="email-input" placeholder="Usuario" required autocomplete="username">
          </div>
          <div class="modal-line">
            <input type="password" id="signup-password" class="email-input" placeholder="Contraseña" required autocomplete="new-password">
          </div>
          <div class="modal-line">
            <input type="password" id="signup-confirm" class="email-input" placeholder="Confirmar contraseña" required autocomplete="new-password">
          </div>
          <button class="pill modal-cta" onclick="registerSubmit()" style="width:100%;">Crear cuenta</button>
        </div>
      </div>
    </div>

    <div id="login-modal" class="modal-backdrop" style="{login_modal_style}">
      <div class="modal">
        <div class="modal-head">
          <div class="modal-title">Entrar</div>
          <button class="modal-x" onclick="closeLoginModal()">✕</button>
        </div>

        <div class="modal-body">
          <form action="/auth/login" method="post" enctype="application/x-www-form-urlencoded">
            <input type="email" name="email" required autocomplete="username" inputmode="email">
            <input type="password" name="password" required autocomplete="current-password">
            <button type="submit">Entrar</button>
          </form>
          <div class="modal-line" style="margin-top: 12px;">
            <a href="#" onclick="closeLoginModal(); openForgotModal(); return false;" class="muted" style="font-size: 13px;">¿Olvidaste tu contraseña?</a>
          </div>
        </div>
      </div>
    </div>

    <div id="forgot-modal" class="modal-backdrop" style="display:none;">
      <div class="modal">
        <div class="modal-head">
          <div class="modal-title">Recuperar contraseña</div>
          <button class="modal-x" onclick="closeForgotModal()">✕</button>
        </div>
        <div class="modal-body">
          <div id="forgot-error" class="modal-line" style="color:#c00; display:none;"></div>
          <div id="forgot-success" class="modal-line" style="color:#0a0; display:none;"></div>
          <div class="modal-line">
            <input type="email" id="forgot-email" class="email-input" placeholder="tu@email.com">
          </div>
          <button class="pill modal-cta" onclick="forgotSubmit()" style="width:100%;">Enviar enlace</button>
          <p class="muted" style="font-size: 12px; margin-top: 12px;">Si el email existe en AFTR, recibirás un enlace para restablecer la contraseña. Revisá la bandeja y spam.</p>
        </div>
      </div>
    </div>

    <div id="premium-success-modal" class="modal-backdrop premium-success-backdrop" style="display:none;">
      <div class="modal premium-success-modal">
        <div class="premium-success-content">
          <div class="premium-success-icon">✨</div>
          <h3 class="premium-success-title">Premium activado</h3>
          <p class="premium-success-sub">Bienvenido a AFTR Elite</p>
          <p class="premium-success-detail">Todos los picks desbloqueados</p>
          <button type="button" class="pill modal-cta" onclick="closePremiumSuccess()">Continuar</button>
        </div>
        <button class="modal-x premium-success-x" onclick="closePremiumSuccess()" aria-label="Cerrar">✕</button>
      </div>
    </div>
        """
    
    page_html += """
    <div id="welcome-modal" class="modal-backdrop" style="display:none;">
      <div class="modal">
        <div class="modal-head">
          <div class="modal-title">👋 Bienvenido a AFTR</div>
          <button class="modal-x" onclick="closeWelcome()">✕</button>
        </div>

        <div class="modal-body">
          <div class="modal-line"><b>Cómo se usa:</b> elegís liga ➜ mirás Top Picks ➜ tocás una card para ver stats.</div>
          <div class="modal-line"><b>FREE:</b> 3 picks por liga free + resultados y rendimiento.</div>
          <div class="modal-line"><b>PREMIUM:</b> todas las picks + más ligas + combinadas + más data.</div>

          <button class="pill modal-cta" onclick="closeWelcome()">Entendido ✅</button>
          """ + ('<div class="premium-badge" style="margin-top:10px;">⭐ Premium activo</div>' if user_premium else '<button class="pill" style="width:100%; margin-top:10px;" onclick="closeWelcome(); openPremium();">Ver Premium ⭐</button>') + """
        </div>
      </div>
    </div>
    """

    page_html += f"""
      <div class="top top-pro top-pro--with-carousel">
        <div class="brand">
          <img src="/static/logo_aftr.png" class="logo-aftr" alt="AFTR" />
          <div class="brand-text">
            <div class="brand-title">AFTR</div>
            <div class="brand-tag">Motor de apuestas con IA</div>  
          </div>
          {plan_badge}
        </div>
        {welcome_banner}
        {cache_status_html}
        <div class="top-actions top-actions--carousel" role="navigation" aria-label="Liga y enlaces">
          {league_carousel_dashboard_html}
          <div class="links">
            <a href="/">Inicio</a>
            <a href="/?league={league}">Panel</a>
            <a href="/api/matches?league={league}" target="_blank">Matches JSON</a>
            <a href="/api/picks?league={league}" target="_blank">Picks JSON</a>
            {admin_users_link}
          </div>
        </div>
      </div>
    """

    page_html += """
    <script>
      function openWelcome(){
        var m = document.getElementById('welcome-modal');
        if (m) m.style.display = 'flex';
      }

      function closeWelcome(){
        var m = document.getElementById('welcome-modal');
        if (m) m.style.display = 'none';
        try { localStorage.setItem('aftr_welcome_seen', '1'); } catch(e){}
      }

      document.addEventListener('DOMContentLoaded', function(){
        var seen = null;
        try { seen = localStorage.getItem('aftr_welcome_seen'); } catch(e){}
        if (!seen) {
          setTimeout(openWelcome, 450);
        }
      });
    </script>
    <script>

    window.openLoginModal = function() {
      var m = document.getElementById("login-modal");
      if (m) m.style.display = "flex";
    };
    window.closeLoginModal = function() {
      var m = document.getElementById("login-modal");
      if (m) m.style.display = "none";
    };
    window.openSignupModal = function() {
      var m = document.getElementById("signup-modal");
      if (m) m.style.display = "flex";
    };
    window.closeSignupModal = function() {
      var m = document.getElementById("signup-modal");
      if (m) m.style.display = "none";
    };
    function openForgotModal(){
      var m = document.getElementById("forgot-modal");
      if (m) { m.style.display = "flex"; document.getElementById("forgot-error").style.display = "none"; document.getElementById("forgot-success").style.display = "none"; }
    }
    function closeForgotModal(){
      var m = document.getElementById("forgot-modal");
      if (m) m.style.display = "none";
    }
    function forgotSubmit(){
      var email = (document.getElementById("forgot-email") || {}).value.trim();
      var errEl = document.getElementById("forgot-error");
      var okEl = document.getElementById("forgot-success");
      errEl.style.display = "none";
      okEl.style.display = "none";
      if (!email || email.indexOf("@") < 1) { errEl.textContent = "Introduce un email válido."; errEl.style.display = "block"; return; }
      var url = (window.location.origin || (window.location.protocol + "//" + window.location.host)) + "/auth/forgot-password";
      fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, credentials: "include", body: JSON.stringify({ email: email }) })
        .then(function(r){ return r.json(); })
        .then(function(d){
          if (d.ok) { okEl.textContent = "Listo. Si el email existe, recibirás instrucciones."; okEl.style.display = "block"; }
          else { errEl.textContent = d.error === "email_invalido" ? "Introduce un email válido." : (d.error || "Error."); errEl.style.display = "block"; }
        })
        .catch(function(){ errEl.textContent = "Error de conexión."; errEl.style.display = "block"; });
    }

    window.registerSubmit = async function(){
      var email = document.getElementById("signup-email");
      var username = document.getElementById("signup-username");
      var password = document.getElementById("signup-password");
      var confirm = document.getElementById("signup-confirm");
      var errEl = document.getElementById("signup-error");
      if (errEl) { errEl.style.display = "none"; errEl.textContent = ""; }
      var e = email ? email.value.trim() : "";
      var u = username ? username.value.trim() : "";
      var p = password ? password.value : "";
      var c = confirm ? confirm.value : "";
      if (!e || e.indexOf("@") < 1 || e.indexOf(".") < 1) {
        if (errEl) { errEl.textContent = "Introduce un email válido."; errEl.style.display = "block"; }
        return;
      }
      if (!u) {
        if (errEl) { errEl.textContent = "El usuario es obligatorio."; errEl.style.display = "block"; }
        return;
      }
      if (!p) {
        if (errEl) { errEl.textContent = "La contraseña es obligatoria."; errEl.style.display = "block"; }
        return;
      }
      if (p !== c) {
        if (errEl) { errEl.textContent = "Las contraseñas no coinciden."; errEl.style.display = "block"; }
        return;
      }
      try {
        var res = await fetch("/auth/register", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify({
            email: e,
            username: u,
            password: p,
            confirm_password: c
          })
        });
        console.log("Register response status:", res.status);
        var data = await res.json();
        if (res.ok && data.ok) {
          var sm = document.getElementById("signup-modal");
          if (sm) sm.style.display = "none";
          window.location.href = "/?msg=cuenta_creada&user=" + encodeURIComponent(data.username || u);
        } else {
          var msg = data.error || "Error al crear la cuenta.";
          if (data.error === "email_ya_registrado") msg = "Este email ya está registrado.";
          else if (data.error === "username_ya_usado") msg = "Este usuario ya está en uso.";
          else if (data.error === "password_demasiado_larga") msg = "La contraseña es demasiado larga. Usá hasta 72 caracteres.";
          if (errEl) { errEl.textContent = msg; errEl.style.display = "block"; }
        }
      } catch (err) {
        console.error("Register fetch error:", err);
        if (errEl) { errEl.textContent = "Error de conexión. Intenta de nuevo."; errEl.style.display = "block"; }
      }
    };

    function showPremiumSuccess(){
      var m = document.getElementById("premium-success-modal");
      if (m) { m.style.display = "flex"; window._premiumSuccessTimeout = setTimeout(closePremiumSuccess, 5000); }
    }
    function closePremiumSuccess(){
      var m = document.getElementById("premium-success-modal");
      if (m) m.style.display = "none";
      if (window._premiumSuccessTimeout) { clearTimeout(window._premiumSuccessTimeout); window._premiumSuccessTimeout = null; }
      var params = new URLSearchParams(window.location.search);
      if (params.get("msg") === "premium_activated") {
        params.delete("msg");
        var qs = params.toString();
        window.location.href = window.location.pathname + (qs ? "?" + qs : "");
      }
    }
    document.addEventListener("DOMContentLoaded",function(){
      var params = new URLSearchParams(window.location.search);
      var authParam = params.get("auth");
      if (authParam === "login" && typeof window.openLoginModal === "function") {
        window.openLoginModal();
      } else if (authParam === "register" && typeof window.openSignupModal === "function") {
        window.openSignupModal();
      } else if (params.get("msg") === "premium_activated" && typeof window.showPremiumSuccess === "function") {
        window.showPremiumSuccess();
      }
    })

    </script>
    """

    # Mercado rows compactos (inline) para panel unificado
    market_slice = market_rows[:8]
    market_inline_rows = ""
    for r in market_slice:
        net = r["net_units"]
        net_cls = "mkt-row-net--pos" if net >= 0 else "mkt-row-net--neg"
        wr_str = f"{r['winrate']}%" if r["winrate"] is not None else "—"
        market_inline_rows += (
            f'<div class="mkt-row">'
            f'<span class="mkt-row-name">{html_lib.escape(str(r["market"]))}</span>'
            f'<span class="mkt-row-wl muted">W{r["wins"]}-L{r["losses"]}</span>'
            f'<span class="mkt-row-wr muted">{wr_str}</span>'
            f'<span class="mkt-row-net {net_cls}">{("+" if net >= 0 else "")}{net}u</span>'
            f'</div>'
        )
    if not market_inline_rows:
        market_inline_rows = '<p class="muted" style="padding:8px 0">Sin datos todavía.</p>'

    # Panel unificado: KPI oculto (JS lo sigue usando) + Rendimiento + Mercado expandible
    page_html += f"""
      <div class="league-dash-panel">

      <!-- KPI strip oculto: el JS lo usa para actualizar valores via /api/stats/summary -->
      <div id="summary-bar" class="league-kpi-hidden" data-league="{league}" aria-hidden="true">
        <span id="kpi-roi"></span><span id="kpi-total"></span>
        <span id="kpi-wins"></span><span id="kpi-losses"></span>
        <span id="kpi-pending"></span><span id="kpi-net"></span>
      </div>

      <section class="league-perf-block perf-panel-premium" aria-label="Rendimiento">
        <div class="perf-panel-head">
          <h2 class="perf-panel-title">Rendimiento</h2>
          <p class="perf-panel-sub muted">Unidades netas · histórico de picks resueltos</p>
        </div>
        <div class="perf-panel-glass league-perf-chart-shell">
          <div class="perf-strip-stats" role="group" aria-label="Resumen de rendimiento">
            <div class="perf-stat-tile perf-stat-tile--primary {league_primary_tile_class}">
              <span class="perf-stat-arrow perf-stat-arrow--up" aria-hidden="true" style="{league_arrow_up_style}">↑</span>
              <span class="perf-stat-arrow perf-stat-arrow--down" aria-hidden="true" style="{league_arrow_down_style}">↓</span>
              <span class="perf-stat-value" id="perf-strip-roi">{league_strip_roi_str}</span>
              <span class="perf-stat-label">ROI total</span>
            </div>
            <div class="perf-stat-tile{' perf-stat-tile--pos' if league_accum_pos else ''}{' perf-stat-tile--neg' if league_accum_neg else ''}">
              <span class="perf-stat-value" id="perf-strip-accum">{league_perf_accum:+.2f}u</span>
              <span class="perf-stat-label">Profit acum.</span>
            </div>
            <div class="perf-stat-tile{' perf-stat-tile--pos' if league_day_pos else ''}{' perf-stat-tile--neg' if league_day_neg else ''}">
              <span class="perf-stat-value" id="perf-strip-day">{league_perf_day:+.2f}u</span>
              <span class="perf-stat-label">Último día</span>
            </div>
            <div class="perf-stat-tile perf-stat-tile--neutral">
              <span class="perf-stat-value"><span id="kpi-wins-v">—</span>/<span id="kpi-losses-v">—</span></span>
              <span class="perf-stat-label">W / L</span>
            </div>
          </div>
          <div class="roi-spark-head perf-chart-head-inner">
            <div>
              <div class="roi-spark-title">Curva acumulada</div>
              <div class="roi-spark-sub muted">Pasá el mouse para ver el detalle por día</div>
            </div>
          </div>
          <div class="roi-spark-canvas perf-chart-canvas-wrap">
            {league_roi_chart_body}
          </div>
          <details class="perf-market-details">
            <summary class="perf-market-toggle">
              Por mercado
              <span class="perf-market-count">{len(market_slice)} mercados</span>
              <span class="perf-market-chevron">›</span>
            </summary>
            <div class="perf-market-rows">
              {market_inline_rows}
            </div>
          </details>
        </div>
      </section>
      </div>

      <script>
        window.AFTR_ROI_POINTS = {json.dumps(spark_points)};
        // Populate W/L tiles from async KPI data once loaded
        (function() {{
          var bar = document.getElementById('summary-bar');
          if (!bar) return;
          var league = bar.dataset.league;
          fetch('/api/stats/summary?league=' + league)
            .then(function(r){{ return r.json(); }})
            .then(function(d){{
              var wins = d.wins ?? d.won ?? '—';
              var losses = d.losses ?? d.lost ?? '—';
              var wv = document.getElementById('kpi-wins-v');
              var lv = document.getElementById('kpi-losses-v');
              if (wv) wv.textContent = wins;
              if (lv) lv.textContent = losses;
              // Also populate hidden spans for any other JS that uses them
              ['roi','total','wins','losses','pending','net'].forEach(function(k){{
                var el = document.getElementById('kpi-' + k);
                if (el && d[k] !== undefined) el.textContent = d[k];
              }});
            }}).catch(function(){{}});
        }})();
      </script>

      <script>
        if ("serviceWorker" in navigator) {{
          window.addEventListener("load", function () {{
            navigator.serviceWorker.register("/static/sw.js").catch(function(){{}});
          }});
        }}
      </script>
    """

    # Combos after performance blocks (ROI + mercado)
    page_html += league_combos_html

    # Sección En Vivo (solo si hay partidos en vivo en esta liga)
    live_matches_now = [m for m in matches if isMatchLive(m)]
    if live_matches_now:
        pick_by_match_id: dict[int, dict] = {}
        for p in upcoming_picks:
            mid = _safe_int(p.get("match_id") or p.get("id"))
            if mid is None:
                continue
            existing = pick_by_match_id.get(mid)
            if existing is None or _aftr_score(existing) < _aftr_score(p):
                pick_by_match_id[mid] = p

        live_cards = "".join(
            _render_live_match_card(m, pick_by_match_id.get(_safe_int(m.get("match_id") or m.get("id"))))
            for m in live_matches_now
        )
        page_html += f"""
        <section class="live-section" id="live-section-league">
          <h2 class="home-h2 live-section-title"><span class="live-dot live-dot--title"></span> En Vivo</h2>
          <div class="live-grid">{live_cards}</div>
        </section>
        """

    # Upcoming: day filter by date (value=YYYY-MM-DD); default "Hoy" when available
    upcoming_filter_opts = ['<option value="ALL">Todos</option>']
    for d in upcoming_days:
        date_val = d.get("date", "")
        lab = d.get("label", "")
        sel = ' selected' if date_val == default_upcoming_day else ''
        upcoming_filter_opts.append(f'<option value="{html_lib.escape(date_val)}"{sel}>{html_lib.escape(lab)}</option>')
    upcoming_filter_options_html = "".join(upcoming_filter_opts)
    page_html += f"""
      <div class="filterbar">
        <span class="filter-label">Día</span>
        <select id="upcoming-filter" class="day-select" data-default-day="{html_lib.escape(default_upcoming_day)}">
          {upcoming_filter_options_html}
        </select>
      </div>
    """
    if not picks:
        page_html += '<div class="coming-soon muted">Próximamente nuevos picks</div>'
    elif not upcoming_days:
        if not matches:
            page_html += "<p class='muted'>No hay matches JSON para esta liga (todavía).</p>"
        else:
            page_html += "<p class='muted'>No hay partidos ni picks en los próximos 7 días.</p>"
    else:
        for day_block in upcoming_days:
            date_str = str(day_block.get("date", ""))
            label = str(day_block.get("label", ""))
            day_matches = day_block.get("matches") or []
            day_picks = day_block.get("picks") or []
            count_m = len(day_matches)
            count_p = len(day_picks)

            page_html += f"""
            <h3 class="day-title day-title-upcoming" data-day="{html_lib.escape(date_str)}">
              {html_lib.escape(label)} ({count_m} partido{"s" if count_m != 1 else ""}, {count_p} pick{"s" if count_p != 1 else ""})
            </h3>
            <div class="day-block upcoming-block" data-day="{html_lib.escape(date_str)}">
            <div class="grid">
            """
            for m in day_matches:
                if not isinstance(m, dict):
                    continue
                home_part = _team_with_crest(m.get("home_crest"), m.get("home", ""))
                away_part = _team_with_crest(m.get("away_crest"), m.get("away", ""))
                page_html += f"""
              <div class="card">
                <div class="row">{home_part} <span class="vs">vs</span> {away_part}</div>
                <div class="meta" data-utc="{html_lib.escape(str(m.get('utcDate','')))}">
                  {html_lib.escape(format_match_kickoff_ar(m.get("utcDate")))}
                </div>
              </div>
                """
            page_html += "</div>"
            if day_picks:
                page_html += """
            <div class="day-picks-wrap" style="margin-top:14px;">
              <h4 class="muted" style="margin-bottom:8px; font-size:13px;">Selecciones</h4>
            <div class="grid">
            """
                day_picks_sorted = sorted(
                    day_picks,
                    key=lambda p: (-(p.get("aftr_score") or 0), _model_rank(p), -_pick_score(p)),
                )
                pick_list = top_picks_with_variety(
                    day_picks_sorted,
                    top_n=20,
                    max_repeats_per_market=5,
                )
                if can_see_all_picks_val:
                    for p, best in pick_list:
                        page_html += _render_pick_card(p, best, match_by_id=match_by_id)
                else:
                    if not is_free_mode:
                        page_html += _locked_grid(8, "Esta liga es Premium")
                    else:
                        for p, best in pick_list[:3]:
                            page_html += _render_pick_card(p, best, match_by_id=match_by_id)
                        page_html += _premium_unlock_card()
                page_html += "</div></div>"
            page_html += "</div>"

    # =========================================================
    # RESULTADOS (últimos 7 días) — grouped by date
    # =========================================================
    settled_filter_opts = ['<option value="ALL">Todos</option>']
    for day_block in (settled_groups or []):
        date_str = str(day_block.get("date", ""))
        label = str(day_block.get("label", ""))
        settled_filter_opts.append(f'<option value="{html_lib.escape(date_str)}">{html_lib.escape(label)}</option>')
    settled_filter_options_html = "".join(settled_filter_opts)

    page_html += """
      <h2 style="margin-top:22px;">✅ Resultados recientes (últimos 7 días)</h2>
      <div class="filterbar">
        <span class="filter-label">Día</span>
        <select id="settled-filter" class="day-select" data-default-day="ALL">
    """ + settled_filter_options_html + """
        </select>
      </div>
      <div class="section settled">
    """

    page_html += """
      <div class="tabs" id="settled-tabs">
        <button class="tab active" data-target="ALL">Todos</button>
      </div>
    """

    if not settled_sorted:
        page_html += "<p class='muted'>Todavía no hay picks resueltas para mostrar.</p>"
    elif not settled_groups:
        page_html += "<p class='muted'>No hay picks resueltas dentro de los últimos 7 días.</p>"
    else:
        for day_block in settled_groups:
            date_str = str(day_block.get("date", ""))
            label = str(day_block.get("label", ""))
            day_items = day_block.get("matches", []) or []
            count = len(day_items)

            page_html += f"""
            <h3 class="day-title day-title-settled" data-day="{html_lib.escape(date_str)}">
              {html_lib.escape(label)} ({count} pick{"s" if count != 1 else ""})
            </h3>
            <div class="grid day-block settled-block" data-day="{html_lib.escape(date_str)}">
            """

            for p in day_items:
                if not isinstance(p, dict):
                    continue
                page_html += _render_pick_card(p, None, match_by_id=match_by_id)

            page_html += "</div>"

    page_html += """
         <script>
            (function(){
              function clamp(n,a,b){ return Math.max(a, Math.min(b, n)); }

              function paintMarketBars(){
                var els = document.querySelectorAll('.market-fill[data-u]');
                if(!els.length) return;

                var vals = [];
                els.forEach(function(el){
                  vals.push(Number(el.getAttribute('data-u') || 0));
                });

                var maxAbs = 0;
                vals.forEach(function(v){ maxAbs = Math.max(maxAbs, Math.abs(v)); });
                if(maxAbs === 0) maxAbs = 1;

                els.forEach(function(el){
                  var u = Number(el.getAttribute('data-u') || 0);
                  var pct = clamp((Math.abs(u) / maxAbs) * 100, 6, 100);

                  el.classList.remove('pos','neg');
                  el.classList.add(u >= 0 ? 'pos' : 'neg');

                  el.style.display = 'block';
                  el.style.width = '0%';
                  void el.offsetWidth;

                  requestAnimationFrame(function(){
                    el.style.width = pct + '%';
                  });
                });
              }

              window.AFTR_paintMarketBars = paintMarketBars;

              function boot(){
                document.documentElement.style.overflow = '';
                document.body.style.overflow = '';
                paintMarketBars();
                setTimeout(paintMarketBars, 80);
                setTimeout(paintMarketBars, 250);
              }

              if(document.readyState === "loading") {
                document.addEventListener("DOMContentLoaded", boot);
              } else {
                boot();
              }

              window.addEventListener("load", boot);
            })();
            </script>
      """
    page_html += """
         </div> <!-- /section settled -->

            <script>
              function openPremium(){
                var m = document.getElementById('premium-modal');
                if (m) m.style.display = 'flex';
                document.documentElement.style.overflow = 'hidden';
                document.body.style.overflow = 'hidden';
              }

              function closePremium(){
                var m = document.getElementById('premium-modal');
                if (m) m.style.display = 'none';
                document.documentElement.style.overflow = '';
                document.body.style.overflow = '';
              }

              function activatePremium(plan){
                var url = (window.location.origin || (window.location.protocol + '//' + window.location.host)) + '/billing/create-checkout-session';
                fetch(url, {
                  method: 'POST',
                  headers: { 'Content-Type': 'application/json' },
                  credentials: 'include',
                  body: JSON.stringify({})
                })
                .then(function(r){ return r.json().then(function(d){ return { ok: r.ok, data: d }; }); })
                .then(function(result){
                  if (result.ok && result.data && result.data.url) {
                    window.location.href = result.data.url;
                  } else if (result.data && result.data.error === 'need_login') {
                    closePremium();
                    openLoginModal();
                  } else {
                    alert('No se pudo iniciar el checkout: ' + ((result.data && result.data.error) || 'error desconocido'));
                  }
                })
                .catch(function(){
                  alert('Error de conexión con el servidor de pagos.');
                });
              }

              document.addEventListener('click', function(e){
                var m = document.getElementById('premium-modal');
                if (!m || m.style.display !== 'flex') return;
                if (e.target === m) closePremium();
              });


              (function pickActions(){
                var base = window.location.origin || (window.location.protocol + '//' + window.location.host);
                window.__userLoggedIn = null;
                function checkLogin(){
                  if (window.__userLoggedIn !== null) return Promise.resolve(window.__userLoggedIn);
                  return fetch(base + '/user/me', { credentials: 'include' }).then(function(r){ return r.json(); }).then(function(d){
                    window.__userLoggedIn = !!(d && d.ok && d.user);
                    return window.__userLoggedIn;
                  }).catch(function(){ window.__userLoggedIn = false; return false; });
                }
                function toast(msg){
                  var el = document.createElement('div');
                  el.className = 'pick-toast';
                  el.style.cssText = 'position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--card-bg,#1a1a1a);color:#fff;padding:10px 18px;border-radius:8px;font-size:0.9rem;z-index:9999;box-shadow:0 4px 12px rgba(0,0,0,0.3);';
                  el.textContent = msg;
                  document.body.appendChild(el);
                  setTimeout(function(){ if (el.parentNode) el.parentNode.removeChild(el); }, 2500);
                }
                function doFavorite(btn){
                  var pickId = btn.getAttribute('data-pick-id');
                  if (!pickId) return;
                  checkLogin().then(function(loggedIn){
                    if (!loggedIn){ alert('Iniciá sesión para usar esta función'); return; }
                    if (btn.disabled) return;
                    btn.disabled = true;
                    var payload = { pick_id: pickId };
                    var market = btn.getAttribute('data-market'); if (market) payload.market = market;
                    var aftr = btn.getAttribute('data-aftr-score'); if (aftr !== null && aftr !== '') payload.aftr_score = parseInt(aftr, 10);
                    var tier = btn.getAttribute('data-tier'); if (tier) payload.tier = tier;
                    var edge = btn.getAttribute('data-edge'); if (edge !== null && edge !== '') payload.edge = parseFloat(edge);
                    var home = btn.getAttribute('data-home-team'); if (home) payload.home_team = home;
                    var away = btn.getAttribute('data-away-team'); if (away) payload.away_team = away;
                    fetch(base + '/user/favorite', { method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include', body: JSON.stringify(payload) })
                      .then(function(r){ return r.json(); })
                      .then(function(d){ if (d && d.ok){ btn.textContent = 'Guardado ✅'; toast('Pick guardada'); } else { btn.disabled = false; toast(d && d.error || 'Error'); } })
                      .catch(function(){ btn.disabled = false; toast('Error de conexión'); });
                  });
                }
                function doFollow(btn){
                  var pickId = btn.getAttribute('data-pick-id');
                  if (!pickId) return;
                  checkLogin().then(function(loggedIn){
                    if (!loggedIn){ alert('Iniciá sesión para usar esta función'); return; }
                    if (btn.disabled) return;
                    btn.disabled = true;
                    var payload = { pick_id: pickId };
                    var market = btn.getAttribute('data-market'); if (market) payload.market = market;
                    var aftr = btn.getAttribute('data-aftr-score'); if (aftr !== null && aftr !== '') payload.aftr_score = parseInt(aftr, 10);
                    var tier = btn.getAttribute('data-tier'); if (tier) payload.tier = tier;
                    var edge = btn.getAttribute('data-edge'); if (edge !== null && edge !== '') payload.edge = parseFloat(edge);
                    var home = btn.getAttribute('data-home-team'); if (home) payload.home_team = home;
                    var away = btn.getAttribute('data-away-team'); if (away) payload.away_team = away;
                    fetch(base + '/user/follow-pick', { method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include', body: JSON.stringify(payload) })
                      .then(function(r){ return r.json(); })
                      .then(function(d){ if (d && d.ok){ btn.textContent = 'Siguiendo 📈'; toast('Pick seguida'); } else { btn.disabled = false; toast(d && d.error || 'Error'); } })
                      .catch(function(){ btn.disabled = false; toast('Error de conexión'); });
                  });
                }
                function applyPersistedState(){
                  checkLogin().then(function(loggedIn){
                    if (!loggedIn) return;
                    Promise.all([
                      fetch(base + '/user/favorites', { credentials: 'include' }).then(function(r){ return r.json(); }),
                      fetch(base + '/user/followed-ids', { credentials: 'include' }).then(function(r){ return r.json(); })
                    ]).then(function(results){
                      var favData = results[0];
                      var followedData = results[1];
                      var favoriteIds = {};
                      var followedIds = {};
                      if (favData && favData.ok && Array.isArray(favData.favorites)) favData.favorites.forEach(function(x){ favoriteIds[x.pick_id] = true; });
                      if (followedData && followedData.ok && Array.isArray(followedData.pick_ids)) followedData.pick_ids.forEach(function(id){ followedIds[id] = true; });
                      document.querySelectorAll('.btn-favorite-pick').forEach(function(btn){
                        var id = btn.getAttribute('data-pick-id');
                        if (id && favoriteIds[id]){ btn.textContent = 'Guardado ✅'; }
                      });
                      document.querySelectorAll('.btn-follow-pick').forEach(function(btn){
                        var id = btn.getAttribute('data-pick-id');
                        if (id && followedIds[id]){ btn.textContent = 'Siguiendo 📈'; }
                      });
                    }).catch(function(){});
                  });
                }
                document.addEventListener('click', function(e){
                  var fav = e.target.closest && e.target.closest('.btn-favorite-pick');
                  if (fav){ e.preventDefault(); e.stopPropagation(); doFavorite(fav); return; }
                  var fol = e.target.closest && e.target.closest('.btn-follow-pick');
                  if (fol){ e.preventDefault(); e.stopPropagation(); doFollow(fol); return; }
                });
                if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', applyPersistedState);
                else applyPersistedState();
              })();
            </script>
      """
    page_html +="""
      <script>
        (function(){
          function clamp(n,a,b){ return Math.max(a, Math.min(b, n)); }

          function drawSpark(canvasId, points){
            var c = document.getElementById(canvasId);
            var tip = document.getElementById("roiTip");
            if(!c || !points || !points.length) return;

            var ctx = c.getContext('2d');
            var parent = c.parentElement;
            var w = Math.max(320, parent ? parent.clientWidth : c.width);
            var h = 140;
            c.width = w;
            c.height = h;

            // valores
            var vals = points.map(p => Number(p.v || 0));
            var min = Math.min.apply(null, vals);
            var max = Math.max.apply(null, vals);
            if (min === max) { min -= 1; max += 1; }

            var padX = 18, padY = 22;
            var innerW = w - padX*2;
            var innerH = h - padY*2;

            function clamp(n,a,b){ return Math.max(a, Math.min(b, n)); }

            function xAt(i){
              if(points.length === 1) return padX + innerW/2;
              return padX + (innerW * (i/(points.length-1)));
            }
            function yAt(v){
              var t = (v - min) / (max - min);
              return padY + innerH - (t * innerH);
            }

            var pathPts = points.map(function(p, i){
              return { x: xAt(i), y: yAt(Number(p.v || 0)) };
            });

            function redraw(hoverIndex){
              ctx.clearRect(0,0,w,h);

              // grid
              ctx.globalAlpha = 0.28;
              ctx.strokeStyle = "rgba(255,255,255,0.18)";
              ctx.lineWidth = 1;
              for (var i=0;i<3;i++){
                var y = padY + (innerH * (i/2));
                ctx.beginPath(); ctx.moveTo(padX, y); ctx.lineTo(padX+innerW, y); ctx.stroke();
              }
              ctx.globalAlpha = 1;

              // línea 0
              var y0 = yAt(0);
              ctx.globalAlpha = 0.55;
              ctx.strokeStyle = "rgba(255,255,255,0.25)";
              ctx.setLineDash([6,6]);
              ctx.beginPath(); ctx.moveTo(padX, y0); ctx.lineTo(padX+innerW, y0); ctx.stroke();
              ctx.setLineDash([]);
              ctx.globalAlpha = 1;

              // area fill
              ctx.beginPath();
              pathPts.forEach(function(pt, i){
                if(i===0) ctx.moveTo(pt.x, pt.y);
                else ctx.lineTo(pt.x, pt.y);
              });
              ctx.lineTo(pathPts[pathPts.length-1].x, padY+innerH);
              ctx.lineTo(pathPts[0].x, padY+innerH);
              ctx.closePath();

              var grad = ctx.createLinearGradient(0, padY, 0, padY+innerH);
              grad.addColorStop(0, "rgba(120,170,255,0.22)");
              grad.addColorStop(1, "rgba(120,170,255,0.00)");
              ctx.fillStyle = grad;
              ctx.fill();

              // línea
              ctx.lineWidth = 3;
              ctx.strokeStyle = "rgba(120,170,255,0.95)";
              ctx.beginPath();
              pathPts.forEach(function(pt, i){
                if(i===0) ctx.moveTo(pt.x, pt.y);
                else ctx.lineTo(pt.x, pt.y);
              });
              ctx.stroke();

              // puntos (verde/rojo por neto del día)
              pathPts.forEach(function(pt, i){
                var day = Number(points[i].day || 0);
                var col = day > 0 ? "rgba(34,197,94,0.95)"
                        : (day < 0 ? "rgba(239,68,68,0.95)"
                        : "rgba(255,255,255,0.85)");
                ctx.fillStyle = col;
                ctx.beginPath(); ctx.arc(pt.x, pt.y, 3.2, 0, Math.PI*2); ctx.fill();
              });

              // etiqueta
              var last = points[points.length-1];
              var lastV = Number(last.v || 0);
              var lastDay = Number(last.day || 0);
              ctx.fillStyle = "rgba(255,255,255,0.90)";
              ctx.font = "12px system-ui, -apple-system, Segoe UI, Roboto";
              ctx.fillText(
                "Acum: " + (lastV>=0?"+":"") + lastV.toFixed(2) + "u"
                + "  |  Último día: " + (lastDay>=0?"+":"") + lastDay.toFixed(2) + "u",
                padX, 14
              );

              // highlight hover
              if(hoverIndex != null && hoverIndex >= 0){
                var pt = pathPts[hoverIndex];

                ctx.globalAlpha = 0.55;
                ctx.strokeStyle = "rgba(255,255,255,0.20)";
                ctx.lineWidth = 1;
                ctx.beginPath(); ctx.moveTo(pt.x, padY); ctx.lineTo(pt.x, padY+innerH); ctx.stroke();
                ctx.globalAlpha = 1;

                ctx.fillStyle = "rgba(120,170,255,1)";
                ctx.beginPath(); ctx.arc(pt.x, pt.y, 6, 0, Math.PI*2); ctx.fill();
                ctx.fillStyle = "rgba(255,255,255,0.95)";
                ctx.beginPath(); ctx.arc(pt.x, pt.y, 3, 0, Math.PI*2); ctx.fill();
              }
            }

            function nearestIndex(mx){
              var best = 0, bestDist = Infinity;
              for(var i=0;i<pathPts.length;i++){
                var d = Math.abs(pathPts[i].x - mx);
                if(d < bestDist){ bestDist = d; best = i; }
              }
              return best;
            }

            function showTip(i, clientX, clientY){
              if(!tip) return;
              var p = points[i];
              tip.innerHTML =
                "<div><b>" + (p.label || "Día") + "</b></div>"
                + "<div class='muted'>Neto: " + ((Number(p.day||0)>=0?"+":"") + Number(p.day||0).toFixed(2)) + "u</div>"
                + "<div>Acum: " + ((Number(p.v||0)>=0?"+":"") + Number(p.v||0).toFixed(2)) + "u</div>";
              tip.style.display = "block";

              var rect = c.getBoundingClientRect();
              var x = clientX - rect.left;
              var y = clientY - rect.top;

              var tx = clamp(x + 12, 8, rect.width - 220);
              var ty = clamp(y - 10, 8, rect.height - 70);

              tip.style.left = tx + "px";
              tip.style.top = ty + "px";
            }

            function hideTip(){
              if(tip) tip.style.display = "none";
              redraw(-1);
            }

            // inicial
            redraw(-1);

            // eventos
            c.onmousemove = function(e){
              var rect = c.getBoundingClientRect();
              var mx = e.clientX - rect.left;

              if(mx < padX || mx > (padX+innerW)){
                hideTip();
                return;
              }

              var i = nearestIndex(mx);
              redraw(i);
              showTip(i, e.clientX, e.clientY);
            };
            c.onmouseleave = hideTip;
          }

          function boot(){
            var pts = window.AFTR_ROI_POINTS || [];
            drawSpark("roiSpark", pts);
            window.addEventListener("resize", function(){
              drawSpark("roiSpark", window.AFTR_ROI_POINTS || []);
            });
          }

  if(document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
</script>

      <script>
        (function () {
          function fillSelect(selectId, blocksSelector, titleSelector, keepOptionsAndDefault) {
            var sel = document.getElementById(selectId);
            if (!sel) return;

            var blocks = Array.prototype.slice.call(document.querySelectorAll(blocksSelector));
            var titles = Array.prototype.slice.call(document.querySelectorAll(titleSelector));

            function apply() {
              var val = sel.value || 'ALL';
              blocks.forEach(function (b) {
                var d = b.getAttribute('data-day') || '';
                b.style.display = (val === 'ALL' || d === val) ? '' : 'none';
              });
              titles.forEach(function (t) {
                var d = t.getAttribute('data-day') || '';
                t.style.display = (val === 'ALL' || d === val) ? '' : 'none';
              });
            }

            if (keepOptionsAndDefault && sel.options.length > 1) {
              var def = sel.getAttribute('data-default-day');
              if (def) sel.value = def;
              sel.addEventListener('change', apply);
              apply();
              return;
            }

            var days = [];
            blocks.forEach(function (b) {
              var d = b.getAttribute('data-day') || '';
              if (d && days.indexOf(d) === -1) days.push(d);
            });
            while (sel.options.length > 1) sel.remove(1);
            days.forEach(function (d) {
              var opt = document.createElement('option');
              opt.value = d;
              opt.textContent = d;
              sel.appendChild(opt);
            });

            sel.addEventListener('change', apply);
            apply();
          }

          fillSelect('upcoming-filter', '.upcoming-block', '.day-title-upcoming', true);
          fillSelect('settled-filter', '.settled-block', '.day-title-settled', true);
        })();
      </script>

      <!-- (dejé el resto de tus scripts tal cual, podés pegarlos debajo si siguen en tu archivo) -->

      <script>
    document.addEventListener('DOMContentLoaded', function() {
      var bar = document.getElementById('summary-bar');
      if (!bar) return;

      var league = bar.getAttribute('data-league') || '';
      if (!league) return;

      fetch('/api/stats/summary?league=' + encodeURIComponent(league))
        .then(function(r){ return r.ok ? r.json() : null; })
        .then(function(d){
          if (!d) return;

          var settled = (d.wins || 0) + (d.losses || 0) + (d.push || 0);

          var roiEl = document.getElementById('kpi-roi');
          if (roiEl) roiEl.textContent = (settled > 0 && d.roi != null) ? (d.roi + '%') : '—';

          var pr = document.getElementById('perf-strip-roi');
          var ptile = document.querySelector('.league-perf-block .perf-stat-tile--primary');
          if (pr && ptile) {
            var rtxt = (settled > 0 && d.roi != null) ? (String(d.roi).indexOf('%') >= 0 ? String(d.roi) : (d.roi + '%')) : '—';
            pr.textContent = rtxt;
            var rn = (settled > 0 && d.roi != null) ? Number(d.roi) : NaN;
            ptile.classList.remove('perf-stat-tile--pos','perf-stat-tile--neg','perf-stat-tile--flat','perf-stat-tile--neutral');
            var au = ptile.querySelector('.perf-stat-arrow--up');
            var ad = ptile.querySelector('.perf-stat-arrow--down');
            if (au) au.style.display = 'none';
            if (ad) ad.style.display = 'none';
            if (rtxt === '—' || isNaN(rn)) {
              ptile.classList.add('perf-stat-tile--neutral');
            } else if (rn > 0) {
              ptile.classList.add('perf-stat-tile--pos');
              if (au) au.style.display = 'inline';
            } else if (rn < 0) {
              ptile.classList.add('perf-stat-tile--neg');
              if (ad) ad.style.display = 'inline';
            } else {
              ptile.classList.add('perf-stat-tile--flat');
            }
          }

          var el;
          el = document.getElementById('kpi-total'); if (el) el.textContent = (d.total_picks != null) ? d.total_picks : '—';
          el = document.getElementById('kpi-wins'); if (el) el.textContent = (d.wins != null) ? d.wins : '—';
          el = document.getElementById('kpi-losses'); if (el) el.textContent = (d.losses != null) ? d.losses : '—';
          el = document.getElementById('kpi-pending'); if (el) el.textContent = (d.pending != null) ? d.pending : '—';

          var netEl = document.getElementById('kpi-net');
          var netCard = netEl && netEl.closest('.kpi-card');
          if (d.net_units != null && netEl) {
            var n = Number(d.net_units);
            netEl.textContent = (n >= 0 ? '+' : '') + n.toFixed(2);
            if (netCard) netCard.classList.add(n >= 0 ? 'pos' : 'neg');
          } else if (netEl) {
            netEl.textContent = '—';
          }
        })
        .catch(function(){});
    });
    </script>

    <script>
    (function(){
      function clamp(n,a,b){ return Math.max(a, Math.min(b, n)); }
      function paintFront(){
        document.querySelectorAll('.cand-fill[data-w]').forEach(function(el){
          el.style.width = clamp(Number(el.getAttribute('data-w') || 0), 0, 100) + '%';
        });
      }
      document.addEventListener('DOMContentLoaded', function(){ paintFront(); setTimeout(paintFront, 60); });
      window.AFTR_paintFront = paintFront;
    })();
    </script>

    </div> <!-- /page -->
    <script src="/static/aftr-ui.js?v=1" defer></script>
    <div id="match-drawer" class="match-drawer" aria-hidden="true" role="dialog" aria-modal="true">
      <div class="match-drawer-overlay"></div>
      <div class="match-drawer-panel">
        <div class="match-drawer-top">
          <button class="match-drawer-close" aria-label="Cerrar">✕</button>
        </div>
        <div class="match-drawer-body" id="match-drawer-body">
          <div class="md-loading">Cargando...</div>
        </div>
      </div>
    </div>
    </body>
    </html>
    """

    return page_html

