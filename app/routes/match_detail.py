"""
GET /api/match/{league}/{match_id}/detail
Devuelve HTML del panel de detalle de partido (drawer): Forma · Predicción · Tabla.
"""
from __future__ import annotations

import html as html_lib
import logging
import traceback

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from config.settings import settings
from data.cache import read_json_with_fallback
from app.timefmt import format_match_kickoff_ar
from app.ui_helpers import _safe_int, _safe_float

logger = logging.getLogger("aftr.routes.match_detail")

router = APIRouter()

_FALLBACK_CREST = "/static/teams/default.svg"


# ─────────────────────────────────────────────
# Helpers de rendering
# ─────────────────────────────────────────────

def _crest_img(src: str | None, size: int = 24, cls: str = "md-crest") -> str:
    s = html_lib.escape((src or "").strip() or _FALLBACK_CREST)
    fb = html_lib.escape(_FALLBACK_CREST)
    return (
        f'<img src="{s}" class="{cls}" loading="lazy" width="{size}" height="{size}" '
        f'onerror="this.src=\'{fb}\';this.onerror=null;"/>'
    )


def _form_dot(result: str) -> str:
    r = (result or "").strip().upper()
    cls = {"W": "form-dot--w", "D": "form-dot--d", "L": "form-dot--l"}.get(r, "form-dot--u")
    label = {"W": "G", "D": "E", "L": "P"}.get(r, "?")
    return f'<span class="form-dot {cls}" title="{r}">{label}</span>'


def _render_form_col(team_name: str, crest: str | None, stats: dict) -> str:
    if not stats:
        return f'<div class="md-form-col"><div class="md-form-name">{html_lib.escape(team_name)}</div><p class="muted">Sin datos</p></div>'

    form_str  = str(stats.get("form") or "")
    form_dots = "".join(_form_dot(r) for r in form_str.replace(" ", "").upper()[:5])

    gf    = _safe_float(stats.get("gf"), None)
    ga    = _safe_float(stats.get("ga"), None)
    o25   = _safe_float(stats.get("over25"), None)
    btts  = _safe_float(stats.get("btts"), None)

    gf_str   = f"{gf:.1f}" if gf is not None else "—"
    ga_str   = f"{ga:.1f}" if ga is not None else "—"
    o25_str  = f"{o25*100:.0f}%" if o25 is not None else "—"
    btts_str = f"{btts*100:.0f}%" if btts is not None else "—"

    recent = [r for r in (stats.get("recent") or []) if isinstance(r, dict)][:5]
    recent_rows = ""
    for r in recent:
        res   = (r.get("res") or "").upper()
        gf_r  = r.get("gf")
        ga_r  = r.get("ga")
        opp   = html_lib.escape(str(r.get("opp_name") or "—"))
        is_home = r.get("is_home", True)
        loc   = "L" if is_home else "V"
        score = f"{gf_r}-{ga_r}" if gf_r is not None and ga_r is not None else "—"
        res_cls = {"W": "md-res--w", "D": "md-res--d", "L": "md-res--l"}.get(res, "")
        opp_crest = _crest_img(r.get("opp_crest"), 16, "md-crest-sm")
        recent_rows += (
            f'<div class="md-recent-row">'
            f'<span class="md-recent-loc">{loc}</span>'
            f'{opp_crest}'
            f'<span class="md-recent-opp">{opp}</span>'
            f'<span class="md-recent-score">{score}</span>'
            f'<span class="md-res {res_cls}">{res}</span>'
            f'</div>'
        )

    return (
        f'<div class="md-form-col">'
        f'<div class="md-form-name">'
        f'{_crest_img(crest, 20, "md-crest-sm")} {html_lib.escape(team_name)}'
        f'</div>'
        f'<div class="md-form-dots">{form_dots}</div>'
        f'<div class="md-form-stats">'
        f'<div class="md-stat"><span class="md-stat-lbl">GF/PJ</span><span class="md-stat-val">{gf_str}</span></div>'
        f'<div class="md-stat"><span class="md-stat-lbl">GA/PJ</span><span class="md-stat-val">{ga_str}</span></div>'
        f'<div class="md-stat"><span class="md-stat-lbl">+2.5</span><span class="md-stat-val">{o25_str}</span></div>'
        f'<div class="md-stat"><span class="md-stat-lbl">BTTS</span><span class="md-stat-val">{btts_str}</span></div>'
        f'</div>'
        f'<div class="md-recent">{recent_rows}</div>'
        f'</div>'
    )


