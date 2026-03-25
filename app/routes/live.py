from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from data.cache import read_json

router = APIRouter()


def _mock_live_match(match_id: int) -> dict[str, Any]:
    # Mock structure intended to mirror your cached JSON shape.
    # When you connect real data later, keep keys consistent with this object.
    return {
        "match_id": match_id,
        "league": "PL",
        "minute": 57,
        "status": "LIVE",
        "home": {"name": "Arsenal", "crest": "/static/logo_aftr.png"},
        "away": {"name": "Liverpool", "crest": "/static/leagues/pl.png"},
        "score": {"home": 2, "away": 1},
        "aftr_live_signal": {
            "title": "AFTR LIVE SIGNAL",
            "live_probability": 0.67,
            "confidence": 0.78,
            "trend": {"label": "Strong Momentum", "kind": "UP"},
            "reasons": [
                "Live xG edge trending in favor of the home side",
                "Market implied swing + momentum acceleration",
                "Tactical pressure increased in the last ~10 minutes",
            ],
        },
        "timeline": [
            {"minute": 3, "side": "home", "text": "Early pressure: 1st corner conceded"},
            {"minute": 12, "side": "away", "text": "Counter attack blocked (off target)"},
            {"minute": 21, "side": "home", "text": "Goal! Home takes the lead"},
            {"minute": 44, "side": "away", "text": "Goal! Away equalizes"},
            {"minute": 57, "side": "home", "text": "Goal! Home scores again"},
        ],
        "stats": {
            "possession": {"home": 55, "away": 45},
            "shots": {"home": 11, "away": 8},
            "shots_on_target": {"home": 6, "away": 3},
            "corners": {"home": 7, "away": 4},
            "yellow_cards": {"home": 1, "away": 2},
        },
        "picks": {
            "prematch_pick": {
                "market": "1X",
                "prob": 0.61,
                "status": "WIN",
                "tier": "ELITE",
            },
            "live_opportunities": [
                {"market": "Over 1.5", "status": "WATCH", "prob": 0.64},
                {"market": "BTTS YES", "status": "WEAK", "prob": 0.52},
            ],
        },
        "h2h": {
            "title": "H2H (últimos 5)",
            "rows": [
                {"season": "2025", "home": 1, "away": 1},
                {"season": "2024", "home": 2, "away": 0},
                {"season": "2023", "home": 0, "away": 1},
                {"season": "2022", "home": 1, "away": 2},
                {"season": "2021", "home": 3, "away": 1},
            ],
        },
    }


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _pct(v: float) -> str:
    return f"{round(v * 100.0, 1)}%"


def _bar_pct(home_val: float, away_val: float) -> tuple[float, float]:
    total = max(home_val + away_val, 1.0)
    return (home_val / total) * 100.0, (away_val / total) * 100.0


