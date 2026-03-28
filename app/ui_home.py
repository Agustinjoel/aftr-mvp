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
    _premium_unlock_card, _render_pick_card,
)

logger = logging.getLogger("aftr.ui")

def _build_home_league_snap_carousel_html(
    request: Request,
    unsupported: set[str],
    *,
    carousel_id: str = "homeLeagueCarousel",
    active_league: str | None = None,
    include_script: bool = True,
) -> str:
    """
    3D-style league carousel: viewport + transform track + .league-item anchors (home_league_carousel.js).
    Used on home and league dashboard; pass carousel_id + active_league on dashboard.
    """
    if active_league is not None:
        ac = (active_league or "").strip()
        active = ac if settings.is_valid_league(ac) else _home_league_active_code(request)
    else:
        active = _home_league_active_code(request)
    items: list[str] = []
    ix = 0
    for code, name in settings.leagues.items():
        if code in unsupported:
            continue
        act = " is-active active" if code == active else ""
        logo_slug = {"EL": "uel"}.get(code, code.lower())
        logo = f"/static/leagues/{logo_slug}.png"
        initial = (name or code or "?")[:1].upper()
        items.append(
            f'<a class="league-card league-item{act}" href="/?league={html_lib.escape(code)}" data-code="{html_lib.escape(code)}" data-index="{ix}">'
            f'<span class="league-item__card">'
            f'<span class="league-item__glow" aria-hidden="true"></span>'
            f'<img class="league-item__logo" src="{html_lib.escape(logo)}" alt="" width="56" height="56" loading="lazy" '
            "onerror=\"this.style.display='none';this.nextElementSibling.style.display='flex'\" />"
            f'<span class="league-item__fallback" aria-hidden="true">{html_lib.escape(initial)}</span>'
            f'<span class="league-item__name">{html_lib.escape(name)}</span>'
            f"</span></a>"
        )
        ix += 1
    cid = html_lib.escape(carousel_id)
    core = (
        f'<div class="league-carousel league-carousel--3d" id="{cid}" '
        f'data-active-code="{html_lib.escape(active)}">'
        f'<div class="league-carousel__viewport3d" data-carousel-viewport>'
        f'<div class="league-track" data-track>{"".join(items)}</div></div></div>'
    )
    script = (
        '<script src="/static/home_league_carousel.js?v=7" defer></script>'
        if include_script
        else ""
    )
    return core + script


# _locked_card, _locked_grid, _premium_unlock_card → importados de app.ui_card




# top_picks_with_variety, _risk_label_from_conf, _result_norm → importados de app.ui_picks_calc
# _parse_utcdate_maybe → importado de app.ui_helpers


# _combo_leg_kickoff_html → importado de app.ui_combos

# MATCH_LIVE_STATUSES, _match_live_status_token, isMatchFinished, isMatchLive,
# _live_minute_suffix, _format_live_status_line → importados de app.ui_matches

# _label_for_date, _WEEKDAY_LABELS, group_upcoming_picks_by_day,
# group_picks_recent_by_day_desc → importados de app.ui_picks_calc

# _combo_leg_kickoff_html → importado de app.ui_combos


# _stat_line, _wdl_badge, _pct_class, _market_key, _to_pct01,
# _bar_single, _chips_from_form, _render_back_stats → importados de app.ui_stats


# =========================================================
# Score extractor (compat)
# =========================================================
# _extract_score_from_match, _extract_score, _pick_id_for_card → importados de app.ui_data


# _pick_odds_display_value, _pick_odds_home_line_text, _render_pick_card → importados de app.ui_card



