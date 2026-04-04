"""
/rendimiento — página de rendimiento histórico leída desde la DB.

Muestra:
  • KPIs globales (picks resueltos, win%, ROI, net units)
  • Curva acumulada (canvas, mismo drawSpark del dashboard)
  • Desglose por liga
  • Historial de picks resueltos (más nuevos primero)
"""
from __future__ import annotations

import html as html_lib
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import Request

from app.auth import get_user_id, get_user_by_id
from app.db import get_conn, put_conn
from app.models import get_active_plan
from app.ui_helpers import AUTH_BOOTSTRAP_SCRIPT
from config.settings import settings

logger = logging.getLogger("aftr.ui_rendimiento")

# ─── helpers ─────────────────────────────────────────────────────────────────

def _unit_profit(result: str, best_fair: float | None) -> float:
    r = (result or "").strip().upper()
    if r == "WIN":
        fair = float(best_fair or 0)
        return max(fair - 1.0, 1.0) if fair > 1 else 1.0
    if r == "LOSS":
        return -1.0
    return 0.0   # PUSH / unknown


def _fmt_result(result: str) -> str:
    r = (result or "").strip().upper()
    if r == "WIN":
        return '<span class="badge badge-win">WIN</span>'
    if r == "LOSS":
        return '<span class="badge badge-loss">LOSS</span>'
    if r == "PUSH":
        return '<span class="badge badge-push">PUSH</span>'
    return f'<span class="badge badge-muted">{html_lib.escape(result or "—")}</span>'


def _fmt_profit(delta: float) -> str:
    cls = "profit-pos" if delta > 0 else ("profit-neg" if delta < 0 else "profit-zero")
    sign = "+" if delta > 0 else ""
    return f'<span class="{cls}">{sign}{delta:.2f}u</span>'


def _fmt_date(utc_str: str | None) -> str:
    if not utc_str:
        return "—"
    try:
        dt = datetime.fromisoformat(str(utc_str).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%-d %b %Y")
    except Exception:
        return str(utc_str)[:10]


def _league_name(code: str) -> str:
    return settings.leagues.get(code, code)


# ─── DB query ────────────────────────────────────────────────────────────────

def _load_picks_from_db() -> list[dict]:
    """Returns all picks with result WIN/LOSS/PUSH joined with match info, ordered by utcDate asc."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                p.league,
                p.match_id,
                p.created_at,
                p.best_market,
                p.best_prob,
                p.best_fair,
                p.result,
                p.result_reason,
                m.home,
                m.away,
                m."utcDate",
                m.home_goals,
                m.away_goals
            FROM picks p
            LEFT JOIN matches m
                   ON m.league = p.league
                  AND m.match_id = p.match_id
            WHERE UPPER(p.result) IN ('WIN', 'LOSS', 'PUSH')
            ORDER BY m."utcDate" ASC NULLS LAST, p.created_at ASC
        """)
        rows = cur.fetchall()
        return [dict(r) for r in rows] if rows else []
    except Exception:
        logger.exception("_load_picks_from_db: query failed")
        return []
    finally:
        put_conn(conn)


