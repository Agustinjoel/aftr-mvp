"""
Rendering de pick cards, cards premium bloqueadas y helpers de odds para display.
"""
from __future__ import annotations

import html as html_lib
import logging

from config.settings import settings
from app.timefmt import format_match_kickoff_ar
from app.ui_helpers import _safe_float, _safe_int
from app.ui_picks_calc import _aftr_score, _risk_label_from_conf, _result_norm
from app.ui_matches import isMatchFinished, isMatchLive, _format_live_status_line
from app.ui_data import _extract_score, _extract_score_from_match, _pick_id_for_card
from app.ui_team import _team_with_crest
from app.ui_stats import _render_back_stats

logger = logging.getLogger("aftr.ui.card")

# Flag de debug: solo loguea la primera card finalizada por sesión
_finished_card_debug_logged = False

# ── Tooltips de mercado ────────────────────────────────────────────────────────
_MARKET_TIPS: dict[str, str] = {
    "1":        "Gana el local",
    "X":        "Empate",
    "2":        "Gana el visitante",
    "1X":       "Local gana o empate",
    "X2":       "Empate o visitante gana",
    "12":       "Cualquiera gana, sin empate",
    "Over 0.5": "Al menos 1 gol en el partido",
    "Over 1.5": "Más de 1 gol en el partido",
    "Over 2.5": "Más de 2 goles en el partido",
    "Over 3.5": "Más de 3 goles en el partido",
    "Under 1.5": "Menos de 2 goles en el partido",
    "Under 2.5": "Menos de 3 goles en el partido",
    "Under 3.5": "Menos de 4 goles en el partido",
    "BTTS":     "Ambos equipos anotan",
    "BTTS No":  "Al menos uno no anota",
    "DNB 1":    "Local gana (devuelve si empata)",
    "DNB 2":    "Visitante gana (devuelve si empata)",
}

def _market_html(market: str) -> str:
    """Market con tooltip si el mercado es conocido."""
    m = str(market or "—")
    tip = _MARKET_TIPS.get(m)
    esc = html_lib.escape(m)
    if tip:
        return f'<span class="mkt-tip" tabindex="0" data-tip="{html_lib.escape(tip)}">{esc}</span>'
    return esc

TEAM_LOGO_FALLBACK_PATH = "/static/teams/default.svg"


# =========================================================
# Live match card
# =========================================================