def home_page(request: Request) -> str:
    """Global AFTR home: summary across all leagues, top picks, combo, big matches, featured leagues, premium CTA."""
    cookies = getattr(request, "cookies", None) or {}
    has_aftr_session = "aftr_session" in cookies
    uid = get_user_id(request)
    logger.info(
        "home_page render: request.cookies has aftr_session=%s, get_user_id(request)=%s",
        has_aftr_session,
        uid,
    )
    user = get_user_by_id(uid) if uid else None
    if uid and not user:
        # broken cookie: uid not in DB; treat as logged out (middleware clears cookie)
        uid, user = None, None
    auth_param = (request.query_params.get("auth") or "").strip().lower()
    signup_modal_style = "display:flex" if auth_param == "register" else "display:none"
    login_modal_style = "display:flex" if auth_param == "login" else "display:none"
    auth_html = ""
    if user:
        display_name = html_lib.escape((user.get("username") or user.get("email") or ""))
        auth_html = (
            f'<span class="plan-badge">{display_name}</span>'
            f'<a class="plan-logout" href="/account">Mi cuenta</a>'
            f'<a class="plan-logout" href="/auth/logout">Salir</a>'
        )
    else:
        # On home page, navigate to auth routes so the modal can open via ?auth=... param.
        auth_html = (
            '<a class="pill" href="/?auth=login">Entrar</a>'
            '<a class="pill" href="/?auth=register">Crear cuenta</a>'
        )
    is_admin_user = is_admin(user, request)
    plan_badge = auth_html
    if is_admin_user:
        plan_badge = '<span class="plan-badge admin">ADMIN</span>' + auth_html
    elif get_active_plan(uid) == settings.plan_pro:
        plan_badge = '<span class="plan-badge pro">PRO</span>' + auth_html
    elif is_premium_active(user) or get_active_plan(uid) == settings.plan_premium:
        plan_badge = '<span class="plan-badge premium">PREMIUM</span>' + auth_html

    user_premium = bool(uid and (is_premium_active(user) or get_active_plan(uid) == settings.plan_premium))

    (
        _all_picks,
        match_by_key,
        all_settled,
        all_upcoming,
        picks_by_league,
        matches_by_league,
    ) = _load_all_leagues_data()

    # Debug: homepage data counts and reason if empty
    n_picks = len(_all_picks)
    n_leagues = len(picks_by_league)
    n_upcoming = len(all_upcoming)
    if n_picks == 0:
        logger.warning(
            "home_page: rendered with 0 picks. leagues_with_picks=%s; check cache path and _is_pick_valid fallback.",
            list(picks_by_league.keys()),
        )
    else:
        logger.info(
            "home_page: picks_loaded=%s leagues_rendered=%s upcoming=%s",
            n_picks, n_leagues, n_upcoming,
        )

    # Cache status (última actualización / actualizando datos)
    cache_meta = read_cache_meta()
    cache_status_html = _format_cache_status(cache_meta)

    # Global summary (ROI uses resolved settled picks only — not upcoming)
    wins = sum(1 for p in all_settled if _result_norm(p) == "WIN")
    losses = sum(1 for p in all_settled if _result_norm(p) == "LOSS")
    pending = len(all_upcoming)
    total_picks = len(all_settled) + pending
    net = round(sum(_unit_delta(p) for p in all_settled), 2)
    winrate = round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else None
    winrate_str = f"{winrate}%" if winrate is not None else "—"

    # ROI = (total_profit / total_stake) * 100; basis = resolved outcomes, else any finished bucket
    roi_resolved = [p for p in all_settled if _result_norm(p) in ("WIN", "LOSS", "PUSH")]
    roi_basis = roi_resolved if roi_resolved else list(all_settled)
    total_profit = sum(_unit_delta(p) for p in roi_basis)
    total_stake = sum(_pick_stake_units(p) for p in roi_basis)
    roi_pct: float | None
    if not roi_basis or total_stake <= 0:
        roi_pct = None
        roi_str = "—"
    else:
        roi_pct = round((total_profit / total_stake) * 100.0, 1)
        roi_str = f"{roi_pct:+.1f}%"
    if all_settled and not roi_resolved:
        logger.warning(
            "home_page ROI: all_settled=%s but no WIN/LOSS/PUSH on pick.result; using full all_settled for ROI basis",
            len(all_settled),
        )
    logger.info(
        "home_page ROI: resolved=%s basis_picks=%s (all_settled=%s) total_profit=%s total_stake=%s roi_pct=%s display=%s",
        len(roi_resolved),
        len(roi_basis),
        len(all_settled),
        round(total_profit, 4),
        round(total_stake, 4),
        roi_pct,
        roi_str,
    )

    # Performance chart (global)
    settled_sorted = sorted(all_settled, key=lambda p: _parse_utcdate_str(p.get("utcDate")), reverse=True)
    settled_groups = group_picks_recent_by_day_desc(settled_sorted, days=7)
    spark_points = _roi_spark_points(settled_groups)
    last_spark = spark_points[-1] if spark_points else {}
    perf_accum = float(last_spark.get("v", 0) or 0)
    perf_day = float(last_spark.get("day", 0) or 0)
    if roi_pct is None:
        home_perf_trend = "neutral"
    elif roi_pct > 0:
        home_perf_trend = "up"
    elif roi_pct < 0:
        home_perf_trend = "down"
    else:
        home_perf_trend = "flat"
    _hp = []
    if roi_pct is not None:
        if roi_pct > 0:
            _hp.append("perf-stat-tile--pos")
        elif roi_pct < 0:
            _hp.append("perf-stat-tile--neg")
        else:
            _hp.append("perf-stat-tile--flat")
    else:
        _hp.append("perf-stat-tile--neutral")
    home_primary_tile_class = " ".join(_hp)
    home_arrow_up_style = "display:inline" if home_perf_trend == "up" else "display:none"
    home_arrow_down_style = "display:inline" if home_perf_trend == "down" else "display:none"
    home_accum_pos = perf_accum > 0
    home_accum_neg = perf_accum < 0
    home_day_pos = perf_day > 0
    home_day_neg = perf_day < 0

    # Mejores Picks del Día: best by _pick_score (limited to 4 in card build below)

    # 3 premium combo cards with explicit roles: Día / 72h / Value
    premium_combos = _build_home_premium_combos(
        all_upcoming,
        match_by_key,
        log_context="home",
    )
    combos_section_html = "\n".join(_render_home_premium_combo_card(c) for c in premium_combos)

    # Active leagues = all configured leagues that have at least one pick (nav, featured, big matches)
    leagues_with_picks = {
        code for code in settings.leagues
        if (picks_by_league.get(code) or [])
    }

    # Big matches today (only leagues with picks)
    today_iso = datetime.now().astimezone().date().isoformat()
    big_matches: list[dict] = []
    for code in leagues_with_picks:
        if code not in settings.leagues:
            continue
        league_matches = matches_by_league.get(code) or []
        day_blocks = group_matches_by_day(league_matches, days=1)
        for block in day_blocks:
            if block.get("date") != today_iso and block.get("label") != "Hoy":
                continue
            for m in (block.get("matches") or [])[:2]:
                if not isinstance(m, dict):
                    continue
                mid = _safe_int(m.get("match_id") or m.get("id"))
                league_picks = picks_by_league.get(code) or []
                best = None
                for p in league_picks:
                    p_mid = _safe_int(p.get("match_id") or p.get("id"))
                    p_match = match_by_key.get((code, p_mid)) if p_mid is not None else None
                    if isMatchFinished(p) or (isMatchFinished(p_match) if isinstance(p_match, dict) else False):
                        continue
                    if _safe_int(p.get("match_id")) == mid or _safe_int(p.get("id")) == mid:
                        if best is None or _pick_score(p) > _pick_score(best):
                            best = p
                big_matches.append({
                    "league": code,
                    "league_name": settings.leagues.get(code, code),
                    "match": m,
                    "best_pick": best,
                })
            break
        if len(big_matches) >= 10:
            break
    big_matches = big_matches[:4]

    _unsupported_home = get_unsupported_leagues()
    _unsupported_football_home = {
        c for c in _unsupported_home if getattr(settings, "league_sport", {}).get(c) != "basketball"
    }
    home_league_carousel_html = _build_home_league_snap_carousel_html(request, _unsupported_football_home)

    # Live picks (match in play): dedicated section + exclude from "Mejores Picks del Día"
    live_pick_keys: set[str] = set()
    live_picks: list[dict] = []
    for p in all_upcoming:
        if not isinstance(p, dict):
            continue
        mid = _safe_int(p.get("match_id") or p.get("id"))
        league = p.get("_league")
        if mid is None or not league:
            continue
        m = match_by_key.get((league, mid))
        if not isinstance(m, dict):
            continue
        if isMatchFinished(p) or isMatchFinished(m):
            continue
        if not isMatchLive(m):
            continue
        pk = _pick_id_for_card(p, {"market": p.get("best_market")})
        if pk in live_pick_keys:
            continue
        live_pick_keys.add(pk)
        live_picks.append(p)
    live_picks.sort(key=lambda p: (-(p.get("aftr_score") or 0), -_pick_score(p)))

    # Mejores Picks del Día: only picks scheduled for today or within near-term window (exclude far-future)
    today_local = datetime.now().astimezone().date()
    top_picks_max_days_ahead = 2  # today + up to 2 days ahead
    end_local = today_local + timedelta(days=top_picks_max_days_ahead)
    picks_near_term = []
    for p in all_upcoming:
        if not isinstance(p, dict):
            continue
        pk = _pick_id_for_card(p, {"market": p.get("best_market")})
        if pk in live_pick_keys:
            continue
        local_d = _pick_local_date(p, match_by_key)
        if local_d is None or not (today_local <= local_d <= end_local):
            continue
        picks_near_term.append(p)
    top_picks = sorted(
        picks_near_term,
        key=lambda p: (-(p.get("aftr_score") or 0), -_pick_score(p)),
    )[:4]
    top_picks_source = "near_term"
    if not top_picks:
        # Fallback #2: nearest upcoming picks even outside strict near-term window.
        nearest_candidates = []
        now_utc = datetime.now(timezone.utc)
        for p in all_upcoming:
            if not isinstance(p, dict):
                continue
            pk = _pick_id_for_card(p, {"market": p.get("best_market")})
            if pk in live_pick_keys:
                continue
            dt = _parse_utcdate_str(p.get("utcDate"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt < now_utc:
                continue
            nearest_candidates.append(p)
        top_picks = sorted(
            nearest_candidates,
            key=lambda p: (_parse_utcdate_str(p.get("utcDate")), -_pick_score(p)),
        )[:4]
        if top_picks:
            top_picks_source = "nearest_upcoming"

    if not top_picks:
        # Fallback #3: last successful visible homepage picks snapshot.
        snap_raw = read_json(HOME_VISIBLE_SNAPSHOT_FILE)
        if isinstance(snap_raw, list):
            snap_picks = [p for p in snap_raw if isinstance(p, dict)]
            if snap_picks:
                top_picks = snap_picks[:4]
                top_picks_source = "snapshot"

    # Persist only successful live results; never overwrite snapshot with empty data.
    if top_picks and top_picks_source != "snapshot":
        try:
            write_json(HOME_VISIBLE_SNAPSHOT_FILE, top_picks[:8])
        except Exception as e:
            logger.warning("home_page: snapshot write failed: %s", e)

    active_picks_now = len(live_picks) + len(picks_near_term)
    top_picks_source_note = ""
    if top_picks_source == "nearest_upcoming":
        top_picks_source_note = "Mostrando los próximos picks disponibles fuera de la ventana corta."
    elif top_picks_source == "snapshot":
        top_picks_source_note = "Mostrando la última selección visible guardada mientras llegan nuevos picks activos."
    top_picks_empty_html = (
        '<p class="home-empty muted">No hay picks activos para hoy/próximas horas.</p>'
        if total_picks <= 0
        else '<p class="home-empty muted">No hay picks activos ahora. Las métricas del encabezado son históricas (incluyen picks ya resueltos).</p>'
    )
    for p in top_picks:
        if p.get("home") and p.get("away"):
            continue
        mid = _safe_int(p.get("match_id")) or _safe_int(p.get("id"))
        league = p.get("_league")
        m = match_by_key.get((league, mid)) if mid is not None and league else None
        if isinstance(m, dict):
            if not p.get("home"):
                p["home"] = m.get("home") or "—"
            if not p.get("away"):
                p["away"] = m.get("away") or "—"
            if not p.get("home_crest") and m.get("home_crest"):
                p["home_crest"] = m.get("home_crest")
            if not p.get("away_crest") and m.get("away_crest"):
                p["away_crest"] = m.get("away_crest")
    top_pick_cards = []
    for p in top_picks:
        league_code = p.get("_league") or "—"
        league_name = html_lib.escape(settings.leagues.get(league_code, league_code))
        home = html_lib.escape(str(p.get("home") or "—"))
        away = html_lib.escape(str(p.get("away") or "—"))
        market = html_lib.escape(str(p.get("best_market") or "—"))
        score = p.get("aftr_score")
        if score is None:
            score = _aftr_score(p)
        else:
            try:
                score = int(round(float(score)))
            except (TypeError, ValueError):
                score = _aftr_score(p)
        edge = p.get("edge")
        try:
            edge_str = f"{float(edge)*100:+.1f}%" if edge is not None else "—"
        except (TypeError, ValueError):
            edge_str = "—"
        conf_level = (p.get("confidence_level") or p.get("confidence") or "—")
        conf_str = str(conf_level).upper() if conf_level != "—" else "—"
        odds_line_text = html_lib.escape(_pick_odds_home_line_text(p))
        try:
            edge_pos = edge is not None and float(edge) > 0
        except (TypeError, ValueError):
            edge_pos = False
        edge_class = " home-pick-edge-pos" if edge_pos else ""
        _t = p.get("tier")
        tier = (str(_t).strip().lower() if _t is not None else "pass") or "pass"
        tier_colors = {"elite": "#FFD700", "strong": "#00C853", "risky": "#FF9800", "pass": "#9E9E9E"}
        tier_color = tier_colors.get(tier, "#9E9E9E")
        home_part = _team_with_crest(p.get("home_crest"), p.get("home") or "—")
        away_part = _team_with_crest(p.get("away_crest"), p.get("away") or "—")
        pick_id_val = _pick_id_for_card(p, {"market": p.get("best_market")})
        pick_id_attr = html_lib.escape(pick_id_val)
        market_raw = str(p.get("best_market") or "")
        market_attr = html_lib.escape(market_raw)
        edge_raw = p.get("edge")
        edge_attr = html_lib.escape(str(edge_raw)) if edge_raw is not None else ""
        top_pick_cards.append(f"""
        <div class="card home-pick-card" style="border-left: 4px solid {tier_color};">
          <div class="home-pick-league">{league_name}</div>
          <div class="home-pick-match">
            {home_part}
            <span class="vs">vs</span>
            {away_part}
          </div>
          <div class="home-pick-market">{market}</div>
          <div class="home-pick-meta">
            <span class="home-pick-score">AFTR {score}</span>
            <span class="aftr-tier" style="color: {tier_color};">{html_lib.escape(tier.upper())}</span>
            <span class="home-pick-edge{edge_class}">Ventaja {edge_str}</span>
            <span>Conf {html_lib.escape(conf_str)}</span>
            <span>{odds_line_text}</span>
          </div>
          <div class="pick-actions" style="display:flex; gap:8px; margin-top:10px; flex-wrap:wrap;">
            <button type="button" class="btn-favorite-pick pill"
              data-pick-id="{pick_id_attr}" data-market="{market_attr}" data-aftr-score="{score}"
              data-tier="{html_lib.escape(tier)}" data-edge="{edge_attr}"
              data-home-team="{home}" data-away-team="{away}"
              style="padding:6px 12px; font-size:0.85rem;">⭐ Guardar</button>
            <button type="button" class="btn-follow-pick pill"
              data-pick-id="{pick_id_attr}" data-market="{market_attr}" data-aftr-score="{score}"
              data-tier="{html_lib.escape(tier)}" data-edge="{edge_attr}"
              data-home-team="{home}" data-away-team="{away}"
              style="padding:6px 12px; font-size:0.85rem;">📈 Seguir pick</button>
          </div>
        </div>""")

    for p in live_picks:
        if p.get("home") and p.get("away"):
            continue
        mid = _safe_int(p.get("match_id")) or _safe_int(p.get("id"))
        league = p.get("_league")
        m = match_by_key.get((league, mid)) if mid is not None and league else None
        if isinstance(m, dict):
            if not p.get("home"):
                p["home"] = m.get("home") or "—"
            if not p.get("away"):
                p["away"] = m.get("away") or "—"
            if not p.get("home_crest") and m.get("home_crest"):
                p["home_crest"] = m.get("home_crest")
            if not p.get("away_crest") and m.get("away_crest"):
                p["away_crest"] = m.get("away_crest")

    live_pick_cards: list[str] = []
    for p in live_picks:
        league_code = p.get("_league") or "—"
        league_name = html_lib.escape(settings.leagues.get(league_code, league_code))
        home = html_lib.escape(str(p.get("home") or "—"))
        away = html_lib.escape(str(p.get("away") or "—"))
        market = html_lib.escape(str(p.get("best_market") or "—"))
        score = p.get("aftr_score")
        if score is None:
            score = _aftr_score(p)
        else:
            try:
                score = int(round(float(score)))
            except (TypeError, ValueError):
                score = _aftr_score(p)
        edge = p.get("edge")
        try:
            edge_str = f"{float(edge)*100:+.1f}%" if edge is not None else "—"
        except (TypeError, ValueError):
            edge_str = "—"
        conf_level = (p.get("confidence_level") or p.get("confidence") or "—")
        conf_str = str(conf_level).upper() if conf_level != "—" else "—"
        odds_line_text = html_lib.escape(_pick_odds_home_line_text(p))
        try:
            edge_pos = edge is not None and float(edge) > 0
        except (TypeError, ValueError):
            edge_pos = False
        edge_class = " home-pick-edge-pos" if edge_pos else ""
        _t = p.get("tier")
        tier = (str(_t).strip().lower() if _t is not None else "pass") or "pass"
        tier_colors = {"elite": "#FFD700", "strong": "#00C853", "risky": "#FF9800", "pass": "#9E9E9E"}
        tier_color = tier_colors.get(tier, "#9E9E9E")
        home_part = _team_with_crest(p.get("home_crest"), p.get("home") or "—")
        away_part = _team_with_crest(p.get("away_crest"), p.get("away") or "—")
        pick_id_val = _pick_id_for_card(p, {"market": p.get("best_market")})
        pick_id_attr = html_lib.escape(pick_id_val)
        market_raw = str(p.get("best_market") or "")
        market_attr = html_lib.escape(market_raw)
        edge_raw = p.get("edge")
        edge_attr = html_lib.escape(str(edge_raw)) if edge_raw is not None else ""
        mid = _safe_int(p.get("match_id")) or _safe_int(p.get("id"))
        league = p.get("_league")
        m_live = match_by_key.get((league, mid)) if mid is not None and league else None
        status_line = _format_live_status_line(m_live) if isinstance(m_live, dict) else "🔴 LIVE"
        lh, la = _extract_score_from_match(m_live) if isinstance(m_live, dict) else (None, None)
        if lh is not None and la is not None:
            match_block = f"""
          <div class="home-pick-match home-pick-match-live">
            <div class="home-pick-live-team">{home_part}</div>
            <div class="home-pick-live-score" aria-label="Marcador en vivo">{lh} - {la}</div>
            <div class="home-pick-live-team">{away_part}</div>
          </div>"""
        else:
            match_block = f"""
          <div class="home-pick-match">
            {home_part}
            <span class="vs">vs</span>
            {away_part}
          </div>"""
        live_pick_cards.append(f"""
        <div class="card home-pick-card home-pick-card--live" style="border-left: 4px solid {tier_color};">
          <div class="home-pick-live-status" role="status">{html_lib.escape(status_line)}</div>
          <div class="home-pick-league">{league_name}</div>
          {match_block}
          <div class="home-pick-market">{market}</div>
          <div class="home-pick-meta">
            <span class="home-pick-score">AFTR {score}</span>
            <span class="aftr-tier" style="color: {tier_color};">{html_lib.escape(tier.upper())}</span>
            <span class="home-pick-edge{edge_class}">Ventaja {edge_str}</span>
            <span>Conf {html_lib.escape(conf_str)}</span>
            <span>{odds_line_text}</span>
          </div>
          <div class="pick-actions" style="display:flex; gap:8px; margin-top:10px; flex-wrap:wrap;">
            <button type="button" class="btn-favorite-pick pill"
              data-pick-id="{pick_id_attr}" data-market="{market_attr}" data-aftr-score="{score}"
              data-tier="{html_lib.escape(tier)}" data-edge="{edge_attr}"
              data-home-team="{home}" data-away-team="{away}"
              style="padding:6px 12px; font-size:0.85rem;">⭐ Guardar</button>
            <button type="button" class="btn-follow-pick pill"
              data-pick-id="{pick_id_attr}" data-market="{market_attr}" data-aftr-score="{score}"
              data-tier="{html_lib.escape(tier)}" data-edge="{edge_attr}"
              data-home-team="{home}" data-away-team="{away}"
              style="padding:6px 12px; font-size:0.85rem;">📈 Seguir pick</button>
          </div>
        </div>""")

    live_section_html = ""
    if live_pick_cards:
        live_section_html = f"""
      <section class="home-section home-live-section" id="live-now">
      <h2 class="home-h2 home-live-title">🔴 En vivo ahora</h2>
      <div class="home-picks-grid home-live-grid">
        {''.join(live_pick_cards)}
      </div>
      </section>
"""

    # Big matches HTML: [home crest] Home vs Away [away crest] (same helper as league pages)
    big_match_cards = []
    for b in big_matches:
        m = b["match"]
        league_name = html_lib.escape(b["league_name"])
        home_part = _team_with_crest(m.get("home_crest"), m.get("home") or "—")
        away_part = _team_with_crest(m.get("away_crest"), m.get("away") or "—")
        best = b.get("best_pick")
        pick_line = ""
        if best:
            mk = html_lib.escape(str(best.get("best_market") or "—"))
            sc = _aftr_score(best)
            pick_line = f'<div class="home-bigmatch-pick"><span class="home-bigmatch-pick-market">{mk}</span><span class="home-bigmatch-pick-score">AFTR {sc}</span></div>'
        big_match_cards.append(f"""
        <a href="/?league={html_lib.escape(b['league'])}" class="card home-bigmatch-card">
          <div class="home-bigmatch-league">{league_name}</div>
          <div class="home-bigmatch-match">
            {home_part}
            <span class="vs">vs</span>
            {away_part}
          </div>
          {pick_line}
        </a>""")

    # Chart area: canvas + tooltip + embedded data when we have data; otherwise empty-state message.
    # Root cause of blank chart: chart script ran before/inconsistent order vs script that set
    # window.AFTR_ROI_POINTS. Fix: embed data in <script type="application/json" id="aftr-roi-chart-data">
    # so the chart reads from the DOM (getElementById + JSON.parse) when it runs.
    if spark_points:
        # Break "</script" sequences so HTML parsers do not close this tag early.
        chart_data_json = json.dumps(spark_points).replace("</script", "<\\/script")
        home_perf_chart_inner = (
            '<canvas id="roiSpark" aria-hidden="true"></canvas>\n            '
            '<div id="roiTip" class="roi-tip" style="display:none;"></div>\n            '
            '<script type="application/json" id="aftr-roi-chart-data">' + chart_data_json + '</script>'
        )
    else:
        home_perf_chart_inner = (
            '<div class="perf-chart-empty-state" role="status">'
            '<p class="perf-chart-empty-title">Sin datos suficientes todavía</p>'
            '<p class="perf-chart-empty-sub muted">No hay picks resueltos en la ventana reciente para graficar.</p>'
            "</div>"
        )

    page_html = f"""
    <html>
    <head>
      <meta charset="utf-8"/>
      <title>AFTR — AI Picks</title>
      <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
      <link rel="stylesheet" href="/static/style.css?v=22">
      <link rel="icon" type="image/png" href="/static/logo_aftr.png">
      <link rel="manifest" href="/static/manifest.webmanifest">
      <meta name="theme-color" content="#0b0f14">
    </head>
    <body>
    """ + AUTH_BOOTSTRAP_SCRIPT + f"""
      <div id="premium-modal" class="modal-backdrop" style="display:none;">
        <div class="modal">
          <div class="modal-head">
            <div class="modal-title">⭐ AFTR Premium</div>
            <button class="modal-x" onclick="closePremium()">✕</button>
          </div>
          <div class="modal-body">
            <p class="modal-subtitle">Desbloqueá el motor de apuestas con IA</p>
            <ul class="modal-list">
              <li>Todos los picks del día</li>
              <li>Picks con alto AFTR Score</li>
              <li>Apuestas de valor con ventaja positiva</li>
              <li>Picks de todas las ligas</li>
            </ul>
            <p style="margin:14px 0;"><span class="price-main">$9.99</span><span class="price-sub">/ mes</span></p>
            {"<div class=\"premium-badge\">⭐ Premium activo</div>" if user_premium else '<button class="pill modal-cta" onclick="activatePremium(\'PREMIUM\')">Activar Premium</button>'}
          </div>
        </div>
      </div>
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
      <div class="page">
      <header class="top top-pro home-header">
        <div class="brand">
          <img src="/static/logo_aftr.png" class="logo-aftr" alt="AFTR" />
          <div class="brand-text">
            <div class="brand-title">AFTR</div>
            <div class="brand-tag">Motor de apuestas con IA</div>
          </div>
        </div>
        <a href="/" class="home-header-inicio" aria-current="page">Inicio</a>
        <div class="home-header-auth">
          {plan_badge}
          {'<a href="/admin/users" class="muted">Admin</a>' if is_admin_user else ''}
        </div>
      </header>
      {cache_status_html}
      <div class="home-carousel-strip" role="navigation" aria-label="Elegir liga">
        {home_league_carousel_html}
      </div>

      <section class="home-hero hero">
        <div class="hero-copy">
          <h1>Picks con IA, apuestas de valor y combinadas inteligentes</h1>
          <p>Las mejores oportunidades del día, filtradas por AFTR Score, ventaja y confianza.</p>
          <div class="hero-stats home-hero-kpis">
            <div class="home-hero-kpi"><span>ROI HISTÓRICO</span><strong>{roi_str}</strong></div>
            <div class="home-hero-kpi"><span>GANANCIA NETA HIST.</span><strong>{net:+.1f}u</strong></div>
            <div class="home-hero-kpi"><span>ACIERTO HISTÓRICO</span><strong>{winrate_str}</strong></div>
            <div class="home-hero-kpi"><span>PICKS ACTIVOS AHORA</span><strong>{active_picks_now}</strong></div>
          </div>
          <div class="hero-buttons">
            <a href="#top-picks" class="btn-secondary">Ver picks de hoy</a>
            {"<div class=\"premium-badge\">⭐ Premium activo</div>" if user_premium else '<button type="button" class="btn-primary" onclick="openPremium();">Obtener Premium</button>'}
          </div>
        </div>
        <div class="hero-art"></div>
      </section>
      {live_section_html}

      <section class="home-section" id="top-picks">
      <h2 class="home-h2">Mejores Picks del Día</h2>
      {f'<p class="home-empty muted">{html_lib.escape(top_picks_source_note)}</p>' if top_picks_source_note else ''}
      <div class="home-picks-grid">
        {''.join(top_pick_cards) if top_pick_cards else top_picks_empty_html}
      </div>
      </section>

      <section class="home-section">
      <h2 class="home-h2">Combos de Hoy</h2>
      <div class="home-combos-grid">
        {combos_section_html}
      </div>
      </section>

      <section class="home-section">
      <h2 class="home-h2">Partidos Destacados</h2>
      <div class="home-bigmatch-grid">
        {''.join(big_match_cards) if big_match_cards else '<p class="home-empty muted">No hay partidos destacados hoy.</p>'}
      </div>
      </section>

      <section class="home-section home-perf-section perf-panel-section">
      <div class="perf-panel-head perf-panel-head--home">
        <h2 class="home-h2 perf-panel-title">Rendimiento AFTR (histórico)</h2>
        <p class="home-perf-intro muted perf-panel-sub">Evolución del ROI y unidades netas (últimos 7 días).</p>
      </div>
      <div class="home-perf-inner">
        <div class="perf-panel-glass home-perf-chart-wrap card home-spark-wrap">
          <div class="perf-strip-stats perf-strip-stats--home" role="group" aria-label="Resumen de rendimiento">
            <div class="perf-stat-tile perf-stat-tile--primary {home_primary_tile_class}">
              <span class="perf-stat-arrow perf-stat-arrow--up" aria-hidden="true" style="{home_arrow_up_style}">↑</span>
              <span class="perf-stat-arrow perf-stat-arrow--down" aria-hidden="true" style="{home_arrow_down_style}">↓</span>
              <span class="perf-stat-value">{roi_str}</span>
              <span class="perf-stat-label">ROI total</span>
            </div>
            <div class="perf-stat-tile{' perf-stat-tile--pos' if home_accum_pos else ''}{' perf-stat-tile--neg' if home_accum_neg else ''}">
              <span class="perf-stat-value">{perf_accum:+.2f}u</span>
              <span class="perf-stat-label">Profit acumulado</span>
            </div>
            <div class="perf-stat-tile{' perf-stat-tile--pos' if home_day_pos else ''}{' perf-stat-tile--neg' if home_day_neg else ''}">
              <span class="perf-stat-value">{perf_day:+.2f}u</span>
              <span class="perf-stat-label">Último día</span>
            </div>
          </div>
          <div class="roi-spark-head perf-chart-head-inner">
            <div>
              <div class="roi-spark-title">Curva acumulada</div>
              <div class="roi-spark-sub muted">Pasá el mouse para ver el detalle por día</div>
            </div>
          </div>
          <div class="roi-spark-canvas perf-chart-canvas-wrap home-spark-canvas-inner">
            {home_perf_chart_inner}
          </div>
        </div>
      </div>
      </section>

      <section class="home-section home-bottom-hub-section">
        <div class="home-bottom-hub">
          <div class="home-bottom-hub-grid">
            <div class="card home-hub-card home-hub-card--primary">
              {(
                """<div class="home-hub-eyebrow">Estado</div>
              <h3 class="home-hub-title">⭐ Premium activo</h3>
              <ul class="home-hub-perks">
                <li>Todos los picks y ligas desbloqueados</li>
                <li>Combos de valor y AFTR Score completo</li>
                <li>Historial avanzado en tu cuenta</li>
              </ul>
              <a href="/account" class="pill home-hub-cta-secondary">Ver mi cuenta</a>"""
                if user_premium
                else f"""<div class="home-hub-eyebrow">Planes</div>
              <h3 class="home-hub-title">Desbloqueá AFTR Premium</h3>
              <p class="home-hub-desc muted">Picks ilimitados, combos inteligentes y todas las ligas.</p>
              <ul class="home-hub-perks home-hub-perks--compact">
                <li><strong>Gratis</strong> — picks limitadas · ligas seleccionadas</li>
                <li><strong>Premium</strong> — todo el motor · $9.99/mes</li>
              </ul>
              <button type="button" class="pill home-hub-cta" onclick="openPremium();">Obtener Premium</button>"""
              )}
            </div>
            <div class="card home-hub-card home-hub-card--account">
              {(
                f"""<div class="home-hub-eyebrow">Tu cuenta AFTR</div>
              <h3 class="home-hub-title">Hola, {html_lib.escape((user.get("username") or (user.get("email") or "Usuario").split("@")[0] or "Usuario").strip()[:28])}</h3>
              <p class="home-hub-desc muted">Seguí picks activos, favoritos, historial e insights personales.</p>
              <a href="/account" class="pill home-hub-cta">Ir al dashboard</a>
              <p class="home-hub-foot muted">ROI, winrate y actividad reciente en un solo lugar.</p>"""
                if user
                else """<div class="home-hub-eyebrow">Tu cuenta AFTR</div>
              <h3 class="home-hub-title">Seguí tu rendimiento</h3>
              <p class="home-hub-desc muted">Favoritos, picks seguidas y estadísticas con una cuenta gratis.</p>
              <a href="/?auth=register" class="pill home-hub-cta">Crear cuenta</a>
              <a href="/?auth=login" class="home-hub-link muted">Ya tengo cuenta →</a>"""
              )}
            </div>
          </div>
        </div>
      </section>

      </div>
    """
    # JavaScript for home page: must be in plain string (no f-string) to avoid { } interpreted as format placeholders
    page_html += """
      <script>
        function openPremium(){ var m = document.getElementById("premium-modal"); if(m) m.style.display = "flex"; document.body.style.overflow = "hidden"; }
        function closePremium(){ var m = document.getElementById("premium-modal"); if(m) m.style.display = "none"; document.body.style.overflow = ""; }
        function activatePremium(plan){
          var url = (window.location.origin || (window.location.protocol + "//" + window.location.host)) + "/billing/create-checkout-session";
          fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, credentials: "include", body: "{}" })
            .then(function(r){ return r.json().then(function(d){ return { ok: r.ok, data: d }; }); })
            .then(function(result){
              if (result.ok && result.data && result.data.url) { window.location.href = result.data.url; }
              else if (result.data && result.data.error === "need_login") { closePremium(); window.location.href = "/?auth=login"; }
              else { alert("No se pudo iniciar el checkout. Intenta de nuevo."); }
            })
            .catch(function(){ alert("Error de conexión."); });
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
        (function pickActions(){
          var base = window.location.origin || (window.location.protocol + "//" + window.location.host);
          window.__userLoggedIn = window.__userLoggedIn !== undefined ? window.__userLoggedIn : null;
          function checkLogin(){
            if (window.__userLoggedIn !== null) return Promise.resolve(window.__userLoggedIn);
            return fetch(base + "/user/me", { credentials: "include" }).then(function(r){ return r.json(); }).then(function(d){
              window.__userLoggedIn = !!(d && d.ok && d.user);
              return window.__userLoggedIn;
            }).catch(function(){ window.__userLoggedIn = false; return false; });
          }
          function toast(msg){
            var el = document.createElement("div");
            el.style.cssText = "position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--card-bg,#1a1a1a);color:#fff;padding:10px 18px;border-radius:8px;font-size:0.9rem;z-index:9999;box-shadow:0 4px 12px rgba(0,0,0,0.3);";
            el.textContent = msg;
            document.body.appendChild(el);
            setTimeout(function(){ if (el.parentNode) el.parentNode.removeChild(el); }, 2500);
          }
          function doFavorite(btn){
            var pickId = btn.getAttribute("data-pick-id");
            if (!pickId) return;
            checkLogin().then(function(loggedIn){
              if (!loggedIn){ alert("Iniciá sesión para usar esta función"); return; }
              if (btn.disabled) return;
              btn.disabled = true;
              var payload = { pick_id: pickId };
              var market = btn.getAttribute("data-market"); if (market) payload.market = market;
              var aftr = btn.getAttribute("data-aftr-score"); if (aftr !== null && aftr !== "") payload.aftr_score = parseInt(aftr, 10);
              var tier = btn.getAttribute("data-tier"); if (tier) payload.tier = tier;
              var edge = btn.getAttribute("data-edge"); if (edge !== null && edge !== "") payload.edge = parseFloat(edge);
              var home = btn.getAttribute("data-home-team"); if (home) payload.home_team = home;
              var away = btn.getAttribute("data-away-team"); if (away) payload.away_team = away;
              fetch(base + "/user/favorite", { method: "POST", headers: { "Content-Type": "application/json" }, credentials: "include", body: JSON.stringify(payload) })
                .then(function(r){ return r.json(); })
                .then(function(d){ if (d && d.ok){ btn.textContent = "Guardado ✅"; toast("Pick guardada"); } else { btn.disabled = false; toast(d && d.error || "Error"); } })
                .catch(function(){ btn.disabled = false; toast("Error de conexión"); });
            });
          }
          function doFollow(btn){
            var pickId = btn.getAttribute("data-pick-id");
            if (!pickId) return;
            checkLogin().then(function(loggedIn){
              if (!loggedIn){ alert("Iniciá sesión para usar esta función"); return; }
              if (btn.disabled) return;
              btn.disabled = true;
              var payload = { pick_id: pickId };
              var market = btn.getAttribute("data-market"); if (market) payload.market = market;
              var aftr = btn.getAttribute("data-aftr-score"); if (aftr !== null && aftr !== "") payload.aftr_score = parseInt(aftr, 10);
              var tier = btn.getAttribute("data-tier"); if (tier) payload.tier = tier;
              var edge = btn.getAttribute("data-edge"); if (edge !== null && edge !== "") payload.edge = parseFloat(edge);
              var home = btn.getAttribute("data-home-team"); if (home) payload.home_team = home;
              var away = btn.getAttribute("data-away-team"); if (away) payload.away_team = away;
              fetch(base + "/user/follow-pick", { method: "POST", headers: { "Content-Type": "application/json" }, credentials: "include", body: JSON.stringify(payload) })
                .then(function(r){ return r.json(); })
                .then(function(d){ if (d && d.ok){ btn.textContent = "Siguiendo 📈"; toast("Pick seguida"); } else { btn.disabled = false; toast(d && d.error || "Error"); } })
                .catch(function(){ btn.disabled = false; toast("Error de conexión"); });
            });
          }
          function applyPersistedState(){
            checkLogin().then(function(loggedIn){
              if (!loggedIn) return;
              Promise.all([
                fetch(base + "/user/favorites", { credentials: "include" }).then(function(r){ return r.json(); }),
                fetch(base + "/user/followed-ids", { credentials: "include" }).then(function(r){ return r.json(); })
              ]).then(function(results){
                var favoriteIds = {};
                var followedIds = {};
                if (results[0] && results[0].ok && Array.isArray(results[0].favorites)) results[0].favorites.forEach(function(x){ favoriteIds[x.pick_id] = true; });
                if (results[1] && results[1].ok && Array.isArray(results[1].pick_ids)) results[1].pick_ids.forEach(function(id){ followedIds[id] = true; });
                document.querySelectorAll(".btn-favorite-pick").forEach(function(btn){
                  var id = btn.getAttribute("data-pick-id");
                  if (id && favoriteIds[id]){ btn.textContent = "Guardado ✅"; }
                });
                document.querySelectorAll(".btn-follow-pick").forEach(function(btn){
                  var id = btn.getAttribute("data-pick-id");
                  if (id && followedIds[id]){ btn.textContent = "Siguiendo 📈"; }
                });
              }).catch(function(){});
            });
          }
          document.addEventListener("click", function(e){
            var fav = e.target.closest && e.target.closest(".btn-favorite-pick");
            if (fav){ e.preventDefault(); e.stopPropagation(); doFavorite(fav); return; }
            var fol = e.target.closest && e.target.closest(".btn-follow-pick");
            if (fol){ e.preventDefault(); e.stopPropagation(); doFollow(fol); return; }
          });
          if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", applyPersistedState);
          else applyPersistedState();
        })();
      </script>
      <script>
        (function(){
          function drawSpark(canvasId, points){
            var c = document.getElementById(canvasId);
            var tip = document.getElementById("roiTip");
            if(!c || !points || !points.length) return;
            var ctx = c.getContext('2d');
            var parent = c.parentElement;
            if(parent && parent.clientWidth === 0) parent.style.minWidth = "320px";
            var w = Math.max(320, parent ? parent.clientWidth : c.width);
            var h = 180;
            c.width = w; c.height = h;
            var vals = points.map(function(p){ return Number(p.v || 0); });
            var min = Math.min.apply(null, vals);
            var max = Math.max.apply(null, vals);
            if (min === max) { min -= 1; max += 1; }
            var padX = 18, padY = 22;
            var innerW = w - padX*2;
            var innerH = h - padY*2;
            function xAt(i){
              if(points.length === 1) return padX + innerW/2;
              return padX + (innerW * (i/(points.length-1)));
            }
            function yAt(v){
              var t = (v - min) / (max - min);
              return padY + innerH - (t * innerH);
            }
            var pathPts = points.map(function(p, i){ return { x: xAt(i), y: yAt(Number(p.v || 0)) }; });
            function redraw(hoverIndex){
              ctx.clearRect(0,0,w,h);
              ctx.globalAlpha = 0.28;
              ctx.strokeStyle = "rgba(255,255,255,0.18)";
              ctx.lineWidth = 1;
              for (var i=0;i<3;i++){ var y = padY + (innerH * (i/2)); ctx.beginPath(); ctx.moveTo(padX, y); ctx.lineTo(padX+innerW, y); ctx.stroke(); }
              ctx.globalAlpha = 1;
              var y0 = yAt(0);
              ctx.globalAlpha = 0.55;
              ctx.strokeStyle = "rgba(255,255,255,0.25)";
              ctx.setLineDash([6,6]);
              ctx.beginPath(); ctx.moveTo(padX, y0); ctx.lineTo(padX+innerW, y0); ctx.stroke();
              ctx.setLineDash([]); ctx.globalAlpha = 1;
              ctx.beginPath();
              pathPts.forEach(function(pt, i){ if(i===0) ctx.moveTo(pt.x, pt.y); else ctx.lineTo(pt.x, pt.y); });
              ctx.lineTo(pathPts[pathPts.length-1].x, padY+innerH); ctx.lineTo(pathPts[0].x, padY+innerH); ctx.closePath();
              var grad = ctx.createLinearGradient(0, padY, 0, padY+innerH);
              grad.addColorStop(0, "rgba(120,170,255,0.38)"); grad.addColorStop(0.5, "rgba(120,170,255,0.12)"); grad.addColorStop(1, "rgba(120,170,255,0.04)");
              ctx.fillStyle = grad; ctx.fill();
              ctx.lineWidth = 4; ctx.strokeStyle = "rgba(140,200,255,0.98)";
              ctx.beginPath();
              pathPts.forEach(function(pt, i){ if(i===0) ctx.moveTo(pt.x, pt.y); else ctx.lineTo(pt.x, pt.y); });
              ctx.stroke();
              pathPts.forEach(function(pt, i){
                var day = Number(points[i].day || 0);
                var col = day > 0 ? "rgba(34,197,94,0.95)" : (day < 0 ? "rgba(239,68,68,0.95)" : "rgba(255,255,255,0.85)");
                ctx.fillStyle = col; ctx.beginPath(); ctx.arc(pt.x, pt.y, 4.5, 0, Math.PI*2); ctx.fill();
              });
              var last = points[points.length-1];
              ctx.fillStyle = "rgba(255,255,255,0.92)";
              ctx.font = "13px system-ui, -apple-system, Segoe UI, Roboto";
              ctx.fillText("Acum: " + (Number(last.v||0)>=0?"+":"") + Number(last.v||0).toFixed(2) + "u  |  Último día: " + (Number(last.day||0)>=0?"+":"") + Number(last.day||0).toFixed(2) + "u", padX, 16);
              if(hoverIndex != null && hoverIndex >= 0){
                var pt = pathPts[hoverIndex];
                ctx.globalAlpha = 0.55; ctx.strokeStyle = "rgba(255,255,255,0.20)"; ctx.lineWidth = 1;
                ctx.beginPath(); ctx.moveTo(pt.x, padY); ctx.lineTo(pt.x, padY+innerH); ctx.stroke(); ctx.globalAlpha = 1;
                ctx.fillStyle = "rgba(120,170,255,1)"; ctx.beginPath(); ctx.arc(pt.x, pt.y, 6, 0, Math.PI*2); ctx.fill();
                ctx.fillStyle = "rgba(255,255,255,0.95)"; ctx.beginPath(); ctx.arc(pt.x, pt.y, 3, 0, Math.PI*2); ctx.fill();
              }
            }
            function nearestIndex(mx){
              var best = 0, bestDist = Infinity;
              for(var i=0;i<pathPts.length;i++){ var d = Math.abs(pathPts[i].x - mx); if(d < bestDist){ bestDist = d; best = i; } }
              return best;
            }
            function showTip(i, clientX, clientY){
              if(!tip) return;
              var p = points[i];
              tip.innerHTML = "<div><b>" + (p.label || "Día") + "</b></div><div class=\\"muted\\">Neto: " + ((Number(p.day||0)>=0?"+":"") + Number(p.day||0).toFixed(2)) + "u</div><div>Acum: " + ((Number(p.v||0)>=0?"+":"") + Number(p.v||0).toFixed(2)) + "u</div>";
              tip.style.display = "block";
              var rect = c.getBoundingClientRect();
              var x = clientX - rect.left; var y = clientY - rect.top;
              var tx = Math.max(8, Math.min(rect.width - 220, x + 12));
              var ty = Math.max(8, Math.min(rect.height - 70, y - 10));
              tip.style.left = tx + "px"; tip.style.top = ty + "px";
            }
            function hideTip(){ if(tip) tip.style.display = "none"; redraw(-1); }
            redraw(-1);
            c.onmousemove = function(e){
              var rect = c.getBoundingClientRect();
              var mx = e.clientX - rect.left;
              if(mx < padX || mx > (padX+innerW)){ hideTip(); return; }
              var i = nearestIndex(mx); redraw(i); showTip(i, e.clientX, e.clientY);
            };
            c.onmouseleave = hideTip;
          }
          function boot(){
            var pts = [];
            var dataEl = document.getElementById("aftr-roi-chart-data");
            if(dataEl && dataEl.textContent){
              try { pts = JSON.parse(dataEl.textContent); } catch(e) { pts = []; }
            }
            if(!pts.length && typeof window.AFTR_ROI_POINTS !== "undefined" && window.AFTR_ROI_POINTS) pts = window.AFTR_ROI_POINTS;
            function runDraw(){ drawSpark("roiSpark", pts); }
            if(window.requestAnimationFrame) requestAnimationFrame(runDraw);
            else runDraw();
            window.addEventListener("resize", function(){
              var p = [];
              var el = document.getElementById("aftr-roi-chart-data");
              if(el && el.textContent){ try { p = JSON.parse(el.textContent); } catch(e) {} }
              drawSpark("roiSpark", p);
            });
          }
          if(document.readyState === "loading"){ document.addEventListener("DOMContentLoaded", boot); }
          else { boot(); }
        })();
      </script>
    </body>
    </html>
    """
    return page_html