def _count_pending() -> tuple[int, int]:
    """Returns (pending_count, total_count)."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM picks")
        total = int((cur.fetchone() or {}).get("n", 0))
        cur.execute("""
            SELECT COUNT(*) AS n FROM picks
            WHERE result IS NULL OR result = '' OR UPPER(result) = 'PENDING'
        """)
        pending = int((cur.fetchone() or {}).get("n", 0))
        return pending, total
    except Exception:
        return 0, 0
    finally:
        put_conn(conn)


# ─── stats calculation ───────────────────────────────────────────────────────

def _compute_stats(rows: list[dict]) -> dict:
    wins = losses = push = 0
    net = 0.0
    for r in rows:
        res = (r.get("result") or "").strip().upper()
        delta = _unit_profit(res, r.get("best_fair"))
        net += delta
        if res == "WIN":
            wins += 1
        elif res == "LOSS":
            losses += 1
        elif res == "PUSH":
            push += 1
    settled = wins + losses + push
    decided = wins + losses
    win_pct = round(wins / decided * 100, 1) if decided > 0 else None
    roi = round(net / settled * 100, 1) if settled > 0 else None
    return {
        "settled": settled,
        "wins": wins,
        "losses": losses,
        "push": push,
        "net": round(net, 2),
        "win_pct": win_pct,
        "roi": roi,
    }


def _build_chart_points(rows: list[dict]) -> list[dict]:
    """Cumulative profit points grouped by UTC date."""
    day_map: dict[str, float] = {}
    for r in rows:
        utc = r.get("utcDate") or r.get("created_at") or ""
        date_str = str(utc)[:10] if utc else "?"
        delta = _unit_profit(r.get("result", ""), r.get("best_fair"))
        day_map[date_str] = day_map.get(date_str, 0.0) + delta

    pts: list[dict] = []
    cum = 0.0
    for date_str in sorted(day_map.keys()):
        day_net = day_map[date_str]
        cum += day_net
        # label: "D MMM"
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            label = dt.strftime("%-d %b")
        except Exception:
            label = date_str
        pts.append({
            "date": date_str,
            "label": label,
            "v": round(cum, 3),
            "day": round(day_net, 3),
        })
    return pts


def _league_breakdown(rows: list[dict]) -> list[dict]:
    buckets: dict[str, dict] = {}
    for r in rows:
        lg = r.get("league") or "?"
        if lg not in buckets:
            buckets[lg] = {"league": lg, "wins": 0, "losses": 0, "push": 0, "net": 0.0}
        res = (r.get("result") or "").strip().upper()
        delta = _unit_profit(res, r.get("best_fair"))
        buckets[lg]["net"] += delta
        if res == "WIN":
            buckets[lg]["wins"] += 1
        elif res == "LOSS":
            buckets[lg]["losses"] += 1
        elif res == "PUSH":
            buckets[lg]["push"] += 1

    out = []
    for lg, b in buckets.items():
        decided = b["wins"] + b["losses"]
        settled = decided + b["push"]
        out.append({
            "league": lg,
            "name": _league_name(lg),
            "settled": settled,
            "wins": b["wins"],
            "losses": b["losses"],
            "push": b["push"],
            "net": round(b["net"], 2),
            "win_pct": round(b["wins"] / decided * 100, 1) if decided > 0 else None,
            "roi": round(b["net"] / settled * 100, 1) if settled > 0 else None,
        })
    out.sort(key=lambda x: x["net"], reverse=True)
    return out


# ─── HTML builder ────────────────────────────────────────────────────────────

def build_rendimiento_page(request: Request) -> str:
    rows = _load_picks_from_db()
    pending, total_in_db = _count_pending()
    stats = _compute_stats(rows)
    chart_pts = _build_chart_points(rows)
    league_rows = _league_breakdown(rows)

    # ── KPI values ───
    settled = stats["settled"]
    wins = stats["wins"]
    losses = stats["losses"]
    push = stats["push"]
    net = stats["net"]
    win_pct_str = f"{stats['win_pct']:.1f}%" if stats["win_pct"] is not None else "—"
    roi_str = (f"+{stats['roi']:.1f}%" if (stats["roi"] or 0) > 0
               else (f"{stats['roi']:.1f}%" if stats["roi"] is not None else "—"))
    net_str = (f"+{net:.2f}u" if net > 0 else f"{net:.2f}u") if settled > 0 else "—"

    roi_color = ("kpi-pos" if (stats["roi"] or 0) > 0
                 else ("kpi-neg" if (stats["roi"] or 0) < 0 else ""))

    # ── Chart HTML ───
    if chart_pts:
        chart_json = json.dumps(chart_pts).replace("</script", "<\\/script")
        chart_html = (
            '<div class="rendimiento-chart-wrap">'
            '<canvas id="roiSpark" aria-hidden="true"></canvas>'
            '<div id="roiTip" class="roi-tip" style="display:none;"></div>'
            f'<script type="application/json" id="aftr-roi-chart-data">{chart_json}</script>'
            '</div>'
        )
    else:
        chart_html = (
            '<div class="perf-chart-empty-state" role="status">'
            '<p class="perf-chart-empty-title">Sin datos suficientes todavía</p>'
            '<p class="perf-chart-empty-sub muted">La curva aparece cuando haya picks resueltos.</p>'
            '</div>'
        )

    # ── League breakdown table ───
    if league_rows:
        league_rows_html = ""
        for lb in league_rows:
            roi_v = lb["roi"]
            roi_cell = (f'+{roi_v:.1f}%' if (roi_v or 0) > 0 else (f'{roi_v:.1f}%' if roi_v is not None else '—'))
            roi_cls = "profit-pos" if (roi_v or 0) > 0 else ("profit-neg" if (roi_v or 0) < 0 else "")
            net_v = lb["net"]
            net_cell = f'+{net_v:.2f}u' if net_v > 0 else f'{net_v:.2f}u'
            net_cls = "profit-pos" if net_v > 0 else ("profit-neg" if net_v < 0 else "")
            wp = lb["win_pct"]
            wp_str = f'{wp:.1f}%' if wp is not None else '—'
            league_rows_html += f"""
          <tr>
            <td><span class="league-badge">{html_lib.escape(lb['league'])}</span> {html_lib.escape(lb['name'])}</td>
            <td class="tnum">{lb['settled']}</td>
            <td class="tnum">{lb['wins']}W / {lb['losses']}L</td>
            <td class="tnum">{wp_str}</td>
            <td class="tnum"><span class="{roi_cls}">{roi_cell}</span></td>
            <td class="tnum"><span class="{net_cls}">{net_cell}</span></td>
          </tr>"""
        league_section = f"""
        <section class="rendimiento-section">
          <h2 class="rendimiento-section-title">Desglose por liga</h2>
          <div class="rendimiento-table-wrap">
            <table class="rendimiento-table">
              <thead>
                <tr>
                  <th>Liga</th><th>Picks</th><th>W / L</th><th>Win%</th><th>ROI</th><th>Neto</th>
                </tr>
              </thead>
              <tbody>{league_rows_html}</tbody>
            </table>
          </div>
        </section>"""
    else:
        league_section = ""

    # ── Pick history list ───
    if rows:
        picks_html = ""
        for r in reversed(rows):  # newest first
            res = (r.get("result") or "").strip().upper()
            delta = _unit_profit(res, r.get("best_fair"))
            home = html_lib.escape(r.get("home") or "?")
            away = html_lib.escape(r.get("away") or "?")
            market = html_lib.escape(r.get("best_market") or "—")
            league_code = html_lib.escape(r.get("league") or "?")
            date_str = _fmt_date(r.get("utcDate"))
            score_h = r.get("home_goals")
            score_a = r.get("away_goals")
            score = f"<span class='muted'>{score_h}–{score_a}</span>" if score_h is not None else ""
            fair_v = r.get("best_fair")
            fair_str = f"@{float(fair_v):.2f}" if fair_v else ""

            picks_html += f"""
          <div class="pick-history-row">
            <div class="pick-history-date muted">{date_str}</div>
            <div class="pick-history-match">
              <span class="league-badge-sm">{league_code}</span>
              {home} vs {away} {score}
            </div>
            <div class="pick-history-market">{market} <span class="muted">{fair_str}</span></div>
            <div class="pick-history-result">{_fmt_result(res)}</div>
            <div class="pick-history-profit">{_fmt_profit(delta)}</div>
          </div>"""
        history_section = f"""
        <section class="rendimiento-section">
          <h2 class="rendimiento-section-title">Historial de picks resueltos</h2>
          <div class="pick-history-list">{picks_html}</div>
        </section>"""
    else:
        history_section = """
        <section class="rendimiento-section">
          <p class="muted" style="text-align:center;padding:24px 0;">
            No hay picks resueltos todavía.
          </p>
        </section>"""

    pending_note = (
        f'<p class="muted" style="font-size:0.82rem;margin-top:4px;">'
        f'{pending} pick{"s" if pending != 1 else ""} pendiente{"s" if pending != 1 else ""} '
        f'de resolución · {total_in_db} en total en la DB.</p>'
    )

    # ── draw chart JS (same as dashboard) ───
    chart_js = """