def _render_live_match_card(match: dict, pick: dict | None = None, actions_html: str = "") -> str:
    """
    Card de partido en vivo: score grande + estado + pick opcional.
    Usada tanto en la home como en el dashboard de liga.
    """
    if not isinstance(match, dict):
        return ""

    home      = html_lib.escape(match.get("home") or "—")
    away      = html_lib.escape(match.get("away") or "—")
    h_crest   = html_lib.escape((match.get("home_crest") or "").strip() or TEAM_LOGO_FALLBACK_PATH)
    a_crest   = html_lib.escape((match.get("away_crest") or "").strip() or TEAM_LOGO_FALLBACK_PATH)
    fb        = html_lib.escape(TEAM_LOGO_FALLBACK_PATH)

    lh, la   = _extract_score_from_match(match)
    score_l  = str(lh) if lh is not None else "—"
    score_r  = str(la) if la is not None else "—"

    status_line = _format_live_status_line(match)
    status_esc  = html_lib.escape(status_line)

    pick_row = ""
    if pick and isinstance(pick, dict):
        market  = html_lib.escape(str(pick.get("best_market") or pick.get("market") or "—"))
        _raw_sc = pick.get("aftr_score")
        try:
            sc = int(round(float(_raw_sc))) if _raw_sc is not None else _aftr_score(pick)
        except (TypeError, ValueError):
            sc = _aftr_score(pick)
        edge    = _safe_float(pick.get("edge"), None)
        if edge is not None:
            edge_val = f"{edge * 100:+.1f}%"
            edge_cls = " live-card-edge--pos" if edge > 0 else " live-card-edge--neg"
            edge_html = f'<span class="live-card-edge{edge_cls}">{html_lib.escape(edge_val)}</span>'
        else:
            edge_html = ""
        pick_row = (
            f'<div class="live-card-pick">'
            f'<span class="live-card-market">{market}</span>'
            f'<span class="live-card-aftr">AFTR {sc}</span>'
            f'{edge_html}'
            f'</div>'
        )

    actions_block = f'<div class="live-card-actions">{actions_html}</div>' if actions_html else ""

    return (
        f'<div class="card live-card">'
        f'<div class="live-card-header">'
        f'<span class="live-dot-badge"><span class="live-dot"></span>EN VIVO</span>'
        f'<span class="live-card-status">{status_esc}</span>'
        f'</div>'
        f'<div class="live-card-scoreboard">'
        f'<div class="live-card-team">'
        f'<img src="{h_crest}" class="live-card-crest" loading="lazy" width="28" height="28" '
        f'onerror="this.src=\'{fb}\';this.onerror=null;"/>'
        f'<span class="live-card-team-name">{home}</span>'
        f'</div>'
        f'<div class="live-card-score">{score_l} <span class="live-score-sep">—</span> {score_r}</div>'
        f'<div class="live-card-team live-card-team--away">'
        f'<img src="{a_crest}" class="live-card-crest" loading="lazy" width="28" height="28" '
        f'onerror="this.src=\'{fb}\';this.onerror=null;"/>'
        f'<span class="live-card-team-name">{away}</span>'
        f'</div>'
        f'</div>'
        f'{pick_row}'
        f'{actions_block}'
        f'</div>'
    )


# =========================================================
# Odds helpers
# =========================================================

def _pick_odds_display_value(p: dict) -> str:
    """
    Fuente única de odds para la UI (home cards + flip cards).
    Orden: odds_decimal → best_fair → implied_prob → 1/best_prob.
    Devuelve token corto como "2.10", "Impl 48.2%" o "—".
    """
    if not isinstance(p, dict):
        return "—"
    od = p.get("odds_decimal")
    if od is not None:
        try:
            return f"{float(od):.2f}"
        except (TypeError, ValueError):
            pass
    bf = p.get("best_fair")
    if bf is not None:
        try:
            return f"{float(bf):.2f}"
        except (TypeError, ValueError):
            pass
    ip = p.get("implied_prob")
    if ip is not None:
        try:
            return f"Impl {float(ip) * 100:.1f}%"
        except (TypeError, ValueError):
            pass
    bp = p.get("best_prob")
    if bp is not None:
        try:
            pp = float(bp)
            if pp > 0:
                return f"{1.0 / pp:.2f}"
        except (TypeError, ValueError):
            pass
    return "—"


def _pick_odds_home_line_text(p: dict) -> str:
    """Texto completo de odds para la fila meta de home (ej. 'Odds 2.10' o 'Impl 48%')."""
    v = _pick_odds_display_value(p)
    if v == "—":
        return "Odds —"
    if v.startswith("Impl"):
        return v
    return f"Odds {v}"


# =========================================================
# Premium lock cards (teaser para usuarios free)
# =========================================================