def _render_tab_pred(pick: dict) -> str:
    if not pick:
        return '<p class="muted">Sin datos de predicción.</p>'

    market   = html_lib.escape(str(pick.get("best_market") or "—"))
    prob     = _safe_float(pick.get("best_prob"), None)
    fair     = _safe_float(pick.get("best_fair"), None)
    edge     = _safe_float(pick.get("edge"), None)
    aftr_sc  = pick.get("aftr_score") or 0
    xg_h     = _safe_float(pick.get("xg_home"), None)
    xg_a     = _safe_float(pick.get("xg_away"), None)
    xg_t     = _safe_float(pick.get("xg_total"), None)
    probs    = pick.get("probs") or {}

    prob_str  = f"{prob*100:.1f}%" if prob is not None else "—"
    fair_str  = f"{fair:.2f}" if fair is not None else "—"
    edge_str  = f"{edge*100:+.1f}%" if edge is not None else "—"
    edge_cls  = "md-pred-edge--pos" if (edge or 0) > 0 else "md-pred-edge--neg"
    xg_h_str  = f"{xg_h:.2f}" if xg_h is not None else "—"
    xg_a_str  = f"{xg_a:.2f}" if xg_a is not None else "—"
    xg_t_str  = f"{xg_t:.2f}" if xg_t is not None else "—"

    prob_rows = ""
    PROB_LABELS = [
        ("home",     "1 (Local)"),
        ("draw",     "X (Empate)"),
        ("away",     "2 (Visita)"),
        ("over_25",  "Más de 2.5"),
        ("under_25", "Menos de 2.5"),
        ("btts_yes", "BTTS Sí"),
        ("btts_no",  "BTTS No"),
    ]
    for key, lbl in PROB_LABELS:
        v = _safe_float(probs.get(key), None)
        if v is None:
            continue
        pct = round(v * 100, 1)
        bar_w = min(100, max(0, pct))
        prob_rows += (
            f'<div class="md-prob-row">'
            f'<span class="md-prob-lbl">{html_lib.escape(lbl)}</span>'
            f'<div class="md-prob-bar"><div class="md-prob-fill" style="width:{bar_w:.0f}%"></div></div>'
            f'<span class="md-prob-val">{pct}%</span>'
            f'</div>'
        )

    return (
        f'<div class="md-pred-hero">'
        f'<div class="md-pred-market">{market}</div>'
        f'<div class="md-pred-row">'
        f'<div class="md-pred-kv"><span class="md-pred-k">Prob.</span><span class="md-pred-v">{prob_str}</span></div>'
        f'<div class="md-pred-kv"><span class="md-pred-k">Cuota justa</span><span class="md-pred-v">{fair_str}</span></div>'
        f'<div class="md-pred-kv"><span class="md-pred-k">Edge</span>'
        f'<span class="md-pred-v {edge_cls}">{edge_str}</span></div>'
        f'<div class="md-pred-kv"><span class="md-pred-k">AFTR</span><span class="md-pred-v">{aftr_sc}</span></div>'
        f'</div>'
        f'</div>'
        f'<div class="md-xg-row">'
        f'<div class="md-xg-block"><span class="md-xg-val">{xg_h_str}</span><span class="md-xg-lbl">xG Local</span></div>'
        f'<div class="md-xg-block"><span class="md-xg-val">{xg_t_str}</span><span class="md-xg-lbl">xG Total</span></div>'
        f'<div class="md-xg-block"><span class="md-xg-val">{xg_a_str}</span><span class="md-xg-lbl">xG Visita</span></div>'
        f'</div>'
        f'<div class="md-probs">{prob_rows}</div>'
    )


def _render_tab_tabla(standings: list[dict], home_id: int | None, away_id: int | None) -> str:
    if not standings:
        return '<p class="muted" style="padding:12px 0">Tabla no disponible aún (se carga en el próximo refresh).</p>'

    rows = ""
    for s in standings:
        pos     = s.get("position") or "—"
        tid     = s.get("team_id")
        name    = html_lib.escape(str(s.get("team_name") or "—"))
        crest   = s.get("team_crest")
        pts     = s.get("points") or 0
        played  = s.get("played") or 0
        gd      = s.get("gd") or 0
        won     = s.get("won") or 0
        draw    = s.get("draw") or 0
        lost    = s.get("lost") or 0
        gf      = s.get("gf") or 0
        ga      = s.get("ga") or 0

        is_home_team = tid is not None and tid == home_id
        is_away_team = tid is not None and tid == away_id
        highlight = " md-table-row--home" if is_home_team else (" md-table-row--away" if is_away_team else "")

        gd_int  = int(gd) if gd else 0
        gd_str  = f"+{gd_int}" if gd_int > 0 else str(gd_int)
        rows += (
            f'<tr class="md-table-row{highlight}">'
            f'<td class="md-td-pos">{pos}</td>'
            f'<td class="md-td-team">'
            f'{_crest_img(crest, 16, "md-crest-sm")}'
            f'<span class="md-table-name">{name}</span>'
            f'</td>'
            f'<td>{played}</td>'
            f'<td>{won}</td><td>{draw}</td><td>{lost}</td>'
            f'<td>{gf}</td><td>{ga}</td>'
            f'<td class="md-td-gd">{gd_str}</td>'
            f'<td class="md-td-pts">{pts}</td>'
            f'</tr>'
        )

    return (
        f'<div class="md-table-wrap">'
        f'<table class="md-table">'
        f'<thead><tr>'
        f'<th>#</th><th>Equipo</th><th>PJ</th>'
        f'<th>G</th><th>E</th><th>P</th>'
        f'<th>GF</th><th>GA</th><th>+/-</th><th class="md-td-pts">Pts</th>'
        f'</tr></thead>'
        f'<tbody>{rows}</tbody>'
        f'</table>'
        f'</div>'
    )


