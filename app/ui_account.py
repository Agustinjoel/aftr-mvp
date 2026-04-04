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


def _simple_page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>{html_lib.escape(title)}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="stylesheet" href="/static/style.css?v=30">
  <link rel="icon" type="image/png" href="/static/logo_aftr.png">
</head>
<body>
  {body}
  """ + AUTH_BOOTSTRAP_SCRIPT + """
</body>
</html>"""
from app.ui_picks_calc import (
    _result_norm, _suggest_units, _unit_delta, _pick_stake_units,
    _risk_label_from_conf, _pick_score, _aftr_score, _profit_by_market,
    _roi_spark_points, _pick_local_date, top_picks_with_variety,
    _label_for_date, _WEEKDAY_LABELS,
    group_upcoming_picks_by_day, group_picks_recent_by_day_desc,
)

logger = logging.getLogger("aftr.ui")

def _account_header(request: Request):
    """Build (user, auth_html, plan_badge) for account/admin pages."""
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
        auth_html = (
            '<a class="pill" href="/?auth=login">Entrar</a>'
            '<a class="pill" href="/?auth=register">Crear cuenta</a>'
        )
    is_admin_user = is_admin(user, request)
    plan_badge = auth_html
    if is_admin_user:
        plan_badge = '<span class="plan-badge admin">ADMIN</span>' + auth_html
    return user, auth_html, plan_badge


def _account_created_display(created_at) -> str:
    """Format created_at for display (e.g. 2025-03-15 -> 15/03/2025 or 'desde 2025')."""
    if not created_at:
        return "—"
    s = str(created_at).strip()[:10]
    if len(s) >= 10:
        try:
            y, m, d = s[:4], s[5:7], s[8:10]
            return f"{d}/{m}/{y}"
        except Exception:
            pass
    return html_lib.escape(s)


def account_page(request: Request):
    """Phase 2 account dashboard: greeting, plan, stats (from /user/*), quick actions, history preview."""
    user, _, plan_badge = _account_header(request)

    header_html = """
    <div class="account-header-row">
      <h1 class="account-header-title">Mi cuenta</h1>
      <a href="/" class="account-header-home">🏠 Volver al inicio</a>
    </div>"""

    if not user:
        body = header_html + """
    <div class="page account-page" style="max-width: 560px; margin: 24px auto;">
      <div class="card account-card" style="padding: 28px; text-align: center; background: var(--card-bg, #1a1a1a); border-radius: 12px;">
        <p class="account-greeting muted" style="font-size: 1.1rem; margin-bottom: 20px;">Iniciá sesión para ver tu cuenta</p>
        <div style="display: flex; gap: 12px; justify-content: center; flex-wrap: wrap;">
          <a href="/?auth=login" class="pill" style="padding: 10px 20px;">Entrar</a>
          <a href="/?auth=register" class="pill" style="padding: 10px 20px;">Crear cuenta</a>
        </div>
      </div>
    </div>"""
        return _simple_page("Mi cuenta — AFTR", body)

    display_name = (user.get("username") or user.get("email") or "Usuario").strip()
    display_name = html_lib.escape(display_name)
    email_display = html_lib.escape(str(user.get("email") or "").strip())
    created_display = _account_created_display(user.get("created_at"))
    uid = user.get("id")
    is_premium = (
        is_admin(user, request)
        or is_premium_active(user)
        or (get_active_plan(uid) if uid else "") in (settings.plan_premium, settings.plan_pro)
    )
    plan_label = "⭐ AFTR Premium activo" if is_premium else "Free plan"
    plan_class = "account-plan-premium" if is_premium else "account-plan-free"
    upgrade_cta = (
        ""
        if is_premium
        else '<p class="account-hero-upgrade muted"><a href="/?open=premium" class="account-hero-upgrade-link">Desbloquear Premium</a></p>'
    )

    premium_upsell_card = ""
    if not is_premium:
        premium_upsell_card = """
      <div class="card account-premium-teaser account-premium-cta">
        <div class="account-premium-teaser-glow" aria-hidden="true"></div>
        <h3 class="account-premium-teaser-title">Desbloqueá AFTR Premium</h3>
        <ul class="account-premium-teaser-list muted">
          <li>Picks elite y strong</li>
          <li>AFTR Score completo</li>
          <li>Historial y seguimiento avanzado</li>
        </ul>
        <a href="/?open=premium" class="pill account-premium-teaser-cta">Desbloquear Premium</a>
      </div>"""

    premium_wrapper_class = " account-page--premium" if is_premium else ""
    hero_class = "account-hero account-hero-premium" if is_premium else "account-hero"
    account_email_line = (
        f'<p class="account-email muted">{email_display}</p>' if email_display else ""
    )
    fav_team_name  = html_lib.escape(str(user.get("favorite_team_name") or ""))
    fav_team_crest = html_lib.escape(str(user.get("favorite_team_crest") or ""))
    fav_team_html  = ""
    if fav_team_name:
        crest_img = (
            f'<img src="{fav_team_crest}" class="hero-fav-crest" alt="" onerror="this.remove()">'
            if fav_team_crest else ""
        )
        fav_team_html = (
            f'<div class="hero-fav-team" id="hero-fav-team">'
            f'{crest_img}<span class="hero-fav-name">{fav_team_name}</span>'
            f'</div>'
        )
    else:
        fav_team_html = (
            '<div class="hero-fav-team hero-fav-team--empty" id="hero-fav-team">'
            '<span class="muted">Sin equipo favorito — <a href="#mi-equipo" class="hero-fav-choose">elegir</a></span>'
            '</div>'
        )
    body = header_html + f"""
    <div class="page account-page account-page-wrapper{premium_wrapper_class}">
      <div class="card {hero_class} account-hero-card">
        <div class="account-hero-card-shine" aria-hidden="true"></div>
        <div class="account-hero-ident">
          <p class="account-greeting">Hola, {display_name}</p>
          {account_email_line}
          {fav_team_html}
        </div>
        <div class="account-hero-badge-row">
          {(
            '<span class="account-premium-badge-gold"><span class="account-premium-badge-gold-inner">Premium</span></span>'
            if is_premium
            else '<span class="account-type-pill account-type-pill--free">Free</span>'
          )}
          <span class="account-created muted">Miembro desde {created_display}</span>
        </div>
        {upgrade_cta}
        <div class="account-hero-summary">
          <div class="account-summary-item">
            <span class="account-summary-label muted">Seguidos</span>
            <span id="hero-followed" class="account-summary-value js-stat-anim">0</span>
          </div>
          <div class="account-summary-item">
            <span class="account-summary-label muted">Favoritos</span>
            <span id="hero-favorites" class="account-summary-value js-stat-anim">0</span>
          </div>
          <div class="account-summary-item">
            <span class="account-summary-label muted">Winrate</span>
            <span id="hero-winrate" class="account-summary-value js-stat-anim">—</span>
          </div>
          <div class="account-summary-item">
            <span class="account-summary-label muted">ROI</span>
            <span id="hero-roi" class="account-summary-value js-stat-anim">0%</span>
          </div>
          <div class="account-summary-item">
            <span class="account-summary-label muted">Racha</span>
            <span id="hero-streak" class="account-summary-value">—</span>
          </div>
        </div>
      </div>
{premium_upsell_card}

      <div id="streak-banner" class="account-streak-banner" style="display:none" aria-live="polite">
        <span id="streak-banner-icon" class="account-streak-banner-icon"></span>
        <span id="streak-banner-text" class="account-streak-banner-text"></span>
      </div>

      <!-- Stats grandes -->
      <div class="ap-stats-row">
        <div class="ap-stat ap-stat--win">
          <span id="stat-wins" class="ap-stat-num js-stat-anim">0</span>
          <label class="ap-stat-label">Victorias</label>
        </div>
        <div class="ap-stat ap-stat--loss">
          <span id="stat-losses" class="ap-stat-num js-stat-anim">0</span>
          <label class="ap-stat-label">Derrotas</label>
        </div>
        <div class="ap-stat ap-stat--rate">
          <span id="stat-winrate" class="ap-stat-num js-stat-anim">—</span>
          <label class="ap-stat-label">Winrate</label>
        </div>
        <div class="ap-stat ap-stat--streak">
          <span id="stat-streak" class="ap-stat-num">—</span>
          <label class="ap-stat-label">Racha</label>
        </div>
      </div>
      <!-- Stats secundarios -->
      <div class="ap-stats-secondary">
        <span>Seguidos: <strong id="stat-followed">0</strong></span>
        <span>Favoritos: <strong id="stat-favorites">0</strong></span>
        <span>Pendientes: <strong id="stat-pending">0</strong></span>
        <span>ROI: <strong id="stat-roi">—</strong></span>
      </div>

      <div class="account-actions">
        <a href="#mi-equipo" class="pill account-action-pill">Mi equipo</a>
        <a href="#seguidas" class="pill account-action-pill">Seguidas</a>
        <a href="#favoritos" class="pill account-action-pill">Favoritos</a>
        <a href="#historial" class="pill account-action-pill">Historial</a>
        {'<a href="#bankroll" class="pill account-action-pill">Bankroll</a>' if is_premium else ''}
        <a href="/auth/logout" class="pill account-action-pill">Salir</a>
      </div>

      <section id="mi-equipo" class="account-section account-section--team">
        <h3 class="account-section-title"><span class="account-section-title-accent">Mi Equipo</span></h3>
        <div class="team-selector-wrap">
          <div class="team-current-display" id="team-current-display">
            {fav_team_html}
          </div>
          {'<button type="button" class="pill team-change-btn" id="team-change-btn" style="margin-top:8px;">Cambiar equipo</button>' if fav_team_name else ''}
          <div id="team-selector-open" style="{'display:none' if fav_team_name else ''}">
            <input type="text" id="team-search-input" class="team-search-input"
              placeholder="Buscar equipo..." autocomplete="off">
            <div id="team-grid" class="team-grid">
              <p class="muted team-grid-hint">Cargando equipos…</p>
            </div>
          </div>
          <p id="team-save-msg" class="team-save-msg" style="display:none"></p>
        </div>
      </section>

      <section id="seguidas" class="account-section account-section--picks">
        <h3 class="account-section-title"><span class="account-section-title-accent">Activas</span></h3>
        <div id="account-active-picks">
          <p class="muted">Cargando…</p>
        </div>
      </section>

      <section id="favoritos" class="account-section account-section--picks">
        <h3 class="account-section-title"><span class="account-section-title-accent">Favoritos</span></h3>
        <div id="account-favorites">
          <p class="muted">Cargando…</p>
        </div>
      </section>

      <section id="historial" class="account-section account-section--picks">
        <h3 class="account-section-title"><span class="account-section-title-accent">Historial</span></h3>
        <div id="account-history">
          <p class="muted">Cargando…</p>
        </div>
      </section>

      {'<section id="bankroll" class="account-section account-section--bankroll"><h3 class="account-section-title"><span class="account-section-title-accent">Bankroll</span><span class="account-section-badge">Premium</span></h3><div id="bankroll-display"><p class="muted">Cargando…</p></div><form id="bankroll-form" class="bankroll-form" style="display:none"><div class="bankroll-form-row"><label>Capital inicial<input type="number" id="br-initial" class="bankroll-input" min="1" step="any" placeholder="10000"></label><label>Por unidad<input type="number" id="br-stake" class="bankroll-input" min="1" step="any" placeholder="1000"></label><label>Moneda<select id="br-currency" class="bankroll-input"><option value="ARS">ARS</option><option value="USD">USD</option><option value="EUR">EUR</option></select></label></div><button type="submit" class="pill bankroll-save-btn">Guardar</button></form></section>' if is_premium else ''}
    </div>
    <script>
    (function(){{
      var base = window.location.origin || (window.location.protocol + "//" + window.location.host);
      var knownLeagues = {json.dumps(list(settings.leagues.keys()))};
      var __accountDidCountUp = false;
      function esc(s) {{ if (s == null || s === "") return ""; return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }}
      function prefersReducedMotion() {{
        return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      }}
      function easeOutCubic(t) {{ return 1 - Math.pow(1 - t, 3); }}
      function animateValueEl(el, finalStr) {{
        if (!el || prefersReducedMotion()) return;
        var s = String(finalStr).trim();
        if (s === "—" || s === "") return;
        var m = /^([+-]?\\d+(?:\\.\\d+)?)\\s*(%)?$/.exec(s.replace(",", "."));
        if (!m) return;
        var end = parseFloat(m[1]);
        if (isNaN(end)) return;
        var suffix = m[2] || "";
        var start = 0;
        var t0 = performance.now();
        var dur = 580;
        function step(now) {{
          var p = easeOutCubic(Math.min(1, (now - t0) / dur));
          var cur = start + (end - start) * p;
          if (suffix === "%") el.textContent = cur.toFixed(1) + "%";
          else el.textContent = String(Math.round(cur));
          if (p < 1) requestAnimationFrame(step);
          else el.textContent = s;
        }}
        if (suffix === "%") el.textContent = "0.0%";
        else el.textContent = "0";
        requestAnimationFrame(step);
      }}
      function runInitialStatCountUp() {{
        if (__accountDidCountUp || prefersReducedMotion()) return;
        __accountDidCountUp = true;
        ["stat-followed","stat-favorites","stat-wins","stat-losses","stat-pending","stat-roi","stat-winrate","hero-followed","hero-favorites","hero-roi","hero-winrate"].forEach(function(id) {{
          var el = document.getElementById(id);
          if (!el) return;
          animateValueEl(el, el.textContent);
        }});
      }}
      function apResultBadge(result) {{
        var r = (result || "PENDING").toUpperCase();
        var cls = {{ "WIN": "ap-badge--win", "LOSS": "ap-badge--loss", "PUSH": "ap-badge--push" }}[r] || "ap-badge--pending";
        var label = {{ "WIN": "WIN", "LOSS": "LOSS", "PUSH": "PUSH", "PENDING": "Pendiente" }}[r] || r;
        return "<span class=\\"ap-badge " + cls + "\\">" + label + "</span>";
      }}
      function apCard(opts) {{
        // opts: result, market, teams, score, aftrScore, tier, edge, when, pickId, viewHref, removeBtn, unfollowBtn
        var r = (opts.result || "PENDING").toUpperCase();
        var borderCls = {{ "WIN": "ap-card--win", "LOSS": "ap-card--loss", "PUSH": "ap-card--push" }}[r] || "ap-card--pending";
        var scoreStr = opts.score ? " <span class=\\"ap-card-score\\">" + esc(opts.score) + "</span>" : "";
        var tierStr = opts.tier && opts.tier !== "—" ? "<span class=\\"ap-card-chip ap-chip-tier\\">" + esc(opts.tier) + "</span>" : "";
        var edgeStr = opts.edge && opts.edge !== "—" ? "<span class=\\"ap-card-chip ap-chip-edge\\">" + esc(opts.edge) + "</span>" : "";
        var aftrStr = opts.aftrScore && opts.aftrScore !== "—" ? "<span class=\\"ap-card-chip\\">AFTR " + esc(String(opts.aftrScore)) + "</span>" : "";
        var h = "<div class=\\"ap-card " + borderCls + "\\">";
        h += "<div class=\\"ap-card-left\\">" + apResultBadge(r) + "</div>";
        h += "<div class=\\"ap-card-body\\">";
        h += "<div class=\\"ap-card-market\\">" + esc(opts.market || "—") + scoreStr + "</div>";
        h += "<div class=\\"ap-card-teams\\">" + esc(opts.teams || "") + "</div>";
        h += "<div class=\\"ap-card-chips\\">" + aftrStr + tierStr + edgeStr + "</div>";
        if (opts.when) h += "<div class=\\"ap-card-when muted\\">" + esc(opts.when) + "</div>";
        h += "</div>";
        h += "<div class=\\"ap-card-right\\">";
        if (opts.viewHref) h += "<a class=\\"ap-card-link\\" href=\\"" + esc(opts.viewHref) + "\\">Ver →</a>";
        if (r === "WIN") {{
          var sp = opts.shareOpts || {{}};
          h += "<button class=\\"ap-share-btn aftr-share-trigger\\""
            + " data-home=\\"" + esc(sp.home || "") + "\\""
            + " data-away=\\"" + esc(sp.away || "") + "\\""
            + " data-market=\\"" + esc(opts.market || "") + "\\""
            + " data-aftr-score=\\"" + esc(String(opts.aftrScore || "")) + "\\""
            + " data-tier=\\"" + esc(opts.tier || "") + "\\""
            + " data-score-home=\\"" + esc(String(sp.score_home != null ? sp.score_home : "")) + "\\""
            + " data-score-away=\\"" + esc(String(sp.score_away != null ? sp.score_away : "")) + "\\""
            + " title=\\"Compartir\\">&#8679;</button>";
        }}
        if (opts.unfollowBtn) h += opts.unfollowBtn;
        if (opts.removeBtn) h += opts.removeBtn;
        h += "</div>";
        h += "</div>";
        return h;
      }}
      function accountPickHeadHtml(market, teams, viewHref) {{
        var h = "<div class=\\"account-pick-head\\">";
        h += "<div class=\\"account-pick-head-main\\">";
        h += "<div class=\\"account-pick-title\\">" + market + "</div>";
        h += "<div class=\\"account-pick-subtitle\\">" + teams + "</div></div>";
        if (viewHref) h += "<a class=\\"account-pick-open\\" href=\\"" + esc(viewHref) + "\\" aria-label=\\"Abrir pick\\"><span class=\\"account-pick-open-icon\\" aria-hidden=\\"true\\"></span></a>";
        h += "</div>";
        return h;
      }}
      function fetchJSON(url) {{
        return fetch(url, {{ credentials: "include" }}).then(function(r) {{ return r.json(); }});
      }}
      function tierNice(t){{
        var v = (t == null ? "" : String(t)).trim().toLowerCase();
        if (!v) return "—";
        if (v === "pass" || v === "watch") return "WATCH";
        if (v === "elite") return "ELITE";
        if (v === "strong") return "STRONG";
        if (v === "risky") return "RISKY";
        return v.toUpperCase();
      }}
      function edgeNice(edgeVal){{
        if (edgeVal == null || edgeVal === "") return "—";
        var n = Number(edgeVal);
        if (isNaN(n)) return "—";
        var pct = n * 100;
        var sign = pct >= 0 ? "+" : "";
        return sign + pct.toFixed(1) + "%";
      }}
      function fmtDate(iso){{
        var s = (iso || "").toString();
        if (!s) return "—";
        return s.slice(0,10);
      }}
      function timeAgo(iso){{
        var s = (iso || "").toString();
        if (!s) return "—";
        var d = new Date(s);
        if (isNaN(d.getTime())) return fmtDate(s);
        var diffMs = Date.now() - d.getTime();
        if (diffMs < 0) diffMs = 0;
        var mins = Math.floor(diffMs / (60*1000));
        var hrs = Math.floor(diffMs / (60*60*1000));
        var days = Math.floor(diffMs / (24*60*60*1000));
        if (mins < 60) return "hace " + mins + "m";
        if (hrs < 24) return "hace " + hrs + "h";
        return "hace " + days + "d";
      }}
      function leagueFromPickId(pickId){{
        try {{
          var s = (pickId || "").toString();
          var parts = s.split("|");
          if (parts && parts.length > 0) return String(parts[0] || "").trim();
          return "";
        }} catch(e) {{
          return "";
        }}
      }}
      function panelHrefForPick(pickId){{
        var code = leagueFromPickId(pickId);
        if (!code) return "";
        if (Array.isArray(knownLeagues) && knownLeagues.length > 0) {{
          if (knownLeagues.indexOf(code) < 0) return "";
        }}
        return "/?league=" + encodeURIComponent(code);
      }}
      function computeCurrentStreak(historyList){{
        // historyList is expected newest-first (as returned by /user/history)
        var list = Array.isArray(historyList) ? historyList : [];
        var firstResolved = null;
        for (var i=0;i<list.length;i++) {{
          var r = (list[i] && list[i].result) ? String(list[i].result).toUpperCase() : "PENDING";
          if (r === "WIN" || r === "LOSS") {{ firstResolved = r; break; }}
          if (r === "PUSH") {{ firstResolved = "PUSH"; break; }}
        }}
        if (!firstResolved || firstResolved === "PUSH") {{
          return {{ count: 0, label: "—", kind: null }};
        }}
        var count = 0;
        for (var j=0;j<list.length;j++) {{
          var r2 = (list[j] && list[j].result) ? String(list[j].result).toUpperCase() : "PENDING";
          if (r2 === firstResolved) count++;
          else break;
        }}
        var label = count + " " + (firstResolved === "WIN" ? "WIN" : "LOSS");
        return {{ count: count, label: label, kind: firstResolved }};
      }}
      function updateStatEls(s) {{
        var el = function(id) {{ return document.getElementById(id); }};
        var num = function(v) {{ return (v != null && v !== "") ? Number(v) : 0; }};
        var roiVal = s.roi != null ? Number(s.roi) : 0;
        var totalWL = (num(s.wins) + num(s.losses));
        var winrate = totalWL > 0 ? (num(s.wins) / totalWL) * 100.0 : null;

        if (el("stat-wins")) el("stat-wins").textContent = num(s.wins);
        if (el("stat-losses")) el("stat-losses").textContent = num(s.losses);
        if (el("stat-winrate")) el("stat-winrate").textContent = winrate != null ? winrate.toFixed(1) + "%" : "—";
        if (el("stat-followed")) el("stat-followed").textContent = num(s.followed_picks);
        if (el("stat-favorites")) el("stat-favorites").textContent = num(s.favorites_count);
        if (el("stat-pending")) el("stat-pending").textContent = num(s.pending);
        if (el("stat-roi")) el("stat-roi").textContent = roiVal !== 0 ? (roiVal > 0 ? "+" : "") + roiVal.toFixed(1) + "%" : "—";

        if (el("hero-followed")) el("hero-followed").textContent = num(s.followed_picks);
        if (el("hero-favorites")) el("hero-favorites").textContent = num(s.favorites_count);
        if (el("hero-roi")) el("hero-roi").textContent = (roiVal > 0 ? "+" : "") + roiVal.toFixed(1) + "%";
        if (el("hero-winrate")) el("hero-winrate").textContent = winrate != null ? winrate.toFixed(1) + "%" : "—";

        var sc = (s.streak_count != null && s.streak_count > 0) ? Number(s.streak_count) : 0;
        var sk = (s.streak_kind && typeof s.streak_kind === "string") ? s.streak_kind.toUpperCase() : null;
        var streakIcon = sc >= 2 && sk === "WIN" ? "🔥 " : (sc >= 2 && sk === "LOSS" ? "📉 " : "");
        var streakTxt = (sc > 0 && sk) ? (streakIcon + sc + " " + sk) : "—";
        if (el("stat-streak")) el("stat-streak").textContent = streakTxt;
        if (el("hero-streak")) el("hero-streak").textContent = streakTxt;

        var banner = el("streak-banner");
        if (banner) {{
          if (sc >= 2 && sk) {{
            var isWin = sk === "WIN";
            var bannerIcon = el("streak-banner-icon");
            var bannerText = el("streak-banner-text");
            if (bannerIcon) bannerIcon.textContent = isWin ? "🔥" : "📉";
            if (bannerText) bannerText.textContent = sc + (isWin ? " victorias seguidas" : " derrotas seguidas");
            banner.className = "account-streak-banner account-streak-banner--" + (isWin ? "win" : "loss");
            banner.style.display = "";
          }} else {{
            banner.style.display = "none";
          }}
        }}
      }}
      function refreshStats() {{
        fetchJSON(base + "/user/stats").then(function(stats) {{
          if (stats && stats.ok && stats.stats) updateStatEls(stats.stats);
        }});
      }}
      document.addEventListener("click", function(e) {{
        var btn = e.target.closest && e.target.closest(".btn-remove-fav");
        if (btn) {{
          e.preventDefault();
          var pickId = btn.getAttribute("data-pick-id");
          if (!pickId) return;
          btn.disabled = true;
          fetch(base + "/user/unfavorite", {{ method: "POST", headers: {{ "Content-Type": "application/json" }}, credentials: "include", body: JSON.stringify({{ pick_id: pickId }}) }})
            .then(function(r) {{ return r.json(); }})
            .then(function(d) {{
              if (d && d.ok) {{
                var card = btn.closest(".account-pick-card");
                if (card) card.remove();
                refreshStats();
                var cont = document.getElementById("account-favorites");
                if (cont && !cont.querySelector(".account-pick-card")) {{
                  cont.innerHTML = "<div class=\\"card\\" style=\\"padding: 20px;\\"><p class=\\"muted\\" style=\\"margin: 0;\\">No tenés favoritos todavía.</p></div>";
                }}
              }} else btn.disabled = false;
            }})
            .catch(function() {{ btn.disabled = false; }});
          return;
        }}
        var unfollowBtn = e.target.closest && e.target.closest(".btn-unfollow");
        if (unfollowBtn) {{
          e.preventDefault();
          var pickId = unfollowBtn.getAttribute("data-pick-id");
          if (!pickId) return;
          unfollowBtn.disabled = true;
          fetch(base + "/user/unfollow", {{ method: "POST", headers: {{ "Content-Type": "application/json" }}, credentials: "include", body: JSON.stringify({{ pick_id: pickId }}) }})
            .then(function(r) {{ return r.json(); }})
            .then(function(d) {{
              if (d && d.ok) {{
                var card = unfollowBtn.closest(".account-pick-card");
                if (card) card.remove();
                refreshStats();
                  var cont = document.getElementById("account-history");
                  if (cont && !cont.querySelector(".account-pick-card")) {{
                    cont.innerHTML = "<div class=\\"card\\" style=\\"padding: 20px;\\"><p class=\\"muted\\" style=\\"margin: 0;\\">No hay historial reciente.</p></div>";
                  }}
                  var activeCont = document.getElementById("account-active-picks");
                  if (activeCont && !activeCont.querySelector(".account-pick-card")) {{
                    activeCont.innerHTML = "<div class=\\"card\\" style=\\"padding: 20px;\\"><p class=\\"muted\\" style=\\"margin: 0;\\">No tenés picks activos.</p></div>";
                  }}
              }} else unfollowBtn.disabled = false;
            }})
            .catch(function() {{ unfollowBtn.disabled = false; }});
        }}
      }});
      Promise.all([
        fetchJSON(base + "/user/stats").catch(function() {{ return {{ok:false}}; }}),
        fetchJSON(base + "/user/history").catch(function() {{ return {{ok:false, history:[]}}; }}),
        fetchJSON(base + "/user/favorites").catch(function() {{ return {{ok:false, favorites:[]}}; }})
      ]).then(function(results) {{
        var stats = results[0];
        var history = results[1];
        var favorites = results[2];
        var histList = (history && history.ok && Array.isArray(history.history)) ? history.history : [];
        var favList = (favorites && favorites.ok && Array.isArray(favorites.favorites)) ? favorites.favorites : [];
        if (stats && stats.ok && stats.stats) updateStatEls(stats.stats);
        runInitialStatCountUp();

        // Best market (most frequent market across history + favorites)
        var marketCounts = {{}};
        [histList, favList].forEach(function(arr){{
          (arr || []).forEach(function(it){{
            var m = (it && it.market) ? String(it.market).trim() : "";
            if (!m || m === "-") return;
            marketCounts[m] = (marketCounts[m] || 0) + 1;
          }});
        }});
        var bestMarket = "—";
        var bestMarketN = 0;
        Object.keys(marketCounts).forEach(function(k){{
          if (marketCounts[k] > bestMarketN) {{ bestMarketN = marketCounts[k]; bestMarket = k; }}
        }});
        if (document.getElementById("insight-best-market")) document.getElementById("insight-best-market").textContent = esc(bestMarket);

        // Most followed league (infer from pick_id prefix)
        var leagueCounts = {{}};
        var allItems = (histList || []).concat(favList || []);
        allItems.forEach(function(it){{
          var pid = it ? it.pick_id : null;
          var code = leagueFromPickId(pid);
          if (!code) return;
          if (Array.isArray(knownLeagues) && knownLeagues.length > 0 && knownLeagues.indexOf(code) < 0) return;
          leagueCounts[code] = (leagueCounts[code] || 0) + 1;
        }});
        var topLeague = "—";
        var topLeagueN = 0;
        Object.keys(leagueCounts).forEach(function(k){{
          if (leagueCounts[k] > topLeagueN) {{ topLeagueN = leagueCounts[k]; topLeague = k; }}
        }});
        if (document.getElementById("insight-top-league")) document.getElementById("insight-top-league").textContent = esc(topLeague);

        // Recent activity (top 3) — card layout: status + date, market, match line
        var recentHtml = "";
        (histList || []).slice(0, 3).forEach(function(item){{
          if (!item) return;
          var r = (item.result || "PENDING").toString().toUpperCase();
          var resultClass = r.toLowerCase();
          var market = esc(item.market || "—");
          var dt = esc((item.created_at || "").slice(0,10));
          var home = esc(item.home || "");
          var away = esc(item.away || "");
          var matchLine = (home && away) ? (home + " vs " + away) : "";
          var finCls = (r === "WIN" || r === "LOSS" || r === "PUSH") ? " account-status-finished" : "";
          recentHtml += "<div class=\\"account-activity-feed-card\\">" +
            "<div class=\\"account-activity-feed-top\\">" +
            "<span class=\\"account-status-badge account-status-" + esc(resultClass) + finCls + "\\">" + esc(r) + "</span>" +
            "<span class=\\"account-activity-feed-date\\">" + (dt || "—") + "</span>" +
            "</div>" +
            "<div class=\\"account-activity-feed-market\\">" + market + "</div>" +
            (matchLine ? "<div class=\\"account-activity-feed-match muted\\">" + matchLine + "</div>" : "") +
            "</div>";
        }});
        var activityEl = document.getElementById("insight-recent-activity");
        if (activityEl) activityEl.innerHTML = recentHtml || "<span class=\\"muted\\">—</span>";

        // ── Favoritos ──────────────────────────────────────────────
        var favCont = document.getElementById("account-favorites");
        if (favCont) {{
          var favList2 = (favorites && favorites.ok && Array.isArray(favorites.favorites)) ? favorites.favorites : [];
          if (!favList2.length) {{
            favCont.innerHTML = "<p class=\\"muted\\">No tenés favoritos todavía.</p>";
          }} else {{
            var fHtml = "";
            favList2.forEach(function(item) {{
              var pickId = esc(item.pick_id || "");
              var viewHref = panelHrefForPick(item.pick_id || "");
              var home = item.home_team || item.home || "";
              var away = item.away_team || item.away || "";
              var teams = (home && away) ? (home + " vs " + away) : "";
              var rmBtn = "<button type=\\"button\\" class=\\"ap-remove-btn btn-remove-fav\\" data-pick-id=\\"" + pickId + "\\" title=\\"Quitar\\">✕</button>";
              fHtml += apCard({{
                result: "FAV",
                market: item.market || "—",
                teams: teams,
                aftrScore: item.aftr_score != null ? Math.round(item.aftr_score) : "—",
                tier: tierNice(item.tier),
                edge: edgeNice(item.edge),
                when: "Guardado " + timeAgo(item.created_at),
                pickId: pickId,
                viewHref: viewHref,
                removeBtn: rmBtn
              }});
            }});
            favCont.innerHTML = fHtml;
          }}
        }}

        // ── Picks activas (PENDING) ────────────────────────────────
        var activeCont = document.getElementById("account-active-picks");
        if (activeCont) {{
          var active = (histList || []).filter(function(it){{
            return (it && (it.result || "PENDING").toUpperCase()) === "PENDING";
          }});
          if (!active.length) {{
            activeCont.innerHTML = "<p class=\\"muted\\">No tenés picks activos por ahora.</p>";
          }} else {{
            var aHtml = "";
            active.slice(0, 8).forEach(function(item){{
              var pickId = esc(item.pick_id || "");
              var viewHref = panelHrefForPick(item.pick_id || "");
              var home = item.home_team || item.home || "";
              var away = item.away_team || item.away || "";
              var teams = (home && away) ? (home + " vs " + away) : "";
              var ufBtn = "<button type=\\"button\\" class=\\"ap-remove-btn btn-unfollow\\" data-pick-id=\\"" + pickId + "\\" title=\\"Dejar de seguir\\">✕</button>";
              aHtml += apCard({{
                result: "PENDING",
                market: item.market || "—",
                teams: teams,
                aftrScore: item.aftr_score != null ? Math.round(item.aftr_score) : "—",
                tier: tierNice(item.tier),
                edge: edgeNice(item.edge),
                when: "Seguido " + timeAgo(item.created_at),
                pickId: pickId,
                viewHref: viewHref,
                unfollowBtn: ufBtn
              }});
            }});
            activeCont.innerHTML = aHtml;
          }}
        }}

        // ── Historial (resueltos) ──────────────────────────────────
        var histCont = document.getElementById("account-history");
        if (histCont) {{
          var resolved = (histList || []).filter(function(it){{
            var r = (it && it.result) ? String(it.result).toUpperCase() : "PENDING";
            return r !== "PENDING";
          }});
          if (!resolved.length) {{
            histCont.innerHTML = "<p class=\\"muted\\">Sin historial todavía.</p>";
          }} else {{
            var hHtml = "";
            resolved.forEach(function(item) {{
              var pickId = esc(item.pick_id || "");
              var viewHref = panelHrefForPick(item.pick_id || "");
              var home = item.home_team || item.home || "";
              var away = item.away_team || item.away || "";
              var finalScore = item.final_score ? " · " + item.final_score : "";
              var teams = (home && away) ? (home + " vs " + away + finalScore) : "";
              var ufBtn = "<button type=\\"button\\" class=\\"ap-remove-btn btn-unfollow\\" data-pick-id=\\"" + pickId + "\\" title=\\"Eliminar\\">✕</button>";
              hHtml += apCard({{
                result: item.result || "PENDING",
                market: item.market || "—",
                teams: teams,
                aftrScore: item.aftr_score != null ? Math.round(item.aftr_score) : "—",
                tier: tierNice(item.tier),
                edge: edgeNice(item.edge),
                when: timeAgo(item.created_at),
                pickId: pickId,
                viewHref: viewHref,
                unfollowBtn: ufBtn,
                shareOpts: {{ home: home, away: away, score_home: item.score_home, score_away: item.score_away }}
              }});
            }});
            histCont.innerHTML = hHtml;
          }}
        }}
      }}).catch(function() {{
        // Individual catches above should prevent this from firing
        var emptyMsg = "<div class=\\"card\\" style=\\"padding:16px;\\"><p class=\\"muted\\" style=\\"margin:0;\\">Sin datos por ahora.</p></div>";
        var els = ["account-history","account-favorites","account-active-picks"];
        els.forEach(function(id) {{ var el=document.getElementById(id); if(el && el.innerHTML.indexOf("Cargando")>-1) el.innerHTML=emptyMsg; }});
      }});
    }})();
    </script>
    <script>
    // ── Team Selector ────────────────────────────────────────────
    (function() {{
      var base = window.location.origin;
      var allTeams = [];
      var searchInput = document.getElementById('team-search-input');
      var grid        = document.getElementById('team-grid');
      var saveMsg     = document.getElementById('team-save-msg');

      function chipHtml(t) {{
        var crest = t.team_crest
          ? '<img src="' + t.team_crest + '" class="team-chip-crest" alt="" onerror="this.remove()">'
          : '';
        var safeName  = t.team_name.replace(/&/g,'&amp;').replace(/"/g,'&quot;');
        var safeCrest = (t.team_crest||'').replace(/"/g,'&quot;');
        var safeId    = (t.team_id||'').replace(/"/g,'&quot;');
        return '<button type="button" class="team-chip"'
          + ' data-name="' + safeName + '"'
          + ' data-crest="' + safeCrest + '"'
          + ' data-id="' + safeId + '">'
          + crest + '<span class="team-chip-name">' + t.team_name + '</span></button>';
      }}

      function renderGrid(teams) {{
        if (!grid) return;
        if (!teams.length) {{
          grid.innerHTML = '<p class="muted team-grid-hint">Sin resultados.</p>';
          return;
        }}
        grid.innerHTML = teams.slice(0, 80).map(chipHtml).join('');
      }}

      function filterAndRender() {{
        var q = searchInput ? searchInput.value.trim().toLowerCase() : '';
        if (!q) {{ renderGrid(allTeams.slice(0, 80)); return; }}
        renderGrid(allTeams.filter(function(t) {{
          return t.team_name.toLowerCase().indexOf(q) !== -1;
        }}));
      }}

      function saveTeam(name, crest, id) {{
        if (saveMsg) {{ saveMsg.textContent = 'Guardando…'; saveMsg.style.display='block'; saveMsg.className='team-save-msg'; }}
        fetch(base + '/user/favorite-team', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          credentials: 'include',
          body: JSON.stringify({{ team_name: name, team_crest: crest, team_id: id }})
        }})
        .then(function(r) {{ return r.json(); }})
        .then(function(d) {{
          if (d.ok) {{
            var img = crest ? '<img src="' + crest + '" class="hero-fav-crest" alt="" onerror="this.remove()">' : '';
            var inner = img + '<span class="hero-fav-name">' + name + '</span>';
            var heroEl = document.getElementById('hero-fav-team');
            if (heroEl) {{ heroEl.className='hero-fav-team'; heroEl.innerHTML=inner; }}
            var curEl = document.getElementById('team-current-display');
            if (curEl) curEl.innerHTML = '<div class="hero-fav-team">' + inner + '</div>';
            if (saveMsg) {{ saveMsg.textContent='✓ Guardado: ' + name; saveMsg.className='team-save-msg team-save-msg--ok'; }}
            collapseSelector();
            if (changeBtn) changeBtn.style.display = '';
          }} else {{
            if (saveMsg) {{ saveMsg.textContent='Error al guardar.'; saveMsg.className='team-save-msg team-save-msg--err'; }}
          }}
          setTimeout(function() {{ if(saveMsg) saveMsg.style.display='none'; }}, 3000);
        }})
        .catch(function() {{
          if (saveMsg) {{ saveMsg.textContent='Error de red.'; saveMsg.className='team-save-msg team-save-msg--err'; saveMsg.style.display='block'; }}
        }});
      }}

      var selectorOpen = document.getElementById('team-selector-open');
      var changeBtn    = document.getElementById('team-change-btn');

      function collapseSelector() {{
        if (selectorOpen) selectorOpen.style.display = 'none';
        if (changeBtn) changeBtn.style.display = '';
      }}
      function expandSelector() {{
        if (selectorOpen) selectorOpen.style.display = '';
        if (changeBtn) changeBtn.style.display = 'none';
        if (searchInput) {{ searchInput.value = ''; searchInput.focus(); }}
        filterAndRender();
      }}

      if (changeBtn) changeBtn.addEventListener('click', expandSelector);

      // Click delegation on the grid
      if (grid) grid.addEventListener('click', function(e) {{
        var btn = e.target.closest('.team-chip');
        if (!btn) return;
        grid.querySelectorAll('.team-chip').forEach(function(b) {{ b.classList.remove('active'); }});
        btn.classList.add('active');
        saveTeam(btn.dataset.name || '', btn.dataset.crest || '', btn.dataset.id || '');
      }});

      if (searchInput) searchInput.addEventListener('input', filterAndRender);

      // Eager load — fetch teams on page load, no typing required
      fetch(base + '/user/available-teams')
        .then(function(r) {{ return r.json(); }})
        .then(function(d) {{
          allTeams = d.teams || [];
          if (!allTeams.length) {{
            if (grid) grid.innerHTML = '<p class="muted team-grid-hint">No hay equipos cargados aún. Volvé después del próximo refresh.</p>';
            return;
          }}
          filterAndRender();
        }})
        .catch(function() {{
          if (grid) grid.innerHTML = '<p class="muted team-grid-hint">No se pudieron cargar los equipos.</p>';
        }});
    }})();
    </script>
    {'<script>/* Bankroll */(function(){var base=window.location.origin;fetch(base+"/user/bankroll").then(function(r){return r.json();}).then(function(d){var el=document.getElementById("bankroll-display");var form=document.getElementById("bankroll-form");if(!el)return;if(!d.ok){el.innerHTML=\'<p class="muted">\'+(d.error==="premium_required"?"Requiere suscripción premium activa.":"Error al cargar bankroll ("+( d.error||"desconocido")+")")+\'</p>\';return;}var cur=d.current_bankroll;var ini=d.initial_amount;var pnl=d.total_pnl;var cur2=d.currency||"ARS";var pnlStr=(pnl>=0?"+":"")+pnl.toLocaleString("es-AR",{minimumFractionDigits:2,maximumFractionDigits:2});var curStr=cur.toLocaleString("es-AR",{minimumFractionDigits:2,maximumFractionDigits:2});var pnlCls=pnl>=0?"bk-pnl--pos":"bk-pnl--neg";el.innerHTML=\'<div class="bankroll-display-inner"><div class="bk-current"><span class="bk-label">Capital actual</span><span class="bk-amount">\'+cur2+" "+curStr+\'</span></div><div class="bk-pnl \'+pnlCls+\'"><span class="bk-label">P&L total</span><span class="bk-pnl-val">\'+cur2+" "+pnlStr+\' ('"+d.total_picks_settled+"' picks)</span></div><button class="pill bk-edit-btn" type="button" id="bk-edit-btn">Editar configuración</button></div>\';if(form){var bi=document.getElementById("br-initial");var bs=document.getElementById("br-stake");var bc=document.getElementById("br-currency");if(bi)bi.value=ini;if(bs)bs.value=d.stake_per_unit||1000;if(bc)bc.value=cur2;document.getElementById("bk-edit-btn")&&document.getElementById("bk-edit-btn").addEventListener("click",function(){form.style.display=form.style.display==="none"?"block":"none";});}}).catch(function(){var el=document.getElementById("bankroll-display");if(el)el.innerHTML=\'<p class="muted">Sin datos.</p>\';});var form=document.getElementById("bankroll-form");if(form){form.addEventListener("submit",function(e){e.preventDefault();var ini=parseFloat(document.getElementById("br-initial").value);var stk=parseFloat(document.getElementById("br-stake").value);var cur=document.getElementById("br-currency").value;if(!ini||!stk)return;fetch(base+"/user/bankroll",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({initial_amount:ini,stake_per_unit:stk,currency:cur})}).then(function(r){return r.json();}).then(function(d){if(d.ok)location.reload();}).catch(function(){});});}})();</script>' if is_premium else ''}
    <script src="/static/aftr-share.js?v=1" defer></script>"""
    return _simple_page("Mi cuenta — AFTR", body)