@router.get("/match/{match_id}", response_class=HTMLResponse)
def live_match_page(request: Request, match_id: int):
    # 1) Try cached JSON first (future real integration); 2) fallback to mock.
    cached = read_json(f"live_match_{match_id}.json")
    if not isinstance(cached, dict) or not cached:
        match = _mock_live_match(match_id)
    else:
        match = cached

    home = match.get("home") or {}
    away = match.get("away") or {}
    score = match.get("score") or {}
    minute = int(match.get("minute") or 0)
    status = str(match.get("status") or "LIVE")

    home_name = str(home.get("name") or "Home")
    away_name = str(away.get("name") or "Away")
    home_crest = str(home.get("crest") or "/static/logo_aftr.png")
    away_crest = str(away.get("crest") or "/static/logo_aftr.png")

    hs = int(score.get("home") or 0)
    as_ = int(score.get("away") or 0)

    aftr = match.get("aftr_live_signal") or {}
    live_prob = _safe_float(aftr.get("live_probability"), 0.0)
    confidence = _safe_float(aftr.get("confidence"), 0.0)
    trend = aftr.get("trend") or {}
    trend_label = str(trend.get("label") or "—")
    trend_kind = str(trend.get("kind") or "UP").upper()
    trend_badge_kind = "live-trend-up" if trend_kind in {"UP", "UPWARD", "STRONG"} else "live-trend-dn"

    reasons = aftr.get("reasons") or []
    timeline = match.get("timeline") or []
    stats = match.get("stats") or {}
    picks = match.get("picks") or {}
    prematch = picks.get("prematch_pick") or {}
    live_opps = picks.get("live_opportunities") or []
    h2h = match.get("h2h") or {}
    h2h_rows = h2h.get("rows") or []
    prematch_status = str(prematch.get("status") or "WIN").strip().upper()
    prematch_badge_class = f"live-pick-badge--{prematch_status.lower()}"

    # Stats bars: use comparative segments (home/away in one track).
    def stat_row(stat_key: str, label: str, invert: bool = False) -> str:
        s = stats.get(stat_key) or {}
        hv = float(s.get("home") or 0)
        av = float(s.get("away") or 0)
        if invert:
            # e.g. yellow cards: "lower is better" visually; invert by swapping.
            hv, av = av, hv
        hp, ap = _bar_pct(hv, av)
        return (
            f'<div class="live-stat-row">'
            f'  <div class="live-stat-label">{label}</div>'
            f'  <div class="live-comparebar" aria-label="{label} home {hv} away {av}">'
            f'    <div class="live-comparebar-home" style="width:{hp}%" title="{home_name}: {hv}"></div>'
            f'    <div class="live-comparebar-away" style="width:{ap}%" title="{away_name}: {av}"></div>'
            f'  </div>'
            f'  <div class="live-stat-values"><span>{hv}</span><span class="live-muted">-</span><span>{av}</span></div>'
            f'</div>'
        )

    page_html = f"""
<!doctype html>
<html>
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <title>AFTR Live — Match {match_id}</title>
    <link rel="stylesheet" href="/static/style.css?v=22">
    <link rel="icon" type="image/png" href="/static/logo_aftr.png">
  </head>
  <body>
    <header class="top top-pro live-header">
      <div class="live-header-inner">
        <a href="/" class="muted live-back-link">Inicio</a>
        <div class="live-matchbar" role="group" aria-label="Marcador en vivo">
          <div class="live-team live-team--home">
            <img class="crest" src="{home_crest}" alt=""/>
            <div class="live-team-name">{home_name}</div>
          </div>
          <div class="live-scorecol">
            <div class="live-score" aria-label="Marcador">{hs} - {as_}</div>
            <div class="live-minute-row">
              <span class="live-minute">{minute}’</span>
              <span class="live-live-badge" aria-label="{status}">LIVE</span>
            </div>
          </div>
          <div class="live-team live-team--away">
            <img class="crest" src="{away_crest}" alt=""/>
            <div class="live-team-name">{away_name}</div>
          </div>
        </div>
        <div class="live-header-right">
          <span class="pill live-watch-hint" data-match-id="{match_id}">WATCH</span>
        </div>
      </div>
    </header>

    <div class="page live-page">
      <div class="live-tabs" role="tablist" aria-label="Secciones del partido">
        <button class="live-tab" type="button" role="tab" aria-selected="true" data-tab="resumen">Resumen</button>
        <button class="live-tab" type="button" role="tab" aria-selected="false" data-tab="timeline">Timeline</button>
        <button class="live-tab" type="button" role="tab" aria-selected="false" data-tab="stats">Stats</button>
        <button class="live-tab" type="button" role="tab" aria-selected="false" data-tab="picks">AFTR Picks</button>
        <button class="live-tab" type="button" role="tab" aria-selected="false" data-tab="h2h">H2H</button>
      </div>

      <section class="live-panel live-panel--active" data-panel="resumen">
        <div class="card live-main-card">
          <div class="live-main-top">
            <div class="live-main-kicker">{aftr.get("title") or "AFTR LIVE SIGNAL"}</div>
            <div class="live-trend-badge {trend_badge_kind}">{trend_label}</div>
          </div>
          <div class="live-main-metrics">
            <div class="live-metric">
              <div class="live-metric-label">Live probability</div>
              <div class="live-metric-value">{_pct(live_prob)}</div>
            </div>
            <div class="live-metric">
              <div class="live-metric-label">Confidence</div>
              <div class="live-metric-value">{_pct(confidence)}</div>
            </div>
          </div>
          <ul class="live-reasons">
            {''.join(f'<li>{r}</li>' for r in reasons[:3])}
          </ul>
          <div class="live-actions">
            <button type="button" class="pill live-watch-btn" data-match-id="{match_id}" aria-label="Watch (connect later)">WATCH</button>
          </div>
        </div>
      </section>

      <section class="live-panel" data-panel="timeline" hidden>
        <div class="card live-timeline-card">
          <h2 class="live-h2">Timeline</h2>
          <div class="live-timeline">
            {''.join(
              (lambda e: (
                f'<div class="live-timeline-row">'
                f'  <div class="live-timeline-side live-timeline-side--{str(e.get("side") or "home").lower()}"></div>'
                f'  <div class="live-timeline-minute">{e.get("minute","—")}’</div>'
                f'  <div class="live-timeline-text">{e.get("text","")}</div>'
                f'</div>'
              ))(e) for e in timeline
            )}
          </div>
        </div>
      </section>

      <section class="live-panel" data-panel="stats" hidden>
        <div class="card live-stats-card">
          <h2 class="live-h2">Stats</h2>
          {stat_row("possession", "Possession")}
          {stat_row("shots", "Shots")}
          {stat_row("shots_on_target", "Shots on target")}
          {stat_row("corners", "Corners")}
          {stat_row("yellow_cards", "Yellow cards", invert=True)}
        </div>
      </section>

      <section class="live-panel" data-panel="picks" hidden>
        <div class="card live-picks-card">
          <h2 class="live-h2">AFTR Picks</h2>

          <div class="live-pick-block">
            <div class="live-pick-title">Prematch pick</div>
            <div class="live-pick-row">
              <div class="live-pick-market">{prematch.get("market") or "—"}</div>
              <div class="live-pick-prob">{_pct(_safe_float(prematch.get("prob"), 0.0))}</div>
              <div class="live-pick-badge {prematch_badge_class}">{prematch_status}</div>
            </div>
            <div class="live-pick-sub muted">Tier: {prematch.get("tier") or "—"}</div>
          </div>

          <div class="live-pick-block">
            <div class="live-pick-title">Live opportunities</div>
            <div class="live-live-opps">
              {''.join(
                f'<div class="live-opportunity">'
                f'  <div class="live-op-market">{o.get("market") or "—"}</div>'
                f'  <div class="live-op-row">'
                f'    <div class="live-pick-prob">{_pct(_safe_float(o.get("prob"), 0.0))}</div>'
                f'    <div class="live-pick-badge live-pick-badge--{str(o.get("status","WATCH")).strip().lower()}">{str(o.get("status","WATCH")).strip().upper()}</div>'
                f'  </div>'
                f'</div>'
                for o in live_opps
              )}
            </div>
          </div>
        </div>
      </section>

      <section class="live-panel" data-panel="h2h" hidden>
        <div class="card live-h2h-card">
          <h2 class="live-h2">{h2h.get("title") or "H2H"}</h2>
          <div class="live-h2h-table">
            {''.join(
              f'<div class="live-h2h-row">'
              f'  <div class="live-h2h-season">{r.get("season","—")}</div>'
              f'  <div class="live-h2h-score"><span>{r.get("home","0")}</span><span class="live-muted">-</span><span>{r.get("away","0")}</span></div>'
              f'</div>'
              for r in h2h_rows
            )}
          </div>
        </div>
      </section>
    </div>

    <script>
      (function(){{
        var tabs = document.querySelectorAll(".live-tab");
        var panels = document.querySelectorAll(".live-panel");
        function setActive(tabKey){{
          tabs.forEach(function(t){{
            var on = t.getAttribute("data-tab") === tabKey;
            t.setAttribute("aria-selected", on ? "true" : "false");
            if(on) t.classList.add("live-tab--active"); else t.classList.remove("live-tab--active");
          }});
          panels.forEach(function(p){{
            var on = p.getAttribute("data-panel") === tabKey;
            if(on){{ p.hidden = false; p.classList.add("live-panel--active"); }}
            else {{ p.hidden = true; p.classList.remove("live-panel--active"); }}
          }});
        }}
        tabs.forEach(function(t){{
          t.addEventListener("click", function(){{
            setActive(t.getAttribute("data-tab"));
          }});
        }});
      }})();
    </script>
  </body>
</html>
    """
    return HTMLResponse(page_html)

