import html as html_lib
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from config.settings import settings
from data.cache import read_json
from app.routes.matches import group_matches_by_day
from core.poisson import market_priority

router = APIRouter()


def _team_with_crest(crest: str | None, name: str) -> str:
    """Fragmento HTML: escudo (28px, lazy) + nombre; si crest es None no se renderiza img."""
    safe_name = html_lib.escape(name or "")
    if crest and crest.strip():
        safe_src = html_lib.escape(crest.strip())
        return f'<span class="team-row"><img src="{safe_src}" alt="" class="crest" loading="lazy" width="28" height="28"/><span class="team-name">{safe_name}</span></span>'
    return f'<span class="team-row"><span class="team-name">{safe_name}</span></span>'


def _safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


def _pill_bar(active: str) -> str:
    pills = []
    for code, name in settings.leagues.items():
        cls = "active" if code == active else ""
        pills.append(f'<a class="pill {cls}" href="/?league={code}">{name}</a>')
    return '<div class="leaguebar">' + "".join(pills) + "</div>"


def top_picks_with_variety(picks: list, top_n: int = 10, max_repeats_per_market: int = 3):
    used_count = {}
    chosen = []

    pool = []
    for p in picks:
        cands = p.get("candidates") or []
        # Ordenar por prioridad (1X/X2 > HOME/AWAY > Over/Under > BTTS > DRAW) y luego por prob desc
        cands = sorted(
            cands,
            key=lambda c: (market_priority(c.get("market")), -_safe_float(c.get("prob"))),
        )
        if cands:
            pool.append((p, cands))

    # Orden del pool por mejor prob del primer candidato (ya en orden de prioridad)
    pool.sort(key=lambda item: _safe_float(item[1][0].get("prob")), reverse=True)

    for p, cands in pool:
        best = None
        for c in cands:
            market = (c.get("market") or "").strip()
            if not market:
                continue
            if used_count.get(market, 0) < max_repeats_per_market:
                best = c
                break

        if best is None:
            best = cands[0]

        market_name = (best.get("market") or "").strip()
        used_count[market_name] = used_count.get(market_name, 0) + 1

        chosen.append((p, best))
        if len(chosen) >= top_n:
            break

    return chosen


