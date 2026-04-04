from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from data.cache import read_json, write_json
from data.providers.football_data import get_match_detail

router = APIRouter()

# Cuántos segundos consideramos fresco el cache de detalle de partido
_DETAIL_CACHE_TTL_SEC = 45


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except Exception:
        return default


def _pct(v: float) -> str:
    return f"{round(v * 100.0, 1)}%"


# ─────────────────────────────────────────────
# Estado visible del partido
# ─────────────────────────────────────────────

def _status_label(status: str, minute: int | None) -> str:
    s = (status or "").strip().upper()
    if s in {"FINISHED", "FT"}:
        return "Final del Partido"
    if s in {"HT", "HALFTIME", "HALF_TIME", "BREAK", "PAUSED"}:
        return "Descanso"
    if s in {"IN_PLAY", "LIVE", "1H", "2H", "PLAYING"} and minute:
        return f"🔴 {minute}'"
    if s in {"IN_PLAY", "LIVE", "PLAYING"}:
        return "🔴 En Vivo"
    if s in {"TIMED", "SCHEDULED"}:
        return "Próximo"
    if s in {"CANCELLED", "POSTPONED", "SUSPENDED", "AWARDED"}:
        return s.title()
    return s or "—"


# ─────────────────────────────────────────────
# Fetch con cache TTL
# ─────────────────────────────────────────────

def _fetch_match_detail(match_id: int) -> dict:
    """Devuelve detalle de partido. Usa cache de archivo si está fresco (<TTL)."""
    cache_key = f"live_match_{match_id}.json"
    cached = read_json(cache_key)
    if isinstance(cached, dict) and cached:
        cached_at = cached.get("_cached_at", 0)
        if isinstance(cached_at, (int, float)) and (time.time() - cached_at) < _DETAIL_CACHE_TTL_SEC:
            return cached

    detail = get_match_detail(match_id)
    if isinstance(detail, dict) and detail:
        detail["_cached_at"] = time.time()
        try:
            write_json(cache_key, detail)
        except Exception:
            pass
        return detail

    # Si la API falla, devolver cache viejo aunque esté expirado
    if isinstance(cached, dict) and cached:
        return cached
    return {}


# ─────────────────────────────────────────────
# Render de timeline de eventos
# ─────────────────────────────────────────────

def _build_timeline(match: dict) -> list[dict]:
    """
    Unifica goals + bookings + substitutions en una lista ordenada por minuto desc.
    Cada evento: {minute, type, side, primary, secondary}
    """
    events: list[dict] = []

    for g in (match.get("goals") or []):
        assist = g.get("assist")
        secondary = f"Asistencia: {assist}" if assist else None
        events.append({
            "minute":    g.get("minute"),
            "type":      "goal",
            "side":      g.get("side", "home"),
            "primary":   g.get("player") or "—",
            "secondary": secondary,
        })

    for b in (match.get("bookings") or []):
        card = (b.get("card") or "YELLOW").upper()
        events.append({
            "minute":    b.get("minute"),
            "type":      "yellow" if card == "YELLOW" else "red",
            "side":      b.get("side", "home"),
            "primary":   b.get("player") or "—",
            "secondary": None,
        })

    for s in (match.get("substitutions") or []):
        player_out = s.get("player_out")
        secondary = f"por {player_out}" if player_out else None
        events.append({
            "minute":    s.get("minute"),
            "type":      "sub",
            "side":      s.get("side", "home"),
            "primary":   s.get("player_in") or "—",
            "secondary": secondary,
        })

    events.sort(key=lambda e: (e.get("minute") or 0), reverse=True)
    return events


_EVENT_ICONS = {
    "goal":   "⚽",
    "yellow": "🟨",
    "red":    "🟥",
    "sub":    "🔄",
}


def _render_timeline_html(events: list[dict]) -> str:
    if not events:
        return '<p class="muted live-empty">Sin eventos registrados todavía.</p>'

    rows = []
    for e in events:
        minute  = e.get("minute")
        etype   = e.get("type", "goal")
        side    = e.get("side", "home")
        primary = e.get("primary") or "—"
        secondary = e.get("secondary")
        icon    = _EVENT_ICONS.get(etype, "•")
        min_str = f"{minute}'" if minute is not None else "—"
        sec_html = f'<div class="lev-secondary">{secondary}</div>' if secondary else ""

        if side == "home":
            rows.append(
                f'<div class="lev-row lev-row--home">'
                f'<div class="lev-minute">{min_str}</div>'
                f'<div class="lev-icon">{icon}</div>'
                f'<div class="lev-info">'
                f'<div class="lev-primary">{primary}</div>'
                f'{sec_html}'
                f'</div>'
                f'<div class="lev-spacer"></div>'
                f'</div>'
            )
        else:
            rows.append(
                f'<div class="lev-row lev-row--away">'
                f'<div class="lev-spacer"></div>'
                f'<div class="lev-info lev-info--away">'
                f'<div class="lev-primary">{primary}</div>'
                f'{sec_html}'
                f'</div>'
                f'<div class="lev-icon">{icon}</div>'
                f'<div class="lev-minute">{min_str}</div>'
                f'</div>'
            )

    return f'<div class="lev-list">{"".join(rows)}</div>'


# ─────────────────────────────────────────────
# Endpoint
# ─────────────────────────────────────────────