def _locked_card(message: str = "Disponible en Premium") -> str:
    return (
        f'<div class="card locked-card" onclick="openPremium()" role="button" tabindex="0">'
        f'<div class="locked-overlay">'
        f'<div class="locked-title">🔒 {html_lib.escape(message)}</div>'
        f'<div class="locked-sub">Desbloqueá picks + combinadas + más ligas</div>'
        f'<button class="pill locked-btn" onclick="event.stopPropagation(); openPremium();">Ver Premium</button>'
        f'</div>'
        f'<div class="locked-content">'
        f'<div class="row">'
        f'<span class="team-row"><span class="team-name">Equipo Local</span></span>'
        f'<span class="vs">vs</span>'
        f'<span class="team-row"><span class="team-name">Equipo Visitante</span></span>'
        f'</div>'
        f'<div class="meta">2026-02-26T21:00:00Z</div>'
        f'<div class="pick pick-best">'
        f'<span class="pick-main">Market</span>'
        f'<span class="pick-badge">PENDING</span>'
        f'<span class="pick-prob">&mdash; 62.5%</span>'
        f'</div>'
        f'<div class="conf-wrap conf-mid">'
        f'<div class="conf-label"><b>CONF 7/10</b></div>'
        f'<div class="conf-track">'
        f'<span class="conf-tick on"></span><span class="conf-tick on"></span>'
        f'<span class="conf-tick on"></span><span class="conf-tick on"></span>'
        f'<span class="conf-tick on"></span><span class="conf-tick on"></span>'
        f'<span class="conf-tick on"></span><span class="conf-tick"></span>'
        f'<span class="conf-tick"></span><span class="conf-tick"></span>'
        f'</div>'
        f'</div>'
        f'<div class="candidates">'
        f'<div class="cand-row"><div class="cand-head">'
        f'<span class="cand-mkt">O/U 2.5</span><span class="cand-pct">58%</span>'
        f'</div><div class="cand-track"><div class="cand-fill fill-mid" style="width:58%"></div></div></div>'
        f'<div class="cand-row"><div class="cand-head">'
        f'<span class="cand-mkt">BTTS</span><span class="cand-pct">54%</span>'
        f'</div><div class="cand-track"><div class="cand-fill fill-low" style="width:54%"></div></div></div>'
        f'</div>'
        f'</div>'
        f'</div>'
    )


def _locked_grid(n: int = 6, message: str = "Disponible en Premium") -> str:
    return "".join(_locked_card(message) for _ in range(max(1, int(n))))


def _premium_unlock_card() -> str:
    """Card que se muestra después de los primeros 3 picks para usuarios free."""
    return (
        '<div class="card premium-unlock-card" onclick="openPremium()" role="button" tabindex="0">'
        '<div class="premium-unlock-inner">'
        '<div class="premium-unlock-title">🔒 Desbloqueá todos los picks AFTR</div>'
        '<p class="premium-unlock-sub">Los usuarios free ven solo 3 picks por día.</p>'
        '<p class="premium-unlock-sub">AFTR Premium incluye:</p>'
        '<ul class="premium-unlock-list">'
        '<li>Todas las selecciones del día</li>'
        '<li>Apuestas de valor con ventaja positiva</li>'
        '<li>Picks de todas las ligas</li>'
        '<li>Desglose de probabilidades</li>'
        '</ul>'
        '<button class="pill premium-unlock-btn" onclick="event.stopPropagation(); openPremium();">Obtener Premium</button>'
        '</div>'
        '</div>'
    )


# =========================================================
# Pick card renderer
# =========================================================

