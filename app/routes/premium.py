"""
AFTR Premium — dashboard y endpoints para usuarios con suscripción activa.

Rutas:
  GET  /premium              → dashboard HTML completo
  GET  /api/premium/stats    → JSON con ranking report completo
  GET  /api/premium/value    → JSON con value picks del día
  GET  /api/premium/insights → JSON con insights rápidos
"""
from __future__ import annotations

import html as html_lib
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.auth import get_user_id, get_user_by_id
from app.models import get_active_plan
from app.user_helpers import can_see_all_picks, is_admin, is_premium_active

logger = logging.getLogger("aftr.premium")
router = APIRouter()


# ─── access guard ────────────────────────────────────────────────────────────

def _check_premium(request: Request) -> tuple[int | None, dict | None, bool]:
    """Returns (user_id, user_dict, is_allowed)."""
    uid = get_user_id(request)
    if not uid:
        return None, None, False
    user = get_user_by_id(uid)
    allowed = is_admin(user, request) or is_premium_active(user)
    return uid, user, allowed


# ─── JSON API endpoints ───────────────────────────────────────────────────────

@router.get("/api/premium/stats")
def api_premium_stats(request: Request):
    """Ranking report completo (cached 5 min)."""
    uid, user, allowed = _check_premium(request)
    if not allowed:
        return JSONResponse({"error": "premium_required"}, status_code=403)
    try:
        from core.ranking import get_full_ranking_report
        report = get_full_ranking_report()
        return JSONResponse(report)
    except Exception as e:
        logger.exception("api_premium_stats error")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/premium/value")
def api_premium_value(request: Request, min_edge: float = 0.04, top: int = 10):
    """Value picks del día ordenados por value_rating."""
    uid, user, allowed = _check_premium(request)
    if not allowed:
        return JSONResponse({"error": "premium_required"}, status_code=403)
    try:
        from core.value import get_todays_value_picks
        picks = get_todays_value_picks(min_edge=min_edge, top_n=top)
        return JSONResponse({"picks": picks, "count": len(picks)})
    except Exception as e:
        logger.exception("api_premium_value error")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/premium/insights")
def api_premium_insights(request: Request):
    """Resumen rápido: racha, mejor liga, summary de valor del día."""
    uid, user, allowed = _check_premium(request)
    if not allowed:
        return JSONResponse({"error": "premium_required"}, status_code=403)
    try:
        from core.ranking import get_full_ranking_report
        from core.value import get_value_summary
        report = get_full_ranking_report()
        value_summary = get_value_summary()
        streaks = report.get("streaks", {})
        by_league = report.get("by_league", [])
        best_league = by_league[0] if by_league else None
        return JSONResponse({
            "streaks": streaks,
            "best_league": best_league,
            "global": report.get("global", {}),
            "value_summary": value_summary,
            "recent_form": report.get("recent_form", [])[:5],
        })
    except Exception as e:
        logger.exception("api_premium_insights error")
        return JSONResponse({"error": str(e)}, status_code=500)


# ─── HTML dashboard ───────────────────────────────────────────────────────────