@router.get("/match/{match_id}", response_class=HTMLResponse)
def live_match_page(request: Request, match_id: int):
    match = _fetch_match_detail(match_id)

    score     = match.get("score") or {}
    hs        = score.get("home")
    as_       = score.get("away")
    score_l   = str(hs) if hs is not None else "—"
    score_r   = str(as_) if as_ is not None else "—"

    status    = match.get("status") or "—"
    minute    = match.get("minute")
    status_lbl = _status_label(status, minute)

    home_name  = str(match.get("home") or "Local")
    away_name  = str(match.get("away") or "Visitante")
    home_crest = str(match.get("home_crest") or "/static/logo_aftr.png")
    away_crest = str(match.get("away_crest") or "/static/logo_aftr.png")

    timeline_events = _build_timeline(match)
    timeline_html   = _render_timeline_html(timeline_events)

    is_empty = not match or not match.get("home")

    page_html = f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <title>AFTR — Partido {match_id}</title>
    <link rel="stylesheet" href="/static/style.css?v=23">
    <link rel="icon" type="image/png" href="/static/logo_aftr.png">
    <style>
      /* ── Live match detail ── */
      .lmd-header {{
        background: var(--card-bg, #1a1a2e);
        padding: 16px;
        text-align: center;
        border-bottom: 1px solid rgba(255,255,255,.08);
      }}
      .lmd-scorebar {{
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 12px;
        margin-bottom: 8px;
      }}
      .lmd-team {{ display: flex; flex-direction: column; align-items: center; gap: 4px; min-width: 80px; }}
      .lmd-crest {{ width: 40px; height: 40px; object-fit: contain; }}
      .lmd-team-name {{ font-size: .75rem; color: var(--muted, #aaa); max-width: 80px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
      .lmd-score {{ font-size: 2.2rem; font-weight: 700; letter-spacing: 2px; }}
      .lmd-status-lbl {{ font-size: .8rem; color: var(--muted, #aaa); margin-top: 4px; }}

      /* ── Tabs ── */
      .lmd-tabs {{ display: flex; overflow-x: auto; border-bottom: 1px solid rgba(255,255,255,.08); background: var(--card-bg, #1a1a2e); }}
      .lmd-tab {{ flex: none; padding: 12px 16px; font-size: .82rem; color: var(--muted, #aaa); cursor: pointer; border: none; background: none; border-bottom: 2px solid transparent; white-space: nowrap; }}
      .lmd-tab--active {{ color: var(--accent, #fff); border-bottom-color: var(--accent, #fff); }}

      /* ── Events (Minuto a minuto) ── */
      .lev-list {{ padding: 8px 0; }}
      .lev-row {{
        display: flex;
        align-items: flex-start;
        padding: 8px 16px;
        gap: 8px;
        border-bottom: 1px solid rgba(255,255,255,.04);
      }}
      .lev-row--home {{ flex-direction: row; }}
      .lev-row--away {{ flex-direction: row-reverse; }}
      .lev-minute {{ min-width: 32px; font-size: .78rem; color: var(--muted, #aaa); font-variant-numeric: tabular-nums; }}
      .lev-icon {{ font-size: 1rem; line-height: 1.4; }}
      .lev-info {{ display: flex; flex-direction: column; flex: 1; }}
      .lev-info--away {{ align-items: flex-end; }}
      .lev-primary {{ font-size: .88rem; font-weight: 500; }}
      .lev-secondary {{ font-size: .75rem; color: var(--muted, #aaa); margin-top: 1px; }}
      .lev-spacer {{ flex: 1; }}
      .live-empty {{ padding: 24px; text-align: center; }}

      /* ── Panels ── */
      .lmd-panel {{ padding: 16px; }}
      .lmd-panel--hidden {{ display: none; }}
    </style>
  </head>
  <body>
    <header class="top top-pro live-header">
      <div class="lmd-header">
        <div class="lmd-scorebar">
          <div class="lmd-team">
            <img class="lmd-crest" src="{home_crest}" alt="" onerror="this.src='/static/logo_aftr.png';this.onerror=null;"/>
            <div class="lmd-team-name">{home_name}</div>
          </div>
          <div class="lmd-score">{score_l} — {score_r}</div>
          <div class="lmd-team">
            <img class="lmd-crest" src="{away_crest}" alt="" onerror="this.src='/static/logo_aftr.png';this.onerror=null;"/>
            <div class="lmd-team-name">{away_name}</div>
          </div>
        </div>
        <div class="lmd-status-lbl">{status_lbl}</div>
      </div>
    </header>

    <div class="lmd-tabs" role="tablist">
      <button class="lmd-tab lmd-tab--active" type="button" data-tab="timeline">Minuto a minuto</button>
      <button class="lmd-tab" type="button" data-tab="info">Info</button>
    </div>

    <div class="page">
      <div class="lmd-panel" data-panel="timeline">
        {timeline_html if not is_empty else '<p class="muted live-empty">No se pudo cargar el detalle del partido.</p>'}
      </div>
      <div class="lmd-panel lmd-panel--hidden" data-panel="info">
        <p class="muted" style="padding:16px 0; font-size:.85rem;">
          Match ID: {match_id} · Status: {status}
        </p>
      </div>
    </div>

    <script>
    (function(){{
      var tabs   = document.querySelectorAll(".lmd-tab");
      var panels = document.querySelectorAll(".lmd-panel");
      tabs.forEach(function(t){{
        t.addEventListener("click", function(){{
          var key = t.getAttribute("data-tab");
          tabs.forEach(function(x){{ x.classList.toggle("lmd-tab--active", x === t); }});
          panels.forEach(function(p){{ p.classList.toggle("lmd-panel--hidden", p.getAttribute("data-panel") !== key); }});
        }});
      }});
    }})();
    </script>
  </body>
</html>"""
    return HTMLResponse(page_html)