@router.get("/ui", response_class=HTMLResponse, include_in_schema=False)
def ui_same(request: Request, league: str = Query(settings.default_league)):
    return dashboard(request, league)


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, league: str = Query(settings.default_league)):
    league = league if settings.is_valid_league(league) else settings.default_league

    matches = read_json(f"daily_matches_{league}.json")   # lista de partidos
    picks = read_json(f"daily_picks_{league}.json")       # lista de picks con candidates

    # Top 10 “selecciones” (con variedad)
    selections = top_picks_with_variety(picks, top_n=10, max_repeats_per_market=3)

    page_html = f"""
    <html>
    <head>
      <meta charset="utf-8"/>
      <title>AFTR Pick</title>
      <link rel="stylesheet" href="/static/style.css">
    </head>
    <body>
      <div class="top">
        <div class="brand">AFTR Pick Local</div>
        <div class="links">
          <a href="/?league={league}">Dashboard</a>
          <a href="/api/matches?league={league}" target="_blank">JSON Matches</a>
          <a href="/api/picks?league={league}" target="_blank">JSON Picks</a>
        </div>
      </div>

      <div id="summary-bar" class="summary-bar" data-league="{league}">
        <div class="kpi-grid">
          <div class="kpi-card"><span class="kpi-label">ROI</span><span class="kpi-value" id="kpi-roi">—</span></div>
          <div class="kpi-card"><span class="kpi-label">Total picks</span><span class="kpi-value" id="kpi-total">—</span></div>
          <div class="kpi-card"><span class="kpi-label">Wins</span><span class="kpi-value" id="kpi-wins">—</span></div>
          <div class="kpi-card"><span class="kpi-label">Losses</span><span class="kpi-value" id="kpi-losses">—</span></div>
          <div class="kpi-card"><span class="kpi-label">Pending</span><span class="kpi-value" id="kpi-pending">—</span></div>
          <div class="kpi-card"><span class="kpi-label">Profit neto</span><span class="kpi-value" id="kpi-net">—</span></div>
        </div>
      </div>

      {_pill_bar(league)}

      <h2>📅 Próximos partidos</h2>
    """

    # Partidos agrupados por día (Hoy, Mañana, Lunes, ...)
    days_with_matches = group_matches_by_day(matches, days=7)
    if not days_with_matches:
        if not matches:
            page_html += "<p class='muted'>No hay matches JSON para esta liga (todavía).</p>"
        else:
            page_html += "<p class='muted'>No hay partidos en los próximos 7 días.</p>"
    else:
        for day_block in days_with_matches:
            label = day_block["label"]
            day_matches = day_block["matches"]
            count = len(day_matches)
            page_html += f"""
      <h3 class="day-title">{label} ({count} partido{"s" if count != 1 else ""})</h3>
      <div class="grid">
            """
            for m in day_matches:
                home_part = _team_with_crest(m.get("home_crest"), m.get("home", ""))
                away_part = _team_with_crest(m.get("away_crest"), m.get("away", ""))
                page_html += f"""
            <div class="card">
              <div class="row">{home_part} <span class="vs">vs</span> {away_part}</div>
              <div class="meta">{html_lib.escape(m.get('utcDate',''))}</div>
            </div>
            """
            page_html += """
      </div>
            """

    page_html += """

      <h2 style="margin-top:20px;">⭐ Selecciones (Top 10)</h2>
      <div class="grid">
    """

    # ✅ ACÁ van las selecciones top10 (con variedad): best destacado + hasta 3 candidatos
    if not selections:
        page_html += "<p class='muted'>No hay picks JSON para esta liga (todavía).</p>"
    else:
        for p, best in selections:
            home_part = _team_with_crest(p.get("home_crest"), p.get("home", ""))
            away_part = _team_with_crest(p.get("away_crest"), p.get("away", ""))
            best_market = best.get("market") or p.get("best_market") or "—"
            best_prob_pct = round(_safe_float(best.get("prob", 0)) * 100, 1)
            best_fair = best.get("fair") or p.get("best_fair")
            best_fair_str = f" • {best_fair}" if best_fair is not None else ""

            result = (p.get("result") or "").strip().upper() or "PENDING"
            if result not in ("WIN", "LOSS", "PUSH", "PENDING"):
                result = "PENDING"
            card_class = "card"
            if result == "WIN":
                card_class = "card pick-win"
            elif result == "LOSS":
                card_class = "card pick-loss"
            elif result == "PUSH":
                card_class = "card pick-push"

            badge_html = f'<span class="pick-badge">{html_lib.escape(result)}</span>'

            candidates = p.get("candidates") or []
            top3 = candidates[:3]

            cand_lines = []
            for c in top3:
                mkt = html_lib.escape((c.get("market") or "—"))
                prob_pct = round(_safe_float(c.get("prob", 0)) * 100, 1)
                fair = c.get("fair")
                fair_str = f" • {fair}" if fair is not None else ""
                cand_lines.append(f"<div class=\"cand-line\">{mkt} — {prob_pct}%{fair_str}</div>")
            cand_block = "\n                ".join(cand_lines) if cand_lines else "<div class='cand-line muted'>Sin candidatos</div>"

            page_html += f"""
            <div class="{card_class}">
              <div class="row">{home_part} <span class="vs">vs</span> {away_part}</div>
              <div class="meta">{html_lib.escape(p.get('utcDate',''))}</div>
              <div class="pick pick-best">{html_lib.escape(best_market)}{badge_html} — {best_prob_pct}%{best_fair_str}</div>
              <div class="candidates">
                {cand_block}
              </div>
            </div>
            """

    page_html += f"""
      </div>
      <script>
        (function() {{
          var bar = document.getElementById('summary-bar');
          if (!bar) return;
          var league = bar.getAttribute('data-league') || '';
          fetch('/api/stats/summary?league=' + encodeURIComponent(league))
            .then(function(r) {{ return r.ok ? r.json() : null; }})
            .then(function(d) {{
              if (!d) return;
              var settled = (d.wins || 0) + (d.losses || 0) + (d.push || 0);
              var roiEl = document.getElementById('kpi-roi');
              roiEl.textContent = settled > 0 && d.roi != null ? d.roi + '%' : '—';
              document.getElementById('kpi-total').textContent = d.total_picks != null ? d.total_picks : '—';
              document.getElementById('kpi-wins').textContent = d.wins != null ? d.wins : '—';
              document.getElementById('kpi-losses').textContent = d.losses != null ? d.losses : '—';
              document.getElementById('kpi-pending').textContent = d.pending != null ? d.pending : '—';
              var netEl = document.getElementById('kpi-net');
              var netCard = netEl && netEl.closest('.kpi-card');
              if (d.net_units != null) {{
                var n = Number(d.net_units);
                netEl.textContent = (n >= 0 ? '+' : '') + n.toFixed(2);
                if (netCard) {{ netCard.classList.add(n >= 0 ? 'pos' : 'neg'); }}
              }} else {{
                netEl.textContent = '—';
              }}
            }})
            .catch(function() {{}});
        }})();
      </script>
    </body>
    </html>
    """
    return page_html