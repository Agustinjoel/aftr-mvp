import html as html_lib
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from config.settings import settings
from data.cache import read_json
from app.routes.matches import group_matches_by_day
from core.poisson import market_priority

router = APIRouter()


def _team_with_crest(crest: str | None, name: str) -> str:
    safe_name = html_lib.escape(name or "")
    if crest and isinstance(crest, str) and crest.strip():
        safe_src = html_lib.escape(crest.strip())
        return (
            f'<span class="team-row">'
            f'<img src="{safe_src}" alt="" class="crest" loading="lazy" width="28" height="28"/>'
            f'<span class="team-name">{safe_name}</span>'
            f"</span>"
        )
    return f'<span class="team-row"><span class="team-name">{safe_name}</span></span>'


def _safe_float(x, default=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return float(default)


def _safe_int(x, default=None):
    try:
        if x is None:
            return default
        return int(x)
    except Exception:
        return default


def _parse_utcdate_str(s) -> datetime:
    """Parse utcDate robusto: soporta '...Z' y fallback a now(UTC)."""
    try:
        if isinstance(s, str) and s:
            if s.endswith("Z"):
                return datetime.fromisoformat(s.replace("Z", "+00:00"))
            return datetime.fromisoformat(s)
    except Exception:
        pass
    return datetime.now(timezone.utc)


def _pill_bar(active: str) -> str:
    pills = []
    for code, name in settings.leagues.items():
        cls = "active" if code == active else ""
        pills.append(
            f'<a class="pill {cls}" href="/?league={code}">{html_lib.escape(name)}</a>'
        )
    return '<div class="leaguebar">' + "".join(pills) + "</div>"


def top_picks_with_variety(picks: list, top_n: int = 10, max_repeats_per_market: int = 3):
    used_count: dict[str, int] = {}
    chosen: list[tuple[dict, dict]] = []

    pool: list[tuple[dict, list[dict]]] = []
    for p in picks or []:
        if not isinstance(p, dict):
            continue
        cands = p.get("candidates") or []
        if not isinstance(cands, list):
            cands = []

        # prioridad por mercado + prob desc
        cands = sorted(
            [c for c in cands if isinstance(c, dict)],
            key=lambda c: (market_priority(c.get("market")), -_safe_float(c.get("prob"))),
        )
        if cands:
            pool.append((p, cands))

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


def _result_norm(p: dict) -> str:
    r = (p.get("result") or "").strip().upper()
    return r if r in ("WIN", "LOSS", "PUSH", "PENDING") else "PENDING"


def _label_for_date(d: datetime.date, today: datetime.date) -> str:
    if d == today:
        return "Hoy"
    if d == today - timedelta(days=1):
        return "Ayer"
    return d.isoformat()


def group_picks_recent_by_day_desc(items: list[dict], days: int = 7):
    """
    Agrupa picks SETTLED por día, mirando hacia atrás por horas (now - days),
    orden DESC. Esto evita el bug de corte por fecha/UTC.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max(1, int(days)))
    today = now.date()

    buckets: dict = {}
    for p in items or []:
        if not isinstance(p, dict):
            continue
        dt = _parse_utcdate_str(p.get("utcDate"))
        if dt < cutoff:
            continue
        d = dt.date()
        buckets.setdefault(d, []).append(p)

    out = []
    for d in sorted(buckets.keys(), reverse=True):
        out.append({"label": _label_for_date(d, today), "matches": buckets[d]})
    return out


def _render_pick_card(p: dict, best: dict | None = None) -> str:
    home_part = _team_with_crest(p.get("home_crest"), p.get("home", ""))
    away_part = _team_with_crest(p.get("away_crest"), p.get("away", ""))

    best_market = (best or {}).get("market") or p.get("best_market") or "—"

    best_prob = (best or {}).get("prob")
    if best_prob is None:
        best_prob = p.get("best_prob")
    best_prob_pct = round(_safe_float(best_prob, 0) * 100, 1)

    best_fair = (best or {}).get("fair")
    if best_fair is None:
        best_fair = p.get("best_fair")
    best_fair_str = f" • {best_fair}" if best_fair is not None else ""

    result = _result_norm(p)

    card_class = "card"
    if result == "WIN":
        card_class = "card pick-win"
    elif result == "LOSS":
        card_class = "card pick-loss"
    elif result == "PUSH":
        card_class = "card pick-push"

    badge_html = f'<span class="pick-badge">{html_lib.escape(result)}</span>'

    model = (p.get("model") or "A").strip().upper()
    model_badge = f'<span class="model-badge">Model {html_lib.escape(model)}</span>'

    conf_i = _safe_int(p.get("confidence"))
    conf_badge = f'<span class="conf-badge">Conf {conf_i}/10</span>' if conf_i is not None else ""

    edge_badge = ""
    try:
        edge = p.get("edge")
        if edge is not None:
            edge_f = float(edge)
            edge_badge = f'<span class="edge-badge">Edge {edge_f:.3f}</span>'
    except Exception:
        edge_badge = ""

    candidates = p.get("candidates") or []
    if not isinstance(candidates, list):
        candidates = []
    top3 = [c for c in candidates if isinstance(c, dict)][:3]

    cand_lines = []
    for c in top3:
        mkt = html_lib.escape((c.get("market") or "—"))
        prob_pct = round(_safe_float(c.get("prob", 0)) * 100, 1)
        fair = c.get("fair")
        fair_str = f" • {fair}" if fair is not None else ""
        cand_lines.append(f'<div class="cand-line">{mkt} — {prob_pct}%{fair_str}</div>')

    cand_block = "\n".join(cand_lines) if cand_lines else "<div class='cand-line muted'>Sin candidatos</div>"

    return f"""
    <div class="{card_class}">
      <div class="row">{home_part} <span class="vs">vs</span> {away_part}</div>
      <div class="meta">{html_lib.escape(str(p.get('utcDate','')))}</div>
      <div class="pick pick-best">
        <span class="pick-main">{html_lib.escape(best_market)}</span>
        {badge_html}{model_badge}{conf_badge}{edge_badge}
        <span class="pick-prob">— {best_prob_pct}%{best_fair_str}</span>
      </div>
      <div class="candidates">
        {cand_block}
      </div>
    </div>
    """


@router.get("/ui", response_class=HTMLResponse, include_in_schema=False)
def ui_same(request: Request, league: str = Query(settings.default_league)):
    return dashboard(request, league)


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, league: str = Query(settings.default_league)):
    league = league if settings.is_valid_league(league) else settings.default_league

    matches = read_json(f"daily_matches_{league}.json") or []
    picks = read_json(f"daily_picks_{league}.json") or []
    picks = [p for p in picks if isinstance(p, dict)]

    upcoming_picks = [p for p in picks if _result_norm(p) == "PENDING"]
    settled_picks = [p for p in picks if _result_norm(p) in ("WIN", "LOSS", "PUSH")]

    def _model_rank(p: dict) -> int:
        return 0 if (p.get("model") or "").strip().upper() == "B" else 1

    upcoming_sorted = sorted(
        upcoming_picks,
        key=lambda p: (_model_rank(p), -_safe_float(p.get("best_prob"))),
    )
    selections = top_picks_with_variety(upcoming_sorted, top_n=10, max_repeats_per_market=3)

    # settled desc por datetime real
    settled_sorted = sorted(settled_picks, key=lambda p: _parse_utcdate_str(p.get("utcDate")), reverse=True)
    settled_groups = group_picks_recent_by_day_desc(settled_sorted, days=7)

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
      <div class="filters">
        <select id="upcoming-filter" class="day-select">
          <option value="ALL">Todos</option>
        </select>
      </div>
    """

    # Próximos partidos (SCHEDULED)
    days_with_matches = group_matches_by_day(matches, days=7)
    if not days_with_matches:
        if not matches:
            page_html += "<p class='muted'>No hay matches JSON para esta liga (todavía).</p>"
        else:
            page_html += "<p class='muted'>No hay partidos en los próximos 7 días.</p>"
    else:
        for day_block in days_with_matches:
            label = str(day_block.get("label", ""))
            day_matches = day_block.get("matches", []) or []
            count = len(day_matches)

            page_html += f"""
            <h3 class="day-title day-title-upcoming" data-day="{html_lib.escape(label)}">
              {html_lib.escape(label)} ({count} partido{"s" if count != 1 else ""})
            </h3>
            <div class="grid day-block upcoming-block" data-day="{html_lib.escape(label)}">
            """
            for m in day_matches:
                if not isinstance(m, dict):
                    continue
                home_part = _team_with_crest(m.get("home_crest"), m.get("home", ""))
                away_part = _team_with_crest(m.get("away_crest"), m.get("away", ""))
                page_html += f"""
              <div class="card">
                <div class="row">{home_part} <span class="vs">vs</span> {away_part}</div>
                <div class="meta">{html_lib.escape(str(m.get('utcDate','')))}</div>
              </div>
                """
            page_html += "</div>"

    # TOP 10
    page_html += """
      <h2 style="margin-top:20px;">⭐ Selecciones (Top 10 • Upcoming)</h2>
      <div class="grid">
    """

    if not selections:
        page_html += "<p class='muted'>No hay picks PENDING para esta liga (todavía).</p>"
    else:
        for p, best in selections:
            page_html += _render_pick_card(p, best)

    page_html += "</div>"

    # Resultados recientes (SETTLED) + dropdown
    page_html += """
      <h2 style="margin-top:22px;">✅ Resultados recientes (últimos 7 días)</h2>
      <div class="filters">
        <select id="settled-filter" class="day-select">
          <option value="ALL">Todos</option>
        </select>
      </div>
      <div class="section settled">
    """

    if not settled_sorted:
        page_html += "<p class='muted'>Todavía no hay picks resueltas para mostrar.</p>"
    elif not settled_groups:
        # IMPORTANTE: esto ahora es raro si tu JSON tiene settled_last_7d > 0
        page_html += "<p class='muted'>No hay picks resueltas dentro de los últimos 7 días.</p>"
    else:
        for day_block in settled_groups:
            label = str(day_block.get("label", ""))
            day_items = day_block.get("matches", []) or []
            count = len(day_items)

            page_html += f"""
            <h3 class="day-title day-title-settled" data-day="{html_lib.escape(label)}">
              {html_lib.escape(label)} ({count} pick{"s" if count != 1 else ""})
            </h3>
            <div class="grid day-block settled-block" data-day="{html_lib.escape(label)}">
            """
            for p in day_items:
                if not isinstance(p, dict):
                    continue
                page_html += _render_pick_card(p, None)
            page_html += "</div>"

    page_html += "</div>"

    # JS: llena dropdowns + filtra bloques
    page_html += f"""
      <script>
        (function() {{
          function fillSelect(selectId, blocksSelector) {{
            var sel = document.getElementById(selectId);
            if (!sel) return;

            var blocks = Array.prototype.slice.call(document.querySelectorAll(blocksSelector));
            var days = [];
            blocks.forEach(function(b) {{
              var d = b.getAttribute('data-day') || '';
              if (d && days.indexOf(d) === -1) days.push(d);
            }});

            days.forEach(function(d) {{
              var opt = document.createElement('option');
              opt.value = d;
              opt.textContent = d;
              sel.appendChild(opt);
            }});

            function apply() {{
              var val = sel.value || 'ALL';
              blocks.forEach(function(b) {{
                var d = b.getAttribute('data-day') || '';
                b.style.display = (val === 'ALL' || d === val) ? '' : 'none';
              }});

              // títulos asociados
              var isUpcoming = (selectId === 'upcoming-filter');
              var titleSelector = isUpcoming ? '.day-title-upcoming' : '.day-title-settled';
              var titles = Array.prototype.slice.call(document.querySelectorAll(titleSelector));
              titles.forEach(function(t) {{
                var d = t.getAttribute('data-day') || '';
                t.style.display = (val === 'ALL' || d === val) ? '' : 'none';
              }});
            }}

            sel.addEventListener('change', apply);
            apply();
          }}

          fillSelect('upcoming-filter', '.upcoming-block');
          fillSelect('settled-filter', '.settled-block');
        }})();
      </script>

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