<script>
(function(){
  function drawSpark(canvasId, points){
    var c = document.getElementById(canvasId);
    var tip = document.getElementById("roiTip");
    if(!c || !points || !points.length) return;
    var ctx = c.getContext('2d');
    var parent = c.parentElement;
    var w = Math.max(320, parent ? parent.clientWidth : c.width);
    var h = 180;
    c.width = w; c.height = h;
    var vals = points.map(function(p){ return Number(p.v||0); });
    var minV = Math.min.apply(null, vals);
    var maxV = Math.max.apply(null, vals);
    if(minV === maxV){ minV -= 1; maxV += 1; }
    var padX = 18, padY = 22;
    var innerW = w - padX*2, innerH = h - padY*2;
    function xAt(i){ return points.length===1 ? padX+innerW/2 : padX+(innerW*(i/(points.length-1))); }
    function yAt(v){ return padY + innerH - ((v - minV)/(maxV - minV))*innerH; }
    var pathPts = points.map(function(p,i){ return {x:xAt(i), y:yAt(Number(p.v||0))}; });
    function clamp(n,a,b){ return Math.max(a, Math.min(b, n)); }

    function redraw(hoverIndex){
      ctx.clearRect(0,0,w,h);
      // grid
      ctx.globalAlpha=0.28; ctx.strokeStyle="rgba(255,255,255,0.18)"; ctx.lineWidth=1;
      for(var i=0;i<3;i++){ var y=padY+(innerH*(i/2)); ctx.beginPath(); ctx.moveTo(padX,y); ctx.lineTo(padX+innerW,y); ctx.stroke(); }
      ctx.globalAlpha=1;
      // zero line
      var y0=yAt(0);
      ctx.globalAlpha=0.55; ctx.strokeStyle="rgba(255,255,255,0.25)"; ctx.setLineDash([6,6]);
      ctx.beginPath(); ctx.moveTo(padX,y0); ctx.lineTo(padX+innerW,y0); ctx.stroke();
      ctx.setLineDash([]); ctx.globalAlpha=1;
      // fill
      ctx.beginPath();
      pathPts.forEach(function(pt,i){ i===0 ? ctx.moveTo(pt.x,pt.y) : ctx.lineTo(pt.x,pt.y); });
      ctx.lineTo(pathPts[pathPts.length-1].x, padY+innerH);
      ctx.lineTo(pathPts[0].x, padY+innerH);
      ctx.closePath();
      var grad=ctx.createLinearGradient(0,padY,0,padY+innerH);
      grad.addColorStop(0,"rgba(120,170,255,0.22)"); grad.addColorStop(1,"rgba(120,170,255,0.00)");
      ctx.fillStyle=grad; ctx.fill();
      // line
      ctx.lineWidth=3; ctx.strokeStyle="rgba(120,170,255,0.95)";
      ctx.beginPath();
      pathPts.forEach(function(pt,i){ i===0 ? ctx.moveTo(pt.x,pt.y) : ctx.lineTo(pt.x,pt.y); });
      ctx.stroke();
      // dots
      pathPts.forEach(function(pt,i){
        var day=Number(points[i].day||0);
        ctx.fillStyle = day>0 ? "rgba(34,197,94,0.95)" : (day<0 ? "rgba(239,68,68,0.95)" : "rgba(255,255,255,0.85)");
        ctx.beginPath(); ctx.arc(pt.x,pt.y,3.2,0,Math.PI*2); ctx.fill();
      });
      // label
      var last=points[points.length-1], lv=Number(last.v||0), ld=Number(last.day||0);
      ctx.fillStyle="rgba(255,255,255,0.90)";
      ctx.font="12px system-ui,-apple-system,Segoe UI,Roboto";
      ctx.fillText("Acum: "+(lv>=0?"+":"")+lv.toFixed(2)+"u  |  Último día: "+(ld>=0?"+":"")+ld.toFixed(2)+"u", padX, 14);
      // hover
      if(hoverIndex!=null && hoverIndex>=0){
        var pt=pathPts[hoverIndex];
        ctx.globalAlpha=0.55; ctx.strokeStyle="rgba(255,255,255,0.20)"; ctx.lineWidth=1;
        ctx.beginPath(); ctx.moveTo(pt.x,padY); ctx.lineTo(pt.x,padY+innerH); ctx.stroke();
        ctx.globalAlpha=1;
        ctx.fillStyle="rgba(120,170,255,1)";
        ctx.beginPath(); ctx.arc(pt.x,pt.y,6,0,Math.PI*2); ctx.fill();
        ctx.fillStyle="rgba(255,255,255,0.95)";
        ctx.beginPath(); ctx.arc(pt.x,pt.y,3,0,Math.PI*2); ctx.fill();
      }
    }

    function nearestIndex(mx){
      var best=0,bestDist=Infinity;
      for(var i=0;i<pathPts.length;i++){ var d=Math.abs(pathPts[i].x-mx); if(d<bestDist){bestDist=d;best=i;} }
      return best;
    }
    function showTip(i,cx,cy){
      if(!tip) return;
      var p=points[i];
      tip.innerHTML="<div><b>"+(p.label||"Día")+"</b></div>"
        +"<div class='muted'>Neto: "+((Number(p.day||0)>=0?"+":"")+Number(p.day||0).toFixed(2))+"u</div>"
        +"<div>Acum: "+((Number(p.v||0)>=0?"+":"")+Number(p.v||0).toFixed(2))+"u</div>";
      tip.style.display="block";
      var rect=c.getBoundingClientRect();
      var x=cx-rect.left, y=cy-rect.top;
      tip.style.left=clamp(x+12,8,rect.width-220)+"px";
      tip.style.top=clamp(y-10,8,rect.height-70)+"px";
    }
    function hideTip(){ if(tip) tip.style.display="none"; redraw(-1); }

    redraw(-1);
    c.onmousemove=function(e){
      var rect=c.getBoundingClientRect(), mx=e.clientX-rect.left;
      if(mx<padX||mx>(padX+innerW)){ hideTip(); return; }
      var i=nearestIndex(mx); redraw(i); showTip(i,e.clientX,e.clientY);
    };
    c.onmouseleave=hideTip;
  }

  function boot(){
    var el = document.getElementById("aftr-roi-chart-data");
    if(!el) return;
    try { var pts = JSON.parse(el.textContent || el.innerHTML || "[]"); drawSpark("roiSpark", pts); }
    catch(e){}
    window.addEventListener("resize", function(){
      var el2 = document.getElementById("aftr-roi-chart-data");
      if(!el2) return;
      try { drawSpark("roiSpark", JSON.parse(el2.textContent||el2.innerHTML||"[]")); } catch(e){}
    });
  }
  if(document.readyState==="loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
</script>"""

    body = f"""
<div class="rendimiento-page">
  <div class="rendimiento-header">
    <a href="/" class="back-link">← Inicio</a>
    <h1 class="rendimiento-title">Rendimiento histórico</h1>
    <p class="rendimiento-sub muted">Todas las picks resueltas desde el inicio de AFTR.</p>
    {pending_note}
  </div>

  <!-- KPIs -->
  <div class="rendimiento-kpis">
    <div class="rendimiento-kpi">
      <span class="rendimiento-kpi-label">Picks resueltos</span>
      <strong class="rendimiento-kpi-val">{settled}</strong>
    </div>
    <div class="rendimiento-kpi">
      <span class="rendimiento-kpi-label">Victoria</span>
      <strong class="rendimiento-kpi-val">{wins}W / {losses}L</strong>
    </div>
    <div class="rendimiento-kpi">
      <span class="rendimiento-kpi-label">Win%</span>
      <strong class="rendimiento-kpi-val">{win_pct_str}</strong>
    </div>
    <div class="rendimiento-kpi">
      <span class="rendimiento-kpi-label">ROI</span>
      <strong class="rendimiento-kpi-val {roi_color}">{roi_str}</strong>
    </div>
    <div class="rendimiento-kpi">
      <span class="rendimiento-kpi-label">Neto</span>
      <strong class="rendimiento-kpi-val">{net_str}</strong>
    </div>
  </div>

  <!-- Chart -->
  <section class="rendimiento-section rendimiento-chart-section">
    <h2 class="rendimiento-section-title">Curva acumulada</h2>
    <div class="perf-panel-glass rendimiento-chart-glass">
      <div style="position:relative;">
        {chart_html}
      </div>
    </div>
  </section>

  {league_section}

  {history_section}
</div>
{chart_js}
"""

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8"/>
  <title>Rendimiento · AFTR</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="stylesheet" href="/static/style.css?v=24">
  <link rel="icon" type="image/png" href="/static/logo_aftr.png">
  <style>
    .rendimiento-page {{ max-width: 900px; margin: 0 auto; padding: 24px 16px 64px; }}
    .rendimiento-header {{ margin-bottom: 28px; }}
    .rendimiento-title {{ font-size: 1.6rem; font-weight: 700; margin: 6px 0 4px; }}
    .rendimiento-sub {{ margin: 0; }}
    .back-link {{ color: var(--accent, #7ab); text-decoration: none; font-size: 0.9rem; }}
    .back-link:hover {{ text-decoration: underline; }}

    .rendimiento-kpis {{
      display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 32px;
    }}
    .rendimiento-kpi {{
      flex: 1 1 140px;
      background: rgba(255,255,255,0.05);
      border: 1px solid rgba(255,255,255,0.09);
      border-radius: 12px;
      padding: 16px 18px;
      display: flex; flex-direction: column; gap: 4px;
    }}
    .rendimiento-kpi-label {{ font-size: 0.75rem; text-transform: uppercase; letter-spacing: .06em; opacity: .6; }}
    .rendimiento-kpi-val {{ font-size: 1.4rem; font-weight: 700; }}
    .kpi-pos {{ color: #22c55e; }}
    .kpi-neg {{ color: #ef4444; }}

    .rendimiento-section {{ margin-bottom: 36px; }}
    .rendimiento-section-title {{ font-size: 1rem; font-weight: 600; margin: 0 0 14px; opacity: .85; }}

    .rendimiento-chart-glass {{
      border-radius: 14px;
      padding: 16px;
      position: relative;
    }}
    .rendimiento-chart-wrap {{ position: relative; }}
    .rendimiento-chart-wrap canvas {{ width: 100% !important; border-radius: 8px; }}

    .rendimiento-table-wrap {{ overflow-x: auto; }}
    .rendimiento-table {{
      width: 100%; border-collapse: collapse; font-size: 0.88rem;
    }}
    .rendimiento-table th {{
      text-align: left; padding: 8px 10px; font-weight: 600;
      border-bottom: 1px solid rgba(255,255,255,0.12); opacity: .7; white-space: nowrap;
    }}
    .rendimiento-table td {{
      padding: 8px 10px;
      border-bottom: 1px solid rgba(255,255,255,0.06);
    }}
    .rendimiento-table tr:hover td {{ background: rgba(255,255,255,0.04); }}
    .tnum {{ text-align: right; font-variant-numeric: tabular-nums; }}

    .league-badge {{
      display: inline-block; padding: 1px 7px; border-radius: 5px;
      background: rgba(120,170,255,0.18); font-size: 0.74rem; font-weight: 700;
      letter-spacing: .04em; margin-right: 4px;
    }}
    .league-badge-sm {{
      display: inline-block; padding: 1px 5px; border-radius: 4px;
      background: rgba(120,170,255,0.15); font-size: 0.7rem; font-weight: 700;
      letter-spacing: .04em; margin-right: 5px;
    }}

    .pick-history-list {{ display: flex; flex-direction: column; gap: 0; }}
    .pick-history-row {{
      display: grid;
      grid-template-columns: 80px 1fr 1fr 64px 72px;
      align-items: center; gap: 8px;
      padding: 10px 4px;
      border-bottom: 1px solid rgba(255,255,255,0.06);
      font-size: 0.86rem;
    }}
    .pick-history-row:hover {{ background: rgba(255,255,255,0.03); }}
    .pick-history-date {{ font-size: 0.78rem; opacity: .6; }}
    .pick-history-result {{ text-align: center; }}
    .pick-history-profit {{ text-align: right; font-variant-numeric: tabular-nums; font-weight: 600; }}

    .badge {{ display: inline-block; padding: 2px 8px; border-radius: 5px; font-size: 0.75rem; font-weight: 700; }}
    .badge-win  {{ background: rgba(34,197,94,0.22);  color: #4ade80; }}
    .badge-loss {{ background: rgba(239,68,68,0.22);  color: #f87171; }}
    .badge-push {{ background: rgba(234,179,8,0.22);  color: #facc15; }}
    .badge-muted {{ background: rgba(255,255,255,0.1); opacity: .7; }}

    .profit-pos  {{ color: #4ade80; }}
    .profit-neg  {{ color: #f87171; }}
    .profit-zero {{ opacity: .5; }}

    @media (max-width: 600px) {{
      .pick-history-row {{
        grid-template-columns: 70px 1fr 56px 60px;
      }}
      .pick-history-market {{ display: none; }}
    }}
  </style>
</head>
<body>
{body}
{AUTH_BOOTSTRAP_SCRIPT}
</body>
</html>"""