def _render_match_detail(
    match: dict | None,
    pick: dict | None,
    standings: list[dict],
    league: str,
) -> str:
    match = match or {}
    pick  = pick  or {}

    home       = str(match.get("home") or pick.get("home") or "—")
    away       = str(match.get("away") or pick.get("away") or "—")
    h_crest    = match.get("home_crest") or pick.get("home_crest")
    a_crest    = match.get("away_crest") or pick.get("away_crest")
    h_id       = _safe_int(match.get("home_team_id") or pick.get("home_team_id"))
    a_id       = _safe_int(match.get("away_team_id") or pick.get("away_team_id"))
    utc_date   = match.get("utcDate") or pick.get("utcDate") or ""
    kickoff    = format_match_kickoff_ar(utc_date)
    league_name = html_lib.escape(settings.leagues.get(league, league))

    stats_h = pick.get("stats_home") or {}
    stats_a = pick.get("stats_away") or {}

    tab_forma = (
        f'<div class="md-form-grid">'
        f'{_render_form_col(home, h_crest, stats_h)}'
        f'{_render_form_col(away, a_crest, stats_a)}'
        f'</div>'
    )
    tab_pred  = _render_tab_pred(pick)
    tab_tabla = _render_tab_tabla(standings, h_id, a_id)

    return (
        f'<div class="md-content">'
        # Header
        f'<div class="md-header">'
        f'<div class="md-header-league">{league_name}</div>'
        f'<div class="md-header-teams">'
        f'<div class="md-header-team">'
        f'{_crest_img(h_crest, 32, "md-crest-lg")}'
        f'<span class="md-header-name">{html_lib.escape(home)}</span>'
        f'</div>'
        f'<div class="md-header-vs">'
        f'<span class="md-header-kickoff">{html_lib.escape(kickoff)}</span>'
        f'<span class="md-vs-label">vs</span>'
        f'</div>'
        f'<div class="md-header-team md-header-team--away">'
        f'{_crest_img(a_crest, 32, "md-crest-lg")}'
        f'<span class="md-header-name">{html_lib.escape(away)}</span>'
        f'</div>'
        f'</div>'
        f'</div>'
        # Tabs
        f'<div class="md-tabs">'
        f'<button class="md-tab active" data-tab="forma">Forma</button>'
        f'<button class="md-tab" data-tab="pred">Predicción</button>'
        f'<button class="md-tab" data-tab="tabla">Tabla</button>'
        f'</div>'
        # Panels
        f'<div class="md-panel" data-panel="forma">{tab_forma}</div>'
        f'<div class="md-panel md-panel--hidden" data-panel="pred">{tab_pred}</div>'
        f'<div class="md-panel md-panel--hidden" data-panel="tabla">{tab_tabla}</div>'
        f'</div>'
    )


# ─────────────────────────────────────────────
# Endpoint
# ─────────────────────────────────────────────

@router.get("/match/{league}/{match_id}/detail")
def match_detail(league: str, match_id: int) -> HTMLResponse:
    if not settings.is_valid_league(league):
        league = settings.default_league

    matches   = read_json_with_fallback(f"daily_matches_{league}.json") or []
    picks     = read_json_with_fallback(f"daily_picks_{league}.json") or []
    standings = read_json_with_fallback(f"standings_{league}.json") or []

    # Si no hay standings en cache, intentar fetchearlos on-demand y cachearlos
    if not standings:
        try:
            from data.providers.football_data import get_standings
            from data.cache import write_json
            standings = get_standings(league) or []
            if standings:
                write_json(f"standings_{league}.json", standings)
        except Exception:
            standings = []

    match = next(
        (m for m in matches if isinstance(m, dict) and _safe_int(m.get("match_id") or m.get("id")) == match_id),
        None,
    )
    pick = next(
        (p for p in picks if isinstance(p, dict) and _safe_int(p.get("match_id") or p.get("id")) == match_id),
        None,
    )

    try:
        html = _render_match_detail(match, pick, standings, league)
    except Exception:
        tb = traceback.format_exc()
        logger.error("match_detail render error league=%s match_id=%s:\n%s", league, match_id, tb)
        html = f'<p class="muted" style="padding:20px">Error al cargar datos del partido.</p><pre style="font-size:11px;color:#f87171;padding:12px;overflow:auto">{html_lib.escape(tb)}</pre>'
    return HTMLResponse(html)