def _render_pick_card(
    p: dict,
    best: dict | None = None,
    match_by_id: dict | None = None,
) -> str:
    """Renderiza el flip card completo (front + back) de un pick."""
    global _finished_card_debug_logged

    home_name      = p.get("home", "")
    away_name      = p.get("away", "")
    home_team_attr = html_lib.escape(str(home_name or ""))
    away_team_attr = html_lib.escape(str(away_name or ""))
    home_part      = _team_with_crest(p.get("home_crest"), home_name)
    away_part      = _team_with_crest(p.get("away_crest"), away_name)
    best_market    = (best or {}).get("market") or p.get("best_market") or "—"

    best_prob = (best or {}).get("prob")
    if best_prob is None:
        best_prob = p.get("best_prob")
    best_prob_present = best_prob is not None
    best_prob_pct     = round(_safe_float(best_prob, 0) * 100, 1)

    best_fair     = (best or {}).get("fair") or p.get("best_fair")
    best_fair_str = f" • {best_fair}" if best_fair is not None else ""

    # ── Resultado ──────────────────────────────────────────
    result     = _result_norm(p)
    status_raw = str(p.get("status") or "").strip().upper()
    finished_flag_raw = p.get("finished")
    if isinstance(finished_flag_raw, bool):
        finished_flag = finished_flag_raw
    elif finished_flag_raw is not None:
        finished_flag = str(finished_flag_raw).strip().lower() in {"1", "true", "yes", "y", "finished"}
    else:
        finished_flag = False

    if status_raw in ("WIN", "LOSS", "PUSH"):
        result = status_raw
    else:
        maybe = _result_norm({"result": status_raw})
        if maybe in ("WIN", "LOSS", "PUSH"):
            result = maybe

    # ── Match state lookup ─────────────────────────────────
    match_for_state: dict | None = None
    if isinstance(match_by_id, dict):
        mid = _safe_int(p.get("match_id") or p.get("id"))
        if mid is not None:
            if mid in match_by_id:
                match_for_state = match_by_id[mid]
            else:
                league_code = (p.get("_league") or p.get("league") or "").strip()
                if league_code:
                    for k in [
                        (league_code, mid),
                        (str(league_code), mid),
                        (league_code, str(mid)),
                        (str(league_code), str(mid)),
                    ]:
                        if k in match_by_id:
                            match_for_state = match_by_id[k]
                            break

    is_finished      = isMatchFinished(p) or (isMatchFinished(match_for_state) if isinstance(match_for_state, dict) else False)
    is_live_display  = isinstance(match_for_state, dict) and isMatchLive(match_for_state)
    if is_live_display:
        is_finished = False

    final_home_score: int | None = None
    final_away_score: int | None = None
    if is_finished:
        final_home_score, final_away_score = _extract_score(p, match_by_id)
        if not _finished_card_debug_logged:
            logger.debug(
                "Finished card: home=%s away=%s score=%s-%s result=%s status=%s",
                p.get("home") or p.get("home_team") or "",
                p.get("away") or p.get("away_team") or "",
                final_home_score, final_away_score,
                p.get("result") or "", p.get("status") or "",
            )
            _finished_card_debug_logged = True

    # ── CSS classes ────────────────────────────────────────
    card_class = "card aftr-pick-card"
    if is_live_display:
        card_class = "card aftr-pick-card aftr-pick-card--live"
    elif result == "WIN":
        card_class = "card pick-win aftr-pick-card"
    elif result == "LOSS":
        card_class = "card pick-loss aftr-pick-card"
    elif result == "PUSH":
        card_class = "card pick-push aftr-pick-card"

    risk       = _risk_label_from_conf(p)
    badge_html = (
        f'<span class="pick-badge">{html_lib.escape(result)}</span>'
        f'<span class="pick-badge risk {html_lib.escape(risk.lower())}">{html_lib.escape(risk)}</span>'
    )

    # ── Teams block ────────────────────────────────────────
    live_hs: int | None = None
    live_as: int | None = None
    if is_live_display and isinstance(match_for_state, dict):
        live_hs, live_as = _extract_score_from_match(match_for_state)

    if is_live_display and live_hs is not None and live_as is not None:
        teams_html = (
            f'<div class="aftr-teams aftr-teams-live">'
            f'<div class="aftr-team aftr-team-left">{home_part}</div>'
            f'<div class="aftr-score-inline aftr-score-inline-live">'
            f'<span class="aftr-score-home">{live_hs}</span>'
            f'<span class="aftr-score-sep">-</span>'
            f'<span class="aftr-score-away">{live_as}</span>'
            f'</div>'
            f'<div class="aftr-team aftr-team-right">{away_part}</div>'
            f'</div>'
        )
    elif is_finished and final_home_score is not None and final_away_score is not None:
        teams_html = (
            f'<div class="aftr-teams aftr-teams-finished">'
            f'<div class="aftr-team aftr-team-left">{home_part}</div>'
            f'<div class="aftr-score-inline">'
            f'<span class="aftr-score-home">{final_home_score}</span>'
            f'<span class="aftr-score-sep">-</span>'
            f'<span class="aftr-score-away">{final_away_score}</span>'
            f'</div>'
            f'<div class="aftr-team aftr-team-right">{away_part}</div>'
            f'</div>'
        )
    else:
        teams_html = (
            f'<div class="aftr-teams">'
            f'<div class="aftr-team aftr-team-left">{home_part}</div>'
            f'<div class="aftr-vs">vs</div>'
            f'<div class="aftr-team aftr-team-right">{away_part}</div>'
            f'</div>'
        )

    # ── CONF bar ───────────────────────────────────────────
    conf_bar = ""
    conf_i = _safe_int(p.get("confidence"))
    if conf_i is not None:
        conf_i    = max(1, min(10, int(conf_i)))
        ticks     = "".join(
            f'<span class="conf-tick{"" if i > conf_i else " on"}"></span>'
            for i in range(1, 11)
        )
        level_cls = "conf-high" if conf_i >= 8 else ("conf-mid" if conf_i >= 5 else "conf-low")
        conf_bar  = (
            f'<div class="conf-wrap {level_cls}">'
            f'<div class="conf-label"><b>CONF {conf_i}/10</b></div>'
            f'<div class="conf-track">{ticks}</div>'
            f'</div>'
        )

    # ── Candidates (top 3) ─────────────────────────────────
    candidates = [c for c in (p.get("candidates") or []) if isinstance(c, dict)][:3]
    cand_lines = []
    for c in candidates:
        mkt      = html_lib.escape((c.get("market") or "—"))
        prob     = _safe_float(c.get("prob", 0))
        prob_pct = round(prob * 100, 1)
        w_pct    = max(0.0, min(100.0, prob_pct))
        fill_cls = "fill-high" if prob_pct >= 75 else ("fill-mid" if prob_pct >= 55 else "fill-low")
        cand_lines.append(
            f'<div class="cand-row">'
            f'<div class="cand-head">'
            f'<span class="cand-mkt">{mkt}</span>'
            f'<span class="cand-pct">{prob_pct}%</span>'
            f'</div>'
            f'<div class="cand-track">'
            f'<div class="cand-fill {fill_cls}" data-w="{w_pct}"></div>'
            f'</div>'
            f'</div>'
        )
    cand_block = "".join(cand_lines) if cand_lines else "<div class='cand-line muted'>Sin candidatos</div>"

    # ── Odds line ──────────────────────────────────────────
    edge_val        = p.get("edge")
    bookmaker_title = str(p.get("bookmaker_title") or "").strip()
    odds_parts      = []
    odds_display    = _pick_odds_display_value(p)
    if odds_display != "—":
        odds_parts.append(odds_display if odds_display.startswith("Impl") else f"Odds {odds_display}")
    if edge_val is not None:
        try:
            ev   = float(edge_val)
            sign = "+" if ev >= 0 else ""
            odds_parts.append(f"Ventaja {sign}{ev * 100:.1f}%")
        except (TypeError, ValueError):
            pass
    odds_line_html = ""
    if odds_parts:
        odds_line_html = '<div class="pick-odds muted">' + html_lib.escape(" • ".join(odds_parts))
        if bookmaker_title:
            odds_line_html += f' <span class="pick-bookmaker">{html_lib.escape(bookmaker_title)}</span>'
        odds_line_html += "</div>"

    # Solo cuota (sin probabilidad)
    oc = _pick_odds_display_value(p)
    prob_odds_html = (
        f'<div class="aftr-prob-odds"><span class="aftr-odds">@{html_lib.escape(oc)}</span></div>'
        if oc != "—" else ""
    )

    # ── AFTR Score block ───────────────────────────────────
    aftr_score_raw = p.get("aftr_score")
    try:
        aftr_score_val = int(round(float(aftr_score_raw))) if aftr_score_raw is not None else _aftr_score(p)
    except (TypeError, ValueError):
        aftr_score_val = _aftr_score(p)

    _t         = p.get("tier")
    tier       = (str(_t).strip().lower() if _t is not None else "pass") or "pass"
    tier_colors = {"elite": "#FFD700", "strong": "#00C853", "risky": "#FF9800", "pass": "#9E9E9E"}
    tier_color = tier_colors.get(tier, "#9E9E9E")
    tier_label = "watch" if tier == "pass" else tier

    edge_badge = ""
    if edge_val is not None:
        try:
            val        = float(str(edge_val).replace(",", ".")) * 100
            edge_badge = f"+{val:.1f}%" if val >= 0 else f"{val:.1f}%"
        except (TypeError, ValueError):
            pass

    aftr_badges = [
        f'<span class="aftr-badge aftr-badge-tier" style="border-color:{tier_color};color:{tier_color};">'
        f'{html_lib.escape(tier_label)}</span>',
    ]
    if edge_badge:
        aftr_badges.append(
            f'<span class="aftr-badge aftr-badge-edge">{html_lib.escape(edge_badge)} EDGE</span>'
        )
    # ── SVG circular gauge (r=28, C=175.93) ───────────────
    _GAUGE_C = 175.93
    _gauge_pct    = max(0, min(100, aftr_score_val)) / 100
    _gauge_offset = _GAUGE_C * (1.0 - _gauge_pct)   # JS animates from C → this value
    _gauge_svg = (
        f'<svg class="aftr-gauge-svg" viewBox="0 0 64 64" width="58" height="58" aria-hidden="true">'
        f'<circle class="aftr-gauge-bg" cx="32" cy="32" r="28"/>'
        f'<circle class="aftr-gauge-arc" cx="32" cy="32" r="28"'
        f' stroke="{tier_color}"'
        f' stroke-dasharray="{_GAUGE_C:.2f}"'
        f' stroke-dashoffset="{_GAUGE_C:.2f}"'
        f' data-gauge-to="{_gauge_offset:.2f}"/>'
        f'<text class="aftr-gauge-num" x="32" y="37">{aftr_score_val}</text>'
        f'</svg>'
    )
    aftr_block_html = (
        f'<div class="aftr-score-block">'
        f'<div class="aftr-gauge-wrap">'
        f'{_gauge_svg}'
        f'<div class="aftr-score-label">AFTR</div>'
        f'</div>'
        f'<div class="aftr-badges">{"".join(aftr_badges)}</div>'
        f'</div>'
    )

    # ── Top meta row ───────────────────────────────────────
    kickoff_time = format_match_kickoff_ar(p.get("utcDate"))
    if kickoff_time == "—" and isinstance(match_for_state, dict):
        kickoff_time = format_match_kickoff_ar(match_for_state.get("utcDate"))
    if is_live_display and isinstance(match_for_state, dict):
        kickoff_time = _format_live_status_line(match_for_state)

    meta_time_class = "aftr-meta-time" + (" aftr-meta-live" if is_live_display else "")
    league_code     = (p.get("_league") or p.get("league") or "").strip()
    league_label    = settings.leagues.get(league_code, league_code) if league_code else "AFTR"
    tier_meta_badge = (
        f'<span class="aftr-meta-tier-pill" style="border-color:{tier_color};color:{tier_color};">'
        f'{html_lib.escape(tier_label)}</span>'
    )
    if is_finished and tier_label == "watch":
        tier_meta_badge = ""

    # ── IDs & attrs ────────────────────────────────────────
    pick_id_attr = html_lib.escape(_pick_id_for_card(p, best))
    market_attr  = html_lib.escape(str((best or {}).get("market") or p.get("best_market") or ""))
    edge_attr    = html_lib.escape(str(edge_val)) if edge_val is not None else ""
    utc_date_attr = html_lib.escape(str(p.get("utcDate") or ""))

    # ── Actions / finished state ───────────────────────────
    if is_finished:
        if final_home_score is None or final_away_score is None:
            logger.debug(
                "Missing score for pick_id=%s match_id=%s result=%s status=%s",
                p.get("id") or p.get("pick_id") or "",
                p.get("match_id") or "",
                p.get("result") or "", p.get("status") or "",
            )
        outcome_badge = result if result in ("WIN", "LOSS", "PUSH") else "FINALIZADO"
        _badge_extra  = (" pick-badge--win" if outcome_badge == "WIN"
                         else " pick-badge--loss" if outcome_badge == "LOSS" else "")
        prob_line     = f'<div class="pick-finished-prob">{best_prob_pct:.1f}%</div>' if best_prob_present else ""
        pick_actions_html = (
            f'<div class="pick-finished-status pick-main-highlight">'
            f'<div class="pick-finished-top">'
            f'<div class="pick-finished-market">{_market_html(str(best_market))}</div>'
            f'{prob_line}'
            f'</div>'
            f'<div class="pick-finished-badge-row">'
            f'<span class="pick-badge{_badge_extra}">{html_lib.escape(outcome_badge)}</span>'
            f'</div>'
            f'</div>'
        )
    else:
        mid_val      = _safe_int(p.get("match_id") or p.get("id"))
        league_attr  = html_lib.escape(str(p.get("_league") or p.get("league") or ""))
        mid_attr     = str(mid_val) if mid_val is not None else ""
        detail_btn   = (
            f'<button type="button" class="btn-match-detail pick-detail-link"'
            f' data-league="{league_attr}" data-match-id="{mid_attr}">Ver partido →</button>'
        ) if league_attr and mid_attr else ""
        pick_actions_html = (
            f'<div class="pick-actions-wrap">'
            f'{detail_btn}'
            f'<div class="pick-actions aftr-actions">'
            f'<button type="button" class="btn-favorite-pick pill pick-action-btn"'
            f' data-pick-id="{pick_id_attr}" data-market="{market_attr}"'
            f' data-aftr-score="{aftr_score_val}" data-tier="{html_lib.escape(tier)}"'
            f' data-edge="{edge_attr}" data-home-team="{home_team_attr}" data-away-team="{away_team_attr}">'
            f'⭐ Guardar</button>'
            f'<button type="button" class="btn-follow-pick pill pick-action-btn pick-action-follow"'
            f' data-pick-id="{pick_id_attr}" data-market="{market_attr}"'
            f' data-aftr-score="{aftr_score_val}" data-tier="{html_lib.escape(tier)}"'
            f' data-edge="{edge_attr}" data-home-team="{home_team_attr}" data-away-team="{away_team_attr}">'
            f'📈 Seguir pick</button>'
            f'</div>'
            f'<button type="button" class="btn-add-tracker pill pick-action-btn pick-action-tracker"'
            f' data-home="{home_team_attr}" data-away="{away_team_attr}"'
            f' data-market="{market_attr}" data-utcdate="{utc_date_attr}"'
            f' onclick="addPickToTracker(this)">'
            f'📊 Agregar al Tracker</button>'
            f'</div>'
        )

    mainpick_html = ""
    if not is_finished:
        mainpick_html = (
            f'<div class="aftr-mainpick pick-main-highlight">'
            f'<div class="aftr-market">{_market_html(str(best_market))}</div>'
            f'{prob_odds_html}'
            f'</div>'
        )

    score_and_actions_html = (
        pick_actions_html + aftr_block_html if is_finished
        else aftr_block_html + pick_actions_html
    )

    front_html = (
        f'<div class="{card_class}">'
        f'<div class="aftr-topmeta">'
        f'<span class="aftr-meta-league">{html_lib.escape(league_label)}</span>'
        f'<span class="{meta_time_class}">{html_lib.escape(kickoff_time)}</span>'
        f'{tier_meta_badge}'
        f'</div>'
        f'{teams_html}'
        f'{mainpick_html}'
        f'{score_and_actions_html}'
        f'</div>'
    )

    return front_html
