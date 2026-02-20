from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from data.cache import read_json
from models.enums import DEFAULT_LEAGUE, LEAGUES

router = APIRouter()


def _safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


def _pill_bar(active: str) -> str:
    pills = []
    for code, name in LEAGUES.items():
        cls = "active" if code == active else ""
        pills.append(f'<a class="pill {cls}" href="/?league={code}">{name}</a>')
    return '<div class="leaguebar">' + "".join(pills) + "</div>"


def top_picks_with_variety(picks: list, top_n: int = 10, max_repeats_per_market: int = 3):
    used_count = {}
    chosen = []

    pool = []
    for p in picks:
        cands = p.get("candidates") or []
        cands = sorted(cands, key=lambda c: _safe_float(c.get("prob")), reverse=True)
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


@router.get("/ui", response_class=HTMLResponse, include_in_schema=False)
def ui_same(request: Request, league: str = Query(DEFAULT_LEAGUE)):
    return dashboard(request, league)


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, league: str = Query(DEFAULT_LEAGUE)):
    league = league if league in LEAGUES else DEFAULT_LEAGUE

    matches = read_json(f"daily_matches_{league}.json")   # lista de partidos
    picks = read_json(f"daily_picks_{league}.json")       # lista de picks con candidates

    # Top 10 “selecciones” (con variedad)
    selections = top_picks_with_variety(picks, top_n=10, max_repeats_per_market=3)

    html = f"""
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

      {_pill_bar(league)}

      <h2>📅 Próximos partidos</h2>
      <div class="grid">
    """

    # ✅ ACÁ van los partidos (no top10, no candidates)
    if not matches:
        html += "<p class='muted'>No hay matches JSON para esta liga (todavía).</p>"
    else:
        for m in matches[:30]:  # si querés 60, sacá el [:30]
            html += f"""
            <div class="card">
              <div class="row"><b>{m.get('home','')}</b> vs <b>{m.get('away','')}</b></div>
              <div class="meta">{m.get('utcDate','')}</div>
            </div>
            """

    html += """
      </div>

      <h2 style="margin-top:20px;">⭐ Selecciones (Top 10)</h2>
      <div class="grid">
    """

    # ✅ ACÁ van las selecciones top10 (con variedad)
    if not selections:
        html += "<p class='muted'>No hay picks JSON para esta liga (todavía).</p>"
    else:
        for p, best in selections:
            prob_pct = round(_safe_float(best.get("prob", 0)) * 100, 1)
            html += f"""
            <div class="card">
              <div class="row"><b>{p.get('home','')}</b> vs <b>{p.get('away','')}</b></div>
              <div class="meta">{p.get('utcDate','')}</div>
              <div class="pick">
                {best.get('market','—')} • {prob_pct}%
              </div>
            </div>
            """

    html += """
      </div>
    </body>
    </html>
    """
    return html