@router.get("/premium", response_class=HTMLResponse)
def premium_dashboard(request: Request):
    uid, user, allowed = _check_premium(request)
    if not uid:
        return RedirectResponse(url="/?msg=need_login", status_code=302)
    if not allowed:
        return RedirectResponse(url="/?msg=premium_required", status_code=302)

    username = (user.get("username") or user.get("email") or "Usuario").split("@")[0] if user else "Usuario"

    try:
        from core.ranking import get_full_ranking_report
        from core.value import get_todays_value_picks, value_rating_color
        report = get_full_ranking_report()
        value_picks = get_todays_value_picks(top_n=8)
    except Exception as e:
        logger.exception("premium_dashboard: data load error")
        report = {}
        value_picks = []

    g = report.get("global", {})
    streaks = report.get("streaks", {})
    recent = report.get("recent_form", [])
    by_league = report.get("by_league", [])
    by_market = report.get("by_market", [])
    curve = report.get("curve", [])
    by_month = report.get("by_month", [])

    # ── KPI cards ─────────────────────────────────────────────────────────────
    roi_color = "#22c55e" if (g.get("roi") or 0) >= 0 else "#ef4444"
    net_color = "#22c55e" if (g.get("net_units") or 0) >= 0 else "#ef4444"
    streak_val = streaks.get("current_streak", 0)
    streak_label = f"+{streak_val} victorias seguidas" if streak_val > 0 else (
        f"{streak_val} derrotas seguidas" if streak_val < 0 else "Sin racha activa"
    )
    streak_color = "#22c55e" if streak_val > 0 else ("#ef4444" if streak_val < 0 else "#64748b")

    kpi_html = f"""
    <div class="pm-kpi-grid">
      <div class="pm-kpi">
        <span class="pm-kpi-val" style="color:{roi_color};">{g.get('roi', 0):+.1f}%</span>
        <span class="pm-kpi-lbl">ROI histórico</span>
      </div>
      <div class="pm-kpi">
        <span class="pm-kpi-val" style="color:{net_color};">{g.get('net_units', 0):+.2f}u</span>
        <span class="pm-kpi-lbl">Ganancia neta</span>
      </div>
      <div class="pm-kpi">
        <span class="pm-kpi-val">{g.get('winrate', 0):.1f}%</span>
        <span class="pm-kpi-lbl">Win rate</span>
      </div>
      <div class="pm-kpi">
        <span class="pm-kpi-val">{g.get('total', 0)}</span>
        <span class="pm-kpi-lbl">Picks resueltos</span>
      </div>
      <div class="pm-kpi">
        <span class="pm-kpi-val">{g.get('avg_odds', 0):.2f}</span>
        <span class="pm-kpi-lbl">Cuota media</span>
      </div>
      <div class="pm-kpi">
        <span class="pm-kpi-val" style="color:{streak_color};">{streak_label}</span>
        <span class="pm-kpi-lbl">Racha actual</span>
      </div>
    </div>"""

    # ── Value picks ────────────────────────────────────────────────────────────
    def _value_badge(rating: int) -> str:
        if rating >= 70:
            return f'<span class="pm-vbadge pm-vbadge--high">⭐ Alto</span>'
        if rating >= 45:
            return f'<span class="pm-vbadge pm-vbadge--mid">Medio</span>'
        return f'<span class="pm-vbadge pm-vbadge--low">Bajo</span>'

    if value_picks:
        vp_rows = ""
        for p in value_picks:
            v = p.get("value", {})
            edge_pct = f"{v.get('edge', 0)*100:.1f}%"
            ev_val = v.get("ev", 0)
            ev_str = f"{ev_val:+.3f}"
            ev_color = "#22c55e" if ev_val > 0 else "#ef4444"
            kelly = f"{v.get('kelly_fraction', 0)*100:.1f}%"
            home = html_lib.escape(p.get("home") or p.get("home_team") or "")
            away = html_lib.escape(p.get("away") or p.get("away_team") or "")
            market = html_lib.escape(p.get("best_market") or "")
            odds = p.get("best_fair") or p.get("best_odds") or ""
            league_code = p.get("_league", "")
            match_str = f"{home} vs {away}" if home and away else "—"
            vp_rows += f"""
              <tr>
                <td><span class="pm-league-tag">{html_lib.escape(league_code)}</span></td>
                <td class="pm-match-cell">{match_str}</td>
                <td><strong>{market}</strong></td>
                <td>{odds}</td>
                <td><span style="color:#38bdf8;font-weight:700;">{edge_pct}</span></td>
                <td><span style="color:{ev_color};font-weight:600;">{ev_str}</span></td>
                <td>{kelly}</td>
                <td>{_value_badge(v.get('value_rating', 0))}</td>
              </tr>"""
        value_section = f"""
        <div class="pm-section">
          <div class="pm-section-head">
            <span class="pm-section-title">Value Picks de hoy</span>
            <span class="pm-section-sub">Picks con edge positivo vs cuota de mercado</span>
          </div>
          <div class="pm-table-wrap">
            <table class="pm-table">
              <thead><tr>
                <th>Liga</th><th>Partido</th><th>Mercado</th><th>Cuota</th>
                <th>Edge</th><th>EV/u</th><th>Kelly</th><th>Valor</th>
              </tr></thead>
              <tbody>{vp_rows}</tbody>
            </table>
          </div>
          <p class="pm-footnote">Edge = probabilidad AFTR − probabilidad implícita de la cuota. Kelly al 25% para gestión conservadora.</p>
        </div>"""
    else:
        value_section = """
        <div class="pm-section">
          <div class="pm-section-head"><span class="pm-section-title">Value Picks de hoy</span></div>
          <p class="pm-empty">No hay picks con edge significativo disponibles hoy.</p>
        </div>"""

    # ── Recent form ────────────────────────────────────────────────────────────
    form_dots = ""
    for p in recent[:10]:
        r = p.get("result", "")
        color = "#22c55e" if r == "WIN" else ("#ef4444" if r == "LOSS" else "#64748b")
        label = html_lib.escape(f"{p.get('home','')} vs {p.get('away','')} — {p.get('market','')} ({r})")
        form_dots += f'<span class="pm-form-dot" style="background:{color};" title="{label}"></span>'

    recent_rows = ""
    for p in recent:
        r = p.get("result", "")
        color = "#22c55e" if r == "WIN" else ("#ef4444" if r == "LOSS" else "#64748b")
        profit = p.get("profit", 0)
        profit_str = f"{profit:+.2f}u"
        profit_color = "#22c55e" if profit > 0 else ("#ef4444" if profit < 0 else "#64748b")
        recent_rows += f"""
          <tr>
            <td>{html_lib.escape(p.get('date',''))}</td>
            <td class="pm-match-cell">{html_lib.escape(p.get('home',''))} vs {html_lib.escape(p.get('away',''))}</td>
            <td>{html_lib.escape(p.get('market',''))}</td>
            <td>{html_lib.escape(p.get('league',''))}</td>
            <td>{p.get('odds') or '—'}</td>
            <td><span style="color:{color};font-weight:700;">{r}</span></td>
            <td><span style="color:{profit_color};font-weight:600;">{profit_str}</span></td>
          </tr>"""

    form_section = f"""
    <div class="pm-section">
      <div class="pm-section-head">
        <span class="pm-section-title">Forma reciente</span>
        <span class="pm-section-sub">Últimos {len(recent)} picks resueltos</span>
      </div>
      <div class="pm-form-strip">{form_dots}</div>
      <div class="pm-table-wrap" style="margin-top:12px;">
        <table class="pm-table">
          <thead><tr><th>Fecha</th><th>Partido</th><th>Mercado</th><th>Liga</th><th>Cuota</th><th>Result.</th><th>P/L</th></tr></thead>
          <tbody>{recent_rows}</tbody>
        </table>
      </div>
    </div>"""

    # ── League breakdown ───────────────────────────────────────────────────────
    league_rows = ""
    for lg in by_league[:8]:
        net = lg.get("net_units", 0)
        net_color = "#22c55e" if net >= 0 else "#ef4444"
        league_rows += f"""
          <tr>
            <td><strong>{html_lib.escape(lg.get('league_name',''))}</strong></td>
            <td>{lg.get('total',0)}</td>
            <td>{lg.get('winrate',0):.1f}%</td>
            <td>{lg.get('roi',0):+.1f}%</td>
            <td><span style="color:{net_color};font-weight:700;">{net:+.2f}u</span></td>
            <td>{lg.get('avg_odds',0):.2f}</td>
          </tr>"""

    league_section = f"""
    <div class="pm-section">
      <div class="pm-section-head"><span class="pm-section-title">Rendimiento por liga</span></div>
      <div class="pm-table-wrap">
        <table class="pm-table">
          <thead><tr><th>Liga</th><th>Picks</th><th>Win%</th><th>ROI</th><th>Neto</th><th>Cuota media</th></tr></thead>
          <tbody>{league_rows if league_rows else '<tr><td colspan="6" style="text-align:center;color:#475569;">Sin datos aún</td></tr>'}</tbody>
        </table>
      </div>
    </div>"""

    # ── Market breakdown ───────────────────────────────────────────────────────
    market_cards = ""
    for mk in by_market:
        net = mk.get("net_units", 0)
        net_color = "#22c55e" if net >= 0 else "#ef4444"
        market_cards += f"""
        <div class="pm-market-card">
          <div class="pm-market-name">{html_lib.escape(mk.get('market',''))}</div>
          <div class="pm-market-stat"><span class="pm-market-val" style="color:{net_color};">{net:+.2f}u</span><span class="pm-market-lbl">Neto</span></div>
          <div class="pm-market-stat"><span class="pm-market-val">{mk.get('winrate',0):.1f}%</span><span class="pm-market-lbl">Win%</span></div>
          <div class="pm-market-stat"><span class="pm-market-val">{mk.get('roi',0):+.1f}%</span><span class="pm-market-lbl">ROI</span></div>
          <div class="pm-market-picks">{mk.get('total',0)} picks</div>
        </div>"""

    market_section = f"""
    <div class="pm-section">
      <div class="pm-section-head"><span class="pm-section-title">Rendimiento por mercado</span></div>
      <div class="pm-market-grid">{market_cards if market_cards else '<p class="pm-empty">Sin datos aún</p>'}</div>
    </div>"""

    # ── Monthly breakdown ──────────────────────────────────────────────────────
    month_bars = ""
    if by_month:
        max_abs = max(abs(m.get("net_units", 0)) for m in by_month) or 1
        for m in by_month:
            net = m.get("net_units", 0)
            pct = min(abs(net) / max_abs * 100, 100)
            bar_color = "#22c55e" if net >= 0 else "#ef4444"
            sign = "+" if net >= 0 else ""
            month_bars += f"""
            <div class="pm-month-bar">
              <div class="pm-month-label">{html_lib.escape(m.get('month_label',''))}</div>
              <div class="pm-month-track">
                <div class="pm-month-fill" style="width:{pct:.1f}%;background:{bar_color};"></div>
              </div>
              <div class="pm-month-val" style="color:{bar_color};">{sign}{net:.2f}u</div>
              <div class="pm-month-picks">{m.get('winrate',0):.0f}% ({m.get('total',0)})</div>
            </div>"""

    monthly_section = f"""
    <div class="pm-section">
      <div class="pm-section-head"><span class="pm-section-title">Rendimiento mensual</span></div>
      <div class="pm-months">{month_bars if month_bars else '<p class="pm-empty">Sin datos aún</p>'}</div>
    </div>"""

    # ── Curve chart data ───────────────────────────────────────────────────────
    curve_json = json.dumps(curve) if curve else "[]"

    # ── Final HTML ─────────────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8"/>
  <title>Premium — AFTR</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <link rel="icon" type="image/png" href="/static/logo_aftr.png">
  <style>
    *{{box-sizing:border-box;margin:0;padding:0;}}
    body{{background:#070a10;color:#eaf2ff;font-family:system-ui,-apple-system,sans-serif;min-height:100vh;}}
    a{{color:#38bdf8;text-decoration:none;}} a:hover{{filter:brightness(1.15);}}

    .pm-root{{max-width:1100px;margin:0 auto;padding:20px 16px 60px;}}

    /* Header */
    .pm-header{{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;
                margin-bottom:28px;padding-bottom:18px;border-bottom:1px solid rgba(255,255,255,.08);}}
    .pm-brand{{display:flex;align-items:center;gap:10px;}}
    .pm-logo{{width:32px;height:32px;border-radius:8px;}}
    .pm-title{{font-size:1.15rem;font-weight:800;}}
    .pm-badge{{background:linear-gradient(135deg,#f59e0b,#d97706);color:#000;font-size:.7rem;
               font-weight:800;padding:3px 8px;border-radius:20px;letter-spacing:.05em;}}
    .pm-nav{{display:flex;gap:12px;align-items:center;flex-wrap:wrap;}}
    .pm-nav a{{font-size:.85rem;color:#94a3b8;}}
    .pm-nav a:hover{{color:#eaf2ff;}}

    /* Welcome */
    .pm-welcome{{background:linear-gradient(135deg,rgba(245,158,11,.08),rgba(217,119,6,.04));
                 border:1px solid rgba(245,158,11,.2);border-radius:14px;padding:18px 20px;
                 margin-bottom:20px;}}
    .pm-welcome-title{{font-size:1.1rem;font-weight:700;margin-bottom:4px;}}
    .pm-welcome-sub{{font-size:.85rem;color:#94a3b8;}}

    /* KPI grid */
    .pm-kpi-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin-bottom:20px;}}
    .pm-kpi{{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:14px 16px;}}
    .pm-kpi-val{{display:block;font-size:1.35rem;font-weight:800;}}
    .pm-kpi-lbl{{display:block;font-size:.72rem;color:#64748b;margin-top:3px;text-transform:uppercase;letter-spacing:.05em;}}

    /* Sections */
    .pm-section{{background:rgba(255,255,255,.02);border:1px solid rgba(255,255,255,.07);
                 border-radius:14px;padding:18px 20px;margin-bottom:16px;}}
    .pm-section-head{{display:flex;align-items:baseline;gap:10px;margin-bottom:14px;flex-wrap:wrap;}}
    .pm-section-title{{font-size:.75rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#475569;}}
    .pm-section-sub{{font-size:.75rem;color:#334155;}}
    .pm-empty{{color:#475569;font-size:.85rem;padding:8px 0;}}
    .pm-footnote{{font-size:.72rem;color:#334155;margin-top:10px;}}

    /* Tables */
    .pm-table-wrap{{overflow-x:auto;border-radius:8px;border:1px solid rgba(255,255,255,.06);}}
    .pm-table{{width:100%;border-collapse:collapse;font-size:13px;}}
    .pm-table thead tr{{border-bottom:1px solid rgba(255,255,255,.08);}}
    .pm-table th{{text-align:left;padding:9px 10px;font-size:11px;color:#475569;font-weight:600;
                  letter-spacing:.04em;text-transform:uppercase;white-space:nowrap;}}
    .pm-table td{{padding:9px 10px;border-bottom:1px solid rgba(255,255,255,.04);white-space:nowrap;}}
    .pm-table tbody tr:last-child td{{border-bottom:none;}}
    .pm-table tbody tr:hover td{{background:rgba(255,255,255,.02);}}
    .pm-match-cell{{max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}}
    .pm-league-tag{{background:rgba(56,189,248,.1);color:#38bdf8;border-radius:4px;padding:2px 6px;
                    font-size:.7rem;font-weight:700;}}

    /* Value badges */
    .pm-vbadge{{border-radius:4px;padding:2px 7px;font-size:.7rem;font-weight:700;}}
    .pm-vbadge--high{{background:rgba(245,158,11,.15);color:#f59e0b;}}
    .pm-vbadge--mid{{background:rgba(56,189,248,.12);color:#38bdf8;}}
    .pm-vbadge--low{{background:rgba(255,255,255,.06);color:#64748b;}}

    /* Form strip */
    .pm-form-strip{{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:4px;}}
    .pm-form-dot{{width:18px;height:18px;border-radius:50%;cursor:default;flex-shrink:0;}}

    /* Market grid */
    .pm-market-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px;}}
    .pm-market-card{{background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.07);
                     border-radius:10px;padding:12px 14px;}}
    .pm-market-name{{font-size:.8rem;font-weight:700;margin-bottom:8px;}}
    .pm-market-stat{{display:flex;align-items:baseline;gap:5px;margin-bottom:3px;}}
    .pm-market-val{{font-size:1.1rem;font-weight:800;}}
    .pm-market-lbl{{font-size:.7rem;color:#64748b;}}
    .pm-market-picks{{font-size:.72rem;color:#475569;margin-top:6px;}}

    /* Monthly bars */
    .pm-months{{display:flex;flex-direction:column;gap:10px;}}
    .pm-month-bar{{display:grid;grid-template-columns:80px 1fr 70px 80px;align-items:center;gap:10px;}}
    .pm-month-label{{font-size:.78rem;color:#94a3b8;text-align:right;}}
    .pm-month-track{{height:8px;background:rgba(255,255,255,.06);border-radius:4px;overflow:hidden;}}
    .pm-month-fill{{height:100%;border-radius:4px;transition:width .4s;}}
    .pm-month-val{{font-size:.82rem;font-weight:700;}}
    .pm-month-picks{{font-size:.72rem;color:#64748b;}}

    /* Curve chart */
    #pm-curve{{width:100%;height:180px;}}

    @media(max-width:600px){{
      .pm-kpi-grid{{grid-template-columns:repeat(2,1fr);}}
      .pm-month-bar{{grid-template-columns:60px 1fr 55px;}}
      .pm-month-picks{{display:none;}}
    }}
  </style>
</head>
<body>
<div class="pm-root">
  <header class="pm-header">
    <div class="pm-brand">
      <img src="/static/logo_aftr.png" class="pm-logo" alt="AFTR">
      <div>
        <div class="pm-title">AFTR <span class="pm-badge">PREMIUM</span></div>
      </div>
    </div>
    <nav class="pm-nav">
      <a href="/">← Inicio</a>
      <a href="/tracker">Tracker</a>
      <a href="/rendimiento">Rendimiento</a>
      <a href="/account">Mi cuenta</a>
    </nav>
  </header>

  <div class="pm-welcome">
    <div class="pm-welcome-title">Bienvenido, {html_lib.escape(username)} ⭐</div>
    <div class="pm-welcome-sub">Dashboard exclusivo premium — picks con edge positivo, stats avanzadas y análisis de valor.</div>
  </div>

  {kpi_html}
  {value_section}
  {form_section}
  {league_section}
  {market_section}
  {monthly_section}

  <div class="pm-section">
    <div class="pm-section-head">
      <span class="pm-section-title">Curva acumulada de unidades</span>
      <span class="pm-section-sub">Ganancia/pérdida neta acumulada histórica</span>
    </div>
    <canvas id="pm-curve"></canvas>
  </div>

</div>

<script>
(function() {{
  var curve = {curve_json};
  if (!curve || curve.length < 2) return;
  var canvas = document.getElementById('pm-curve');
  if (!canvas) return;
  var ctx = canvas.getContext('2d');
  var W = canvas.offsetWidth || 800;
  var H = 180;
  canvas.width = W;
  canvas.height = H;
  var vals = curve.map(function(d){{ return d.cumulative_units; }});
  var minV = Math.min.apply(null, vals);
  var maxV = Math.max.apply(null, vals);
  var range = maxV - minV || 1;
  var pad = {{ t:16, r:16, b:28, l:48 }};
  var cw = W - pad.l - pad.r;
  var ch = H - pad.t - pad.b;

  function xOf(i) {{ return pad.l + (i / (curve.length - 1)) * cw; }}
  function yOf(v) {{ return pad.t + ch - ((v - minV) / range) * ch; }}

  // Zero line
  var y0 = yOf(0);
  ctx.strokeStyle = 'rgba(255,255,255,0.08)';
  ctx.setLineDash([4,4]);
  ctx.beginPath(); ctx.moveTo(pad.l, y0); ctx.lineTo(pad.l + cw, y0); ctx.stroke();
  ctx.setLineDash([]);

  // Gradient fill
  var grad = ctx.createLinearGradient(0, pad.t, 0, pad.t + ch);
  var lastVal = vals[vals.length - 1];
  var lineColor = lastVal >= 0 ? '#22c55e' : '#ef4444';
  grad.addColorStop(0, lastVal >= 0 ? 'rgba(34,197,94,0.25)' : 'rgba(239,68,68,0.25)');
  grad.addColorStop(1, 'rgba(0,0,0,0)');

  ctx.beginPath();
  ctx.moveTo(xOf(0), yOf(vals[0]));
  for (var i=1; i<curve.length; i++) ctx.lineTo(xOf(i), yOf(vals[i]));
  ctx.lineTo(xOf(curve.length-1), pad.t + ch);
  ctx.lineTo(xOf(0), pad.t + ch);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  // Line
  ctx.beginPath();
  ctx.moveTo(xOf(0), yOf(vals[0]));
  for (var j=1; j<curve.length; j++) ctx.lineTo(xOf(j), yOf(vals[j]));
  ctx.strokeStyle = lineColor;
  ctx.lineWidth = 2;
  ctx.stroke();

  // Y labels
  ctx.fillStyle = '#475569';
  ctx.font = '11px system-ui';
  ctx.textAlign = 'right';
  [minV, 0, maxV].forEach(function(v) {{
    var y = yOf(v);
    ctx.fillText((v >= 0 ? '+' : '') + v.toFixed(1) + 'u', pad.l - 4, y + 4);
  }});
}})();
</script>
</body>
</html>"""
    return HTMLResponse(html)
