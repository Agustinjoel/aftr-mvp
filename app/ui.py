from __future__ import annotations

import html as html_lib
import json
import logging
import os
import re
import unicodedata
from datetime import date, datetime, timezone, timedelta
from typing import Any, Callable

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from config.settings import settings
from data.cache import read_json, read_json_with_fallback, read_cache_meta
from data.providers.football_data import get_unsupported_leagues
from app.routes.matches import group_matches_by_day
from core.poisson import market_priority

from itsdangerous import URLSafeSerializer, BadSignature

from fastapi import Body
from fastapi.responses import JSONResponse
from app.timefmt import AFTR_DISPLAY_TZ, format_match_kickoff_ar, parse_utc_instant
from app.auth import create_user
from app.auth import get_user_id, get_user_by_id
from app.models import get_active_plan
from app.user_helpers import can_see_all_picks, is_admin, is_premium_active
from fastapi import Form

router = APIRouter()
logger = logging.getLogger("aftr.ui")
_finished_card_debug_logged = False


# Universal auth modal bootstrap: define on window so no page can throw ReferenceError.
# Inject this script on every page before any other scripts run.
AUTH_BOOTSTRAP_JS = r"""
window.openLoginModal = window.openLoginModal || function () {
  var m = document.getElementById("login-modal");
  if (m) {
    m.style.display = "flex";
    return;
  }
  window.location.href = "/?auth=login";
};
window.closeLoginModal = window.closeLoginModal || function () {
  var m = document.getElementById("login-modal");
  if (m) m.style.display = "none";
};
window.openSignupModal = window.openSignupModal || function () {
  var m = document.getElementById("signup-modal");
  if (m) {
    m.style.display = "flex";
    return;
  }
  window.location.href = "/?auth=register";
};
window.closeSignupModal = window.closeSignupModal || function () {
  var m = document.getElementById("signup-modal");
  if (m) m.style.display = "none";
};
"""
AUTH_BOOTSTRAP_SCRIPT = "<script>" + AUTH_BOOTSTRAP_JS + "</script>"


# =========================================================
# SaaS: cookie firmada (plan)
# =========================================================
def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(settings.secret_key, salt="aftr-premium")

def _get_user_id(request: Request) -> int | None:
    raw = (request.cookie or {}).get("aftr_user")
    if not raw:
        return None

    try:
        return int(raw)
    except Exception:
        return None

def _get_plan_from_cookie(request: Request) -> str:
    raw = (request.cookies or {}).get("aftr_plan")
    if not raw:
        return settings.plan_free
    try:
        data = _serializer().loads(raw)
        plan = (data.get("plan") or "").upper()
        if plan in (settings.plan_free, settings.plan_premium, settings.plan_pro):
            return plan
    except BadSignature:
        pass
    return settings.plan_free

def _norm_market(m: str | None) -> str:
    m = (m or "").strip()
    if not m:
        return "UNKNOWN"
    up = m.upper()

    # normalizamos grupos (opcional)
    if up in ("1", "X", "2", "1X", "X2", "12"):
        return "RESULTADO"
    if "OVER" in up or "O/U" in up or "UNDER" in up:
        return "GOLES (O/U)"
    if "BTTS" in up or "AMBOS" in up:
        return "BTTS"
    if "AH" in up or "HANDICAP" in up:
        return "HANDICAP"
    if "DC" in up or "DOBLE" in up or "DOUBLE" in up:
        return "DOBLE OPORT."
    return m  # deja el market como viene


def _pick_market(p: dict) -> str:
    # prioridad: market del “best”
    m = p.get("best_market")
    if m:
        return _norm_market(m)

    # si tenés best dentro de candidates:
    cands = p.get("candidates") or []
    if isinstance(cands, list) and cands:
        # por si el primero es el mejor (siempre que ya vengan ordenados)
        m2 = (cands[0] or {}).get("market")
        if m2:
            return _norm_market(m2)

    return "UNKNOWN"


def _profit_by_market(settled_picks: list[dict]) -> list[dict]:
    """
    Devuelve lista ordenada por profit desc:
    [{market, picks, wins, losses, push, winrate, net_units}]
    """
    buckets: dict[str, dict] = {}

    for p in settled_picks or []:
        if not isinstance(p, dict):
            continue

        market = _pick_market(p)
        b = buckets.setdefault(market, {
            "market": market,
            "picks": 0,
            "wins": 0,
            "losses": 0,
            "push": 0,
            "net_units": 0.0,
        })

        b["picks"] += 1

        r = _result_norm(p)
        if r == "WIN":
            b["wins"] += 1
        elif r == "LOSS":
            b["losses"] += 1
        elif r == "PUSH":
            b["push"] += 1

        b["net_units"] += float(_unit_delta(p) or 0.0)

    out = []
    for m, b in buckets.items():
        settled = b["wins"] + b["losses"]
        winrate = (b["wins"] / settled * 100.0) if settled > 0 else None
        out.append({
            "market": b["market"],
            "picks": b["picks"],
            "wins": b["wins"],
            "losses": b["losses"],
            "push": b["push"],
            "winrate": round(winrate, 1) if winrate is not None else None,
            "net_units": round(b["net_units"], 3),
        })

    out.sort(key=lambda x: (x["net_units"], x["picks"]), reverse=True)
    return out

def _set_plan_cookie(resp: RedirectResponse, plan: str) -> None:
    plan = (plan or settings.plan_free).upper()
    if plan not in (settings.plan_free, settings.plan_premium, settings.plan_pro):
        plan = settings.plan_free

    token = _serializer().dumps({"plan": plan})
    resp.set_cookie(
        key="aftr_plan",
        value=token,
        max_age=60 * 60 * 24 * 30,  # 30 días
        httponly=True,
        samesite="lax",
        path="/",
    )


def _clear_plan_cookie(resp: RedirectResponse) -> None:
    resp.delete_cookie("aftr_plan", path="/")


@router.post("/auth/signup")
def signup(data: dict = Body(...)):
    email = (data.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return JSONResponse({"ok": False, "error": "email_invalido"}, status_code=400)

    # guarda en leads (sin password)
    from app.db import get_conn
    from datetime import datetime, timezone

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS leads (email TEXT PRIMARY KEY, created_at TEXT)")
        cur.execute(
            "INSERT OR IGNORE INTO leads(email, created_at) VALUES (?, ?)",
            (email, datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
    finally:
        conn.close()

    resp = JSONResponse({"ok": True})
    resp.set_cookie("aftr_user", email, max_age=60*60*24*365, samesite="lax", path="/")
    return resp

@router.get("/premium/activate", include_in_schema=False)
def premium_activate(request: Request, plan: str = "PREMIUM"):
    plan = (plan or "PREMIUM").upper()
    if plan not in (settings.plan_premium, settings.plan_pro):
        plan = settings.plan_premium

    resp = RedirectResponse(url="/", status_code=302)
    _set_plan_cookie(resp, plan)
    return resp


@router.get("/premium/logout", include_in_schema=False)
def premium_logout(request: Request):
    resp = RedirectResponse(url="/", status_code=302)
    _clear_plan_cookie(resp)
    return resp

def _pick_score(p: dict) -> float:
    """Ranking score: prob + conf always; edge added when present (football with odds)."""
    bp = _safe_float(p.get("best_prob"))          # 0..1
    edge_val = p.get("edge")
    conf = _safe_float(p.get("confidence")) / 10.0  # 0..1

    # pesos: prob manda, edge evita picks “empatados”, conf refuerza
    score = (bp * 0.65) + (conf * 0.15)
    if edge_val is not None:
        try:
            score += float(edge_val) * 1.20
        except (TypeError, ValueError):
            pass

    # bonus si modelo B
    if (p.get("model") or "").strip().upper() == "B":
        score += 0.03

    return score


def _aftr_score(p: dict) -> int:
    """
    AFTR Score 0-100 for display on pick cards.
    Formula: best_prob (main) + confidence (secondary) + positive edge as bonus when available.
    When edge is missing, uses probability + confidence only. NBA compatible.
    """
    bp = _safe_float(p.get("best_prob"), 0)
    conf_raw = _safe_int(p.get("confidence"))
    if conf_raw is None:
        conf_norm = 0.5
    else:
        conf_norm = max(0, min(1, (conf_raw - 1) / 9.0))  # 1-10 -> 0-1
    score = (bp * 60) + (conf_norm * 30)  # 0-60 + 0-30 = 0-90
    edge_val = p.get("edge")
    if edge_val is not None:
        try:
            e = float(edge_val)
            if e > 0:
                score += min(e * 100, 10)  # bonus up to 10 pts for positive edge
        except (TypeError, ValueError):
            pass
    return max(0, min(100, int(round(score))))


# =========================================================
# Helpers UI
# =========================================================
def _team_with_crest(crest: str | None, name: str) -> str:
    """Render team row: use crest URL when present (e.g. from API), else /static/teams/{slug}.png. Fallback to default.svg on 404."""
    def _normalize_team_name(raw: str) -> str:
        n = (raw or "").strip()
        if not n:
            return ""
        # Shorten common prefixes/suffixes for a cleaner premium UI.
        n = n.replace("Football Club", "FC")
        n = n.replace("Club Atlético", "Atl.")
        # Remove "Hotspur" (e.g. Tottenham Hotspur -> Tottenham) to prevent awkward wrapping.
        words = [w for w in n.split() if w.strip().lower() != "hotspur"]
        n = " ".join(words).strip()
        # Collapse multiple spaces.
        n = " ".join(n.split())
        return n

    normalized_name = _normalize_team_name(name)
    safe_name = html_lib.escape(normalized_name or "")
    small_name = len(normalized_name) >= 18
    if crest and isinstance(crest, str) and crest.strip():
        src = crest.strip()
    else:
        src = _team_logo_path(name or "")
    safe_src = html_lib.escape(src)
    fallback = html_lib.escape(TEAM_LOGO_FALLBACK_PATH)
    team_name_class = "team-name team-name--small" if small_name else "team-name"
    return (
        f'<span class="team-row">'
        f'<img src="{safe_src}" alt="" class="crest" loading="lazy" width="28" height="28" '
        f'onerror="this.src=\'{fallback}\';this.onerror=null;"/>'
        f'<span class="{team_name_class}">{safe_name}</span>'
        f"</span>"
    )

def _unit_delta(p: dict) -> float:
    """Saca delta de unidades por pick resuelta.
    Si existe profit_units/net_units lo usa. Si no, fallback: WIN=+1, LOSS=-1, PUSH=0.
    """
    if not isinstance(p, dict):
        return 0.0

    for k in ("profit_units", "net_units", "units_delta"):
        v = p.get(k)
        if v is not None:
            try:
                return float(v)
            except Exception:
                pass

    r = (p.get("result") or "").strip().upper()
    if r == "WIN":
        return 1.0
    if r == "LOSS":
        return -1.0
    return 0.0


def _pick_stake_units(p: dict) -> float:
    """Stake in units for ROI denominator. Prefer explicit fields; else infer from suggest_units; default 1u."""
    if not isinstance(p, dict):
        return 1.0
    for k in ("stake_units", "units_staked", "stake_size", "bet_units", "stake"):
        v = p.get(k)
        if v is not None:
            try:
                s = float(v)
                if s > 0:
                    return s
            except (TypeError, ValueError):
                pass
    su = _suggest_units(p)
    if isinstance(su, str) and su.strip().lower().endswith("u"):
        try:
            s = float(su.strip()[:-1].strip())
            if s > 0:
                return s
        except (TypeError, ValueError):
            pass
    return 1.0


def _roi_spark_points(settled_groups: list[dict]) -> list[dict]:
    """Build chart data: list of { date, label, v (cumulative profit), day (day net) } in chronological order."""
    pts: list[dict] = []
    cum = 0.0
    for g in reversed(settled_groups or []):
        date_str = str(g.get("date", ""))
        label = str(g.get("label", "") or date_str or "—")
        items = g.get("matches") or []
        day_net = 0.0
        for p in items:
            if not isinstance(p, dict):
                continue
            u = _unit_delta(p)
            if abs(u) < 1e-9:
                continue
            day_net += u
        cum += day_net
        pts.append({
            "date": date_str,
            "label": label,
            "v": round(cum, 3),
            "day": round(day_net, 3),
        })
    return pts

def _suggest_units(p: dict) -> str:
    c = _safe_int(p.get("confidence"))
    if c is None:
        return "—"
    if c >= 8:
        return "1.0u"
    if c >= 5:
        return "0.6u"
    return "0.3u"

def _safe_float(x, default: float = 0.0) -> float:
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


def _is_pick_valid(p: dict) -> bool:
    """
    True if pick has required data for display: probability > 0, edge not null, odds present.
    Used to hide leagues/picks with missing or empty data.
    """
    if not p or not isinstance(p, dict):
        return False
    prob = _safe_float(p.get("best_prob"), 0)
    if prob <= 0:
        return False
    if p.get("edge") is None:
        return False
    odds_decimal = p.get("odds_decimal")
    odds = p.get("odds")
    has_odds = (odds_decimal is not None) or (odds is not None and odds != "")
    if not has_odds:
        return False
    return True


def _format_cache_status(meta: dict) -> str:
    """Genera HTML para la barra de estado: Última actualización / Actualizando datos."""
    if meta.get("refresh_running"):
        # Tras ~6.5s consulta /api/status: si el flag ya bajó, muestra última actualización; si no, sigue actualizando.
        return (
            '<div class="cache-status cache-status-updating aftr-cache-updating" role="status" '
            'data-refresh-pending="1">Actualizando datos...</div>'
            "<script>(function(){document.querySelectorAll('[data-refresh-pending=\"1\"]').forEach(function(el){"
            "setTimeout(async function(){try{var r=await fetch('/api/status');var j=await r.json();"
            "el.classList.remove('cache-status-updating');if(j.refresh_running){"
            "el.classList.add('cache-status-updating');return;}"
            "el.classList.add('muted');var lu=j.last_update;"
            "el.textContent=lu?('Última actualización: '+String(lu).replace('T',' ').slice(0,16)):"
            "'Última actualización: —';}catch(e){el.classList.remove('cache-status-updating');"
            "el.classList.add('muted');el.textContent='Estado no disponible. Recargá la página.';}"
            "},8000);});})();</script>"
        )
    last = meta.get("last_updated")
    if not last:
        return '<div class="cache-status muted">Última actualización: —</div>'
    try:
        dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        formatted = dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        formatted = str(last)
    return f'<div class="cache-status muted">Última actualización: {html_lib.escape(formatted)}</div>'


def _parse_utcdate_str(s) -> datetime:
    try:
        if isinstance(s, str) and s:
            if s.endswith("Z"):
                return datetime.fromisoformat(s.replace("Z", "+00:00"))
            return datetime.fromisoformat(s)
    except Exception:
        pass
    return datetime.now(timezone.utc)


def _pick_local_date(p: dict, match_by_key: dict[Any, dict] | None) -> date | None:
    """Return the match calendar date in America/Argentina/Buenos_Aires, or None if unknown."""
    utc_str = p.get("utcDate")
    if not utc_str and match_by_key:
        mid = _safe_int(p.get("match_id")) or _safe_int(p.get("id"))
        league = (p.get("_league") or p.get("league") or "").strip()
        m = match_by_key.get((league, mid)) if mid is not None and league else None
        if isinstance(m, dict):
            utc_str = m.get("utcDate")
    if not utc_str:
        return None
    dt = parse_utc_instant(utc_str)
    if dt is None:
        return None
    return dt.astimezone(AFTR_DISPLAY_TZ).date()


def _pill_bar(active: str, unsupported: set[str] | None = None) -> str:
    unsupported = unsupported or set()
    pills = []
    for code, name in settings.leagues.items():
        if code in unsupported:
            continue
        cls = "active" if code == active else ""
        pills.append(f'<a class="pill {cls}" href="/?league={code}">{html_lib.escape(name)}</a>')
    return '<div class="leaguebar">' + "".join(pills) + "</div>"


def _home_league_active_code(request: Request) -> str:
    """League to highlight on home carousel (URL hint or default)."""
    raw = (request.query_params.get("league") or "").strip()
    if raw and settings.is_valid_league(raw):
        return raw
    return settings.default_league


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
        '<script src="/static/home_league_carousel.js?v=6" defer></script>'
        if include_script
        else ""
    )
    return core + script


# =========================================================
# Cards bloqueadas (teaser Premium)
# =========================================================
def _locked_card(message: str = "Disponible en Premium") -> str:
    return f"""
    <div class="card locked-card" onclick="openPremium()" role="button" tabindex="0">
      <div class="locked-overlay">
        <div class="locked-title">🔒 {html_lib.escape(message)}</div>
        <div class="locked-sub">Desbloqueá picks + combinadas + más ligas</div>
        <button class="pill locked-btn" onclick="event.stopPropagation(); openPremium();">Ver Premium</button>
      </div>

      <div class="locked-content">
        <div class="row">
          <span class="team-row"><span class="team-name">Equipo Local</span></span>
          <span class="vs">vs</span>
          <span class="team-row"><span class="team-name">Equipo Visitante</span></span>
        </div>

        <div class="meta">2026-02-26T21:00:00Z</div>

        <div class="pick pick-best">
          <span class="pick-main">Market</span>
          <span class="pick-badge">PENDING</span>
          <span class="pick-prob">&mdash; 62.5%</span>
        </div>

        <div class="conf-wrap conf-mid">
          <div class="conf-label"><b>CONF 7/10</b></div>
          <div class="conf-track">
            <span class="conf-tick on"></span><span class="conf-tick on"></span><span class="conf-tick on"></span>
            <span class="conf-tick on"></span><span class="conf-tick on"></span><span class="conf-tick on"></span>
            <span class="conf-tick on"></span><span class="conf-tick"></span><span class="conf-tick"></span><span class="conf-tick"></span>
          </div>
        </div>

        <div class="candidates">
          <div class="cand-row">
            <div class="cand-head">
              <span class="cand-mkt">O/U 2.5</span><span class="cand-pct">58%</span>
            </div>
            <div class="cand-track"><div class="cand-fill fill-mid" style="width:58%"></div></div>
          </div>

          <div class="cand-row">
            <div class="cand-head">
              <span class="cand-mkt">BTTS</span><span class="cand-pct">54%</span>
            </div>
            <div class="cand-track"><div class="cand-fill fill-low" style="width:54%"></div></div>
          </div>
        </div>
      </div>
    </div>
    """


def _locked_grid(n: int = 6, message: str = "Disponible en Premium") -> str:
    return "".join(_locked_card(message) for _ in range(max(1, int(n))))


def _premium_unlock_card() -> str:
    """
    Single card shown after the first 3 picks for free users. Matches pick card style.
    Explica qué se pierden y ofrece Obtener Premium.
    """
    return """
    <div class="card premium-unlock-card" onclick="openPremium()" role="button" tabindex="0">
      <div class="premium-unlock-inner">
        <div class="premium-unlock-title">🔒 Desbloqueá todos los picks AFTR</div>
        <p class="premium-unlock-sub">Los usuarios free ven solo 3 picks por día.</p>
        <p class="premium-unlock-sub">AFTR Premium incluye:</p>
        <ul class="premium-unlock-list">
          <li>Todas las selecciones del día</li>
          <li>Apuestas de valor con ventaja positiva</li>
          <li>Picks de todas las ligas</li>
          <li>Desglose de probabilidades</li>
        </ul>
        <button class="pill premium-unlock-btn" onclick="event.stopPropagation(); openPremium();">Obtener Premium</button>
      </div>
    </div>
    """




def top_picks_with_variety(picks: list, top_n: int = 10, max_repeats_per_market: int = 3):
    chosen: list[tuple[dict, dict]] = []

    pool: list[tuple[dict, list[dict]]] = []
    for p in picks or []:
        if not isinstance(p, dict):
            continue
        cands = p.get("candidates") or []
        if not isinstance(cands, list):
            cands = []

        cands = sorted(
            [c for c in cands if isinstance(c, dict)],
            key=lambda c: (market_priority(c.get("market")), -_safe_float(c.get("prob"))),
        )
        if cands:
            pool.append((p, cands))

    pool.sort(key=lambda item: _pick_score(item[0]), reverse=True)

    for p, cands in pool:
        best = max(cands, key=lambda c: _safe_float(c.get("prob"))) if cands else None
        if best is None:
            continue
        chosen.append((p, best))
        if len(chosen) >= top_n:
            break

    return chosen

def _risk_label_from_conf(p: dict) -> str:
    c = _safe_int(p.get("confidence"))
    if c is None:
        return "—"
    if c >= 8:
        return "SAFE"
    if c >= 5:
        return "MEDIUM"
    return "SPICY"

def _result_norm(p: dict) -> str:
    r = (p.get("result") or "").strip().upper()
    # Normalize legacy/spanish outcomes to english codes.
    spanish_to_english = {
        # PUSH
        "EMPUJAR": "PUSH",
        "EMPATAR": "PUSH",
        "EMPATE": "PUSH",
        "DRAW": "PUSH",
        # WIN
        "GANAR": "WIN",
        "GANA": "WIN",
        # LOSS
        "PERDER": "LOSS",
        "PIERDE": "LOSS",
    }
    if r in ("WIN", "LOSS", "PUSH", "PENDING"):
        return r
    if r in spanish_to_english:
        return spanish_to_english[r]
    return "PENDING"


def _parse_utcdate_maybe(s: object) -> datetime | None:
    """Parse utcDate/ISO string into UTC datetime. Returns None on failure (no fallback-to-now)."""
    return parse_utc_instant(s)


def _combo_leg_kickoff_html(leg: dict) -> str:
    """Optional kickoff line for combo legs (America/Argentina/Buenos_Aires)."""
    ko = format_match_kickoff_ar(leg.get("utcDate"))
    if ko == "—":
        return ""
    return f'<div class="combo-kickoff muted">{html_lib.escape(ko)}</div>'


# Match / provider statuses meaning the fixture is in progress (not final).
MATCH_LIVE_STATUSES = frozenset(
    {
        "LIVE",
        "IN_PLAY",
        "INPLAY",
        "PLAYING",
        "1H",
        "FIRST_HALF",
        "H1",
        "2H",
        "SECOND_HALF",
        "H2",
        "HT",
        "HALFTIME",
        "HALF_TIME",
        "BREAK",
        "PAUSED",
        "SUSPENDED",
        "ET",
        "EXTRA_TIME",
        "AET",
        "PENALTIES",
        "PENALTY_SHOOTOUT",
        "INT",
        "LIVE_1H",
        "LIVE_2H",
        # Basketball / API-Sports style (short codes)
        "Q1",
        "Q2",
        "Q3",
        "Q4",
        "1Q",
        "2Q",
        "3Q",
        "4Q",
        "OT",
        "BT",
        "C1",
        "C2",
        "C3",
        "C4",
    }
)


def _match_live_status_token(match: dict) -> str:
    """
    Normalize live-relevant status from flat or nested provider shapes (Football-Data, API-Sports, etc.).
    """
    if not isinstance(match, dict):
        return ""
    for key in ("status", "match_status", "state"):
        v = match.get(key)
        if v is not None and str(v).strip():
            return str(v).strip().upper()
    fx = match.get("fixture")
    if isinstance(fx, dict):
        st = fx.get("status")
        if isinstance(st, dict):
            for sub in ("short", "long", "shortName", "type"):
                x = st.get(sub)
                if x is not None and str(x).strip():
                    return str(x).strip().upper()
        elif st is not None and str(st).strip():
            return str(st).strip().upper()
    return ""


def isMatchFinished(match: dict) -> bool:
    """
    Decide finished vs upcoming from explicit fields first:
    - status == "FINISHED"
    - result outcome present (WIN/LOSS/PUSH) in `result`
    - finished flag
    - score + utcDate in the past (home_score/away_score or common score variants)
    """
    if not isinstance(match, dict):
        return False

    status_raw = _match_live_status_token(match)
    if status_raw in {"FINISHED", "FINAL", "SETTLED", "FINALIZADO"}:
        return True
    if status_raw in {"WIN", "LOSS", "PUSH"}:
        return True

    # In progress: never treat as finished from score + kickoff-time heuristic alone.
    if status_raw in MATCH_LIVE_STATUSES:
        return False

    raw_result = match.get("result")
    if raw_result is not None:
        norm = _result_norm({"result": raw_result})
        if norm in {"WIN", "LOSS", "PUSH"}:
            return True

    finished_flag_raw = match.get("finished")
    if isinstance(finished_flag_raw, bool):
        if finished_flag_raw:
            return True
    elif finished_flag_raw is not None:
        try:
            if str(finished_flag_raw).strip().lower() in {"1", "true", "yes", "y", "finished"}:
                return True
        except Exception:
            pass

    # Score + time condition.
    # Prefer explicit home_score/away_score but also accept common legacy variants.
    home_score = match.get("home_score")
    away_score = match.get("away_score")
    if home_score is None and away_score is None:
        home_score = match.get("score_home") if match.get("score_home") is not None else match.get("homeScore")
        away_score = match.get("score_away") if match.get("score_away") is not None else match.get("awayScore")
    if (home_score is None or away_score is None):
        sc = match.get("score")
        if isinstance(sc, dict):
            hh = sc.get("home")
            aa = sc.get("away")
            if hh is not None and aa is not None:
                home_score = hh if home_score is None else home_score
                away_score = aa if away_score is None else away_score
            ft = sc.get("fullTime") or sc.get("full_time")
            if isinstance(ft, dict):
                fh = ft.get("home")
                fa = ft.get("away")
                if fh is not None and fa is not None:
                    home_score = fh if home_score is None else home_score
                    away_score = fa if away_score is None else away_score

    if home_score is not None and away_score is not None:
        dt = _parse_utcdate_maybe(match.get("utcDate"))
        if dt is not None and dt <= datetime.now(timezone.utc):
            # Ensure score parseable to ints
            if _safe_int(home_score) is not None and _safe_int(away_score) is not None:
                # Stale TIMED/SCHEDULED + score + past kickoff is usually LIVE (provider/cache lag), not FT.
                if status_raw in {"TIMED", "SCHEDULED"}:
                    return False
                return True

    return False


def isMatchLive(match: dict) -> bool:
    """True when the fixture is in play (by status), and not considered finished."""
    if not isinstance(match, dict):
        return False
    if isMatchFinished(match):
        return False
    status_raw = _match_live_status_token(match)
    return status_raw in MATCH_LIVE_STATUSES


def _live_minute_suffix(match: dict) -> str | None:
    """Return a display minute like \"67'\" from common API fields, or None."""
    if not isinstance(match, dict):
        return None
    for key in ("minute", "elapsed", "time_elapsed", "match_minute"):
        raw = match.get(key)
        if raw is None:
            continue
        try:
            mi = int(float(str(raw).replace("'", "").strip()))
            if mi >= 0:
                return f"{mi}'"
        except (TypeError, ValueError):
            continue
    return None


def _format_live_status_line(match: dict) -> str:
    """Compact status for live headers (e.g. 🔴 LIVE 67', HT, 2H 74')."""
    if not isinstance(match, dict):
        return "🔴 LIVE"
    st = _match_live_status_token(match)
    minute_s = _live_minute_suffix(match)

    if st in {"HT", "HALFTIME", "HALF_TIME", "BREAK"}:
        return "HT"
    if st in {"1H", "FIRST_HALF", "H1"}:
        return f"1H {minute_s}" if minute_s else "1H"
    if st in {"2H", "SECOND_HALF", "H2"}:
        return f"2H {minute_s}" if minute_s else "2H"
    if st in {"ET", "EXTRA_TIME", "AET"}:
        return f"ET {minute_s}" if minute_s else "ET"
    if st in {"PENALTIES", "PENALTY_SHOOTOUT"}:
        return "Pen."
    if st in {"PAUSED", "SUSPENDED"}:
        return st.title()
    if st == "INT":
        return "Int."
    if minute_s:
        return f"🔴 LIVE {minute_s}"
    return "🔴 LIVE"


def _label_for_date(d: date, today: date) -> str:
    if d == today:
        return "Hoy"
    if d == today - timedelta(days=1):
        return "Ayer"
    return d.isoformat()


_WEEKDAY_LABELS = ("Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo")


def group_upcoming_picks_by_day(picks: list[dict], days: int = 7) -> list[dict]:
    """
    Group upcoming (future) picks by local date. Returns list of { date, label, picks }.
    label: "Hoy" | "Mañana" | weekday name. Only dates in [today, today + days - 1].
    """
    today = datetime.now().astimezone().date()
    end = today + timedelta(days=max(0, days - 1))
    by_date: dict[str, list[dict]] = {}
    for p in picks or []:
        if not isinstance(p, dict):
            continue
        dt = _parse_utcdate_str(p.get("utcDate"))
        local_d = dt.astimezone().date() if dt.tzinfo else dt.date()
        if not (today <= local_d <= end):
            continue
        date_str = local_d.isoformat()
        by_date.setdefault(date_str, []).append(p)
    out = []
    for date_str in sorted(by_date.keys()):
        day_picks = by_date[date_str]
        day_picks.sort(key=lambda x: (x.get("utcDate") or ""))
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        if d == today:
            label = "Hoy"
        elif d == today + timedelta(days=1):
            label = "Mañana"
        else:
            label = _WEEKDAY_LABELS[d.weekday()]
        out.append({"date": date_str, "label": label, "picks": day_picks})
    return out


def group_picks_recent_by_day_desc(items: list[dict], days: int = 7):
    now = datetime.now(timezone.utc)
    today_local = now.astimezone().date()
    cutoff_local = today_local - timedelta(days=max(1, int(days)))

    buckets: dict[date, list[dict]] = {}
    for p in items or []:
        if not isinstance(p, dict):
            continue
        dt = _parse_utcdate_str(p.get("utcDate"))
        if dt.tzinfo:
            local_d = dt.astimezone().date()
        else:
            local_d = dt.date()
        if local_d < cutoff_local:
            continue
        buckets.setdefault(local_d, []).append(p)

    out = []
    for d in sorted(buckets.keys(), reverse=True):
        out.append({
            "date": d.isoformat(),
            "label": _label_for_date(d, today_local),
            "matches": buckets[d],
        })
    return out


# =========================================================
# FLIP: Back content (stats placeholder safe)
# =========================================================
def _stat_line(label: str, home_val, away_val) -> str:
    return f"""
      <div class="statline">
        <div class="statlabel">{html_lib.escape(label)}</div>
        <div class="statval">{html_lib.escape(str(home_val))}</div>
        <div class="teamcol">{html_lib.escape(str(away_val))}</div>
      </div>
    """


def _wdl_badge(letter: str) -> str:
    l = (letter or "").upper().strip()
    cls = "wdl-d"
    if l == "W":
        cls = "wdl-w"
    elif l == "L":
        cls = "wdl-l"
    return f'<span class="wdl {cls}">{html_lib.escape(l or "—")}</span>'


def _pct_class(pct: float) -> str:
    if pct >= 75:
        return "fill-high"
    if pct >= 55:
        return "fill-mid"
    return "fill-low"

def _market_key(m: str) -> str:
    m = (m or "").strip().upper()
    # normalizamos algunos nombres típicos
    if m in ("1", "X", "2", "1X", "X2", "12"):
        return "RES"
    if "OVER" in m or "O/" in m or "O/U" in m:
        return "OVER"
    if "BTTS" in m or "AMBOS" in m:
        return "BTTS"
    return "GEN"

def _to_pct01(x):
    """convierte prob 0..1 a porcentaje 0..100"""
    try:
        if x is None:
            return None
        return max(0.0, min(100.0, float(x) * 100.0))
    except Exception:
        return None

def _bar_single(label: str, left_pct: float | None, right_pct: float | None) -> str:
    """Dos barras separadas, cada una muestra su % real (no relativo)"""
    left_txt = f"{round(left_pct)}%" if left_pct is not None else "—"
    right_txt = f"{round(right_pct)}%" if right_pct is not None else "—"

    left_w = max(0.0, min(100.0, float(left_pct))) if left_pct is not None else 0.0
    right_w = max(0.0, min(100.0, float(right_pct))) if right_pct is not None else 0.0

    left_cls = _pct_class(float(left_pct or 0.0))
    right_cls = _pct_class(float(right_pct or 0.0))

    return f"""
    <div class="bar-row">
      <div class="bar-head">
        <span>{html_lib.escape(label)}</span>
        <span class="muted">{left_txt} • {right_txt}</span>
      </div>

      <div class="bar-track">
        <div class="bar-fill left {left_cls}" data-w="{left_w}"></div>
      </div>

      <div class="bar-track" style="margin-top:8px;">
        <div class="bar-fill right {right_cls}" data-w="{right_w}"></div>
      </div>
    </div>
    """

def _chips_from_form(form_str: str, max_n: int = 5) -> str:
    parts = [x.strip().upper() for x in (form_str or "").replace("-", " ").split() if x.strip()]
    parts = parts[:max_n]
    out = []
    for x in parts:
        if x == "W":
            out.append('<span class="chip w">W</span>')
        elif x == "D":
            out.append('<span class="chip d">D</span>')
        elif x == "L":
            out.append('<span class="chip l">L</span>')
    return "".join(out) if out else '<span class="muted">—</span>'


def _render_back_stats(p: dict, market: str = "") -> str:
    # Back side: compact analysis (no match score, no duplicate market).
    stats_home = p.get("stats_home") if isinstance(p.get("stats_home"), dict) else {}
    stats_away = p.get("stats_away") if isinstance(p.get("stats_away"), dict) else {}

    # Basketball: fallback compact info (no football-specific blocks).
    if (p.get("model") or "").strip().upper() == "BASKETBALL":
        form_h = stats_home.get("form", "")
        form_a = stats_away.get("form", "")
        pick_text = market or "—"
        return f"""
    <div class="back-card back-compact">
      <div class="back-sub">Pick: <b>{html_lib.escape(str(pick_text))}</b></div>
      <div class="back-divider"></div>
      <div class="back-form-compact">
        <div class="back-form-head">
          <span class="back-form-title">ÚLTIMOS</span>
          <span class="back-form-sub muted">5</span>
        </div>
        <div class="back-form-row">
          <div class="back-form-team">
            <div class="back-form-legend muted">H</div>
            <div class="back-form-chips">{_chips_from_form(str(form_h), 5) if form_h else ""}</div>
          </div>
          <div class="back-form-team right">
            <div class="back-form-legend muted">A</div>
            <div class="back-form-chips">{_chips_from_form(str(form_a), 5) if form_a else ""}</div>
          </div>
        </div>
      </div>
    </div>
    """

    gf_h = stats_home.get("gf", "—")
    ga_h = stats_home.get("ga", "—")
    form_h = stats_home.get("form", "")

    gf_a = stats_away.get("gf", "—")
    ga_a = stats_away.get("ga", "—")
    form_a = stats_away.get("form", "")

    over_h_pct = _to_pct01(stats_home.get("over25"))
    over_a_pct = _to_pct01(stats_away.get("over25"))
    btts_h_pct = _to_pct01(stats_home.get("btts"))
    btts_a_pct = _to_pct01(stats_away.get("btts"))

    # Insight (one-liner, premium and non-debug).
    mk = _market_key(market)
    if mk == "OVER":
        insight = "Enfoque: más de 2.5 y presión ofensiva"
    elif mk == "BTTS":
        insight = "Enfoque: BTTS por empuje en ambos lados"
    elif mk == "RES":
        insight = "Enfoque: ataque vs defensa + forma"
    else:
        insight = "Enfoque: forma y tendencia estadística"

    def _safe_form_chips(v) -> str:
        if v is None:
            return ""
        s = str(v).strip()
        if not s:
            return ""
        chips = _chips_from_form(s, 5)
        return "" if "—" in chips else chips

    form_h_html = _safe_form_chips(form_h)
    form_a_html = _safe_form_chips(form_a)

    form_html = f"""
      <div class="back-form-compact">
        <div class="back-form-head">
          <span class="back-form-title">ÚLTIMOS</span>
          <span class="back-form-sub muted">5</span>
        </div>
        <div class="back-form-row">
          <div class="back-form-team">
            <div class="back-form-legend muted">H</div>
            <div class="back-form-chips">{form_h_html}</div>
          </div>
          <div class="back-form-team right">
            <div class="back-form-legend muted">A</div>
            <div class="back-form-chips">{form_a_html}</div>
          </div>
        </div>
      </div>
    """

    # GF/GA compact block (values home vs away).
    def _has_stat(v) -> bool:
        if v is None:
            return False
        s = str(v).strip()
        if not s or s == "—":
            return False
        return s.lower() != "none"

    def _fmt_stat(v) -> str:
        if not _has_stat(v):
            return "—"
        return html_lib.escape(str(v))

    show_gfga = _has_stat(gf_h) or _has_stat(gf_a) or _has_stat(ga_h) or _has_stat(ga_a)
    gfga_html = ""
    if show_gfga:
        gf_h_s = _fmt_stat(gf_h)
        gf_a_s = _fmt_stat(gf_a)
        ga_h_s = _fmt_stat(ga_h)
        ga_a_s = _fmt_stat(ga_a)
        gfga_html = f"""
      <div class="back-gg-compact">
        <div class="back-gg-col">
          <div class="back-gg-label">GF</div>
          <div class="back-gg-values">
            <span class="back-gg-num">{gf_h_s}</span>
            <span class="back-gg-vs">vs</span>
            <span class="back-gg-num right">{gf_a_s}</span>
          </div>
        </div>
        <div class="back-gg-col">
          <div class="back-gg-label">GA</div>
          <div class="back-gg-values">
            <span class="back-gg-num">{ga_h_s}</span>
            <span class="back-gg-vs">vs</span>
            <span class="back-gg-num right">{ga_a_s}</span>
          </div>
        </div>
      </div>
        """

    # Trends: BTTS + Over 2.5 (compact bars).
    btts_html = ""
    if btts_h_pct is not None or btts_a_pct is not None:
        btts_html = _bar_single("BTTS", btts_h_pct, btts_a_pct)
    over_html = ""
    if over_h_pct is not None or over_a_pct is not None:
        over_html = _bar_single("Más de 2.5", over_h_pct, over_a_pct)

    trends_html = ""
    if btts_html or over_html:
        trends_parts = [x for x in [btts_html, over_html] if x]
        trends_html = f'<div class="back-bars back-bars-compact">{"".join(trends_parts)}</div>'

    blocks = [form_html]
    if gfga_html:
        blocks.append(gfga_html)
    if trends_html:
        blocks.append(trends_html)
    sections = ""
    for i, b in enumerate(blocks):
        if not b:
            continue
        if sections:
            sections += '<div class="back-divider"></div>'
        sections += b

    return f"""
    <div class="back-card back-compact">
      {sections}
      <div class="back-insight muted">{html_lib.escape(insight)}</div>
    </div>
    """


# =========================================================
# Score extractor (compat)
# =========================================================
def _extract_score_from_match(m: dict) -> tuple[int | None, int | None]:
    if not isinstance(m, dict):
        return (None, None)

    def _parse_score_string(score_str: object) -> tuple[int | None, int | None]:
        if score_str is None:
            return (None, None)
        s = str(score_str).strip()
        if not s:
            return (None, None)
        # Extract first two integers in any format: "1-1", "1:1", "1–1", "Final 1-1"
        nums = re.findall(r"\d+", s)
        if len(nums) >= 2:
            return (_safe_int(nums[0]), _safe_int(nums[1]))
        return (None, None)

    sc = m.get("score")
    if isinstance(sc, str):
        return _parse_score_string(sc)
    if isinstance(sc, dict):
        h = sc.get("home")
        a = sc.get("away")
        if h is not None and a is not None:
            return (_safe_int(h), _safe_int(a))

        ft = sc.get("fullTime") or sc.get("full_time")
        if isinstance(ft, dict):
            h = ft.get("home")
            a = ft.get("away")
            if h is not None and a is not None:
                return (_safe_int(h), _safe_int(a))

    hg = m.get("home_goals")
    ag = m.get("away_goals")
    if hg is not None and ag is not None:
        hi, ai = _safe_int(hg), _safe_int(ag)
        if hi is not None and ai is not None:
            return (hi, ai)

    return (None, None)


def _extract_score(p: dict, match_by_id: dict[int, dict] | None = None) -> tuple[int | None, int | None]:
    if not isinstance(p, dict):
        return (None, None)

    def _parse_score_string(score_str: object) -> tuple[int | None, int | None]:
        if score_str is None:
            return (None, None)
        s = str(score_str).strip()
        if not s:
            return (None, None)
        nums = re.findall(r"\d+", s)
        if len(nums) >= 2:
            return (_safe_int(nums[0]), _safe_int(nums[1]))
        return (None, None)

    # Try common explicit score fields first
    for hk, ak in [
        ("score_home", "score_away"),
        ("home_score", "away_score"),
        ("homeScore", "awayScore"),
        ("h_home_score", "h_away_score"),  # rare legacy keys
    ]:
        h = p.get(hk)
        a = p.get(ak)
        if h is not None and a is not None:
            return (_safe_int(h), _safe_int(a))

    # Try generic score container
    sc = p.get("score")
    if isinstance(sc, str):
        return _parse_score_string(sc)

    if isinstance(sc, dict):
        hh = sc.get("home")
        aa = sc.get("away")
        if hh is not None and aa is not None:
            return (_safe_int(hh), _safe_int(aa))
        ft = sc.get("fullTime") or sc.get("full_time")
        if isinstance(ft, dict):
            hh = ft.get("home")
            aa = ft.get("away")
            if hh is not None and aa is not None:
                return (_safe_int(hh), _safe_int(aa))

    mid = _safe_int(p.get("match_id") or p.get("id"))
    if match_by_id and mid is not None:
        # Common case: match_by_id keyed by match_id (int).
        if mid in match_by_id:
            return _extract_score_from_match(match_by_id[mid])

        # Dashboard case: match_by_id keyed by (league_code, match_id).
        league_code = (p.get("_league") or p.get("league") or "").strip()
        if league_code:
            candidate_keys = [
                (league_code, mid),
                (str(league_code), mid),
                (league_code, str(mid)),
                (str(league_code), str(mid)),
            ]
            for k in candidate_keys:
                if k in match_by_id:
                    return _extract_score_from_match(match_by_id[k])

    return (None, None)


# =========================================================
# Card renderer
# =========================================================
def _pick_id_for_card(p: dict, best: dict | None = None) -> str:
    """Stable id for a pick (for favorite/follow). Uses p.id or composite from league, match_id, market, utcDate."""
    if not isinstance(p, dict):
        return ""
    pid = p.get("id") or p.get("pick_id")
    if pid is not None and str(pid).strip():
        return str(pid).strip()
    league = (p.get("_league") or p.get("league") or "").strip()
    match_id = str(p.get("match_id") or p.get("id") or "")
    market = (best or {}).get("market") or p.get("best_market") or ""
    utc = str(p.get("utcDate") or "")
    return "|".join([league, match_id, market, utc]).strip("|") or "unknown"


def _pick_odds_display_value(p: dict) -> str:
    """
    Single source for pick odds shown in UI (home cards + league flip cards).
    Order: odds_decimal → best_fair (same as AFTR value scoring) → implied_prob → fair from best_prob.
    Returns a short display token, e.g. "2.10", "Impl 48.2%", or "—".
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
    """Home meta row: full label (e.g. 'Odds 2.10' or 'Impl 48%') for one span."""
    v = _pick_odds_display_value(p)
    if v == "—":
        return "Odds —"
    if v.startswith("Impl"):
        return v
    return f"Odds {v}"


def _render_pick_card(p: dict, best: dict | None = None, match_by_id: dict | None = None) -> str:
    global _finished_card_debug_logged
    home_name = p.get("home", "")
    away_name = p.get("away", "")
    home_team_attr = html_lib.escape(str(home_name or ""))
    away_team_attr = html_lib.escape(str(away_name or ""))

    home_part = _team_with_crest(p.get("home_crest"), home_name)
    away_part = _team_with_crest(p.get("away_crest"), away_name)

    best_market = (best or {}).get("market") or p.get("best_market") or "—"

    best_prob = (best or {}).get("prob")
    if best_prob is None:
        best_prob = p.get("best_prob")
    best_prob_present = best_prob is not None
    best_prob_pct = round(_safe_float(best_prob, 0) * 100, 1)

    best_fair = (best or {}).get("fair")
    if best_fair is None:
        best_fair = p.get("best_fair")
    best_fair_str = f" • {best_fair}" if best_fair is not None else ""

    result = _result_norm(p)
    status_raw = str(p.get("status") or "").strip().upper()
    finished_flag_raw = p.get("finished")
    finished_flag = False
    if isinstance(finished_flag_raw, bool):
        finished_flag = finished_flag_raw
    elif finished_flag_raw is not None:
        finished_flag = str(finished_flag_raw).strip().lower() in {"1", "true", "yes", "y", "finished"}

    # If status carries explicit outcome, prefer it.
    if status_raw in ("WIN", "LOSS", "PUSH"):
        result = status_raw
    else:
        # Handle legacy/spanish outcome stored in `status` (e.g. EMPUJAR/EMPATE).
        maybe_outcome = _result_norm({"result": status_raw})
        if maybe_outcome in ("WIN", "LOSS", "PUSH"):
            result = maybe_outcome

    match_for_state: dict | None = None
    if isinstance(match_by_id, dict):
        mid = _safe_int(p.get("match_id") or p.get("id"))
        if mid is not None:
            if mid in match_by_id:
                match_for_state = match_by_id[mid]
            else:
                league_code = (p.get("_league") or p.get("league") or "").strip()
                if league_code:
                    candidate_keys = [
                        (league_code, mid),
                        (str(league_code), mid),
                        (league_code, str(mid)),
                        (str(league_code), str(mid)),
                    ]
                    for k in candidate_keys:
                        if k in match_by_id:
                            match_for_state = match_by_id[k]
                            break

    # Single authoritative finished detection.
    is_finished = isMatchFinished(p) or (isMatchFinished(match_for_state) if isinstance(match_for_state, dict) else False)
    is_live_display = isinstance(match_for_state, dict) and isMatchLive(match_for_state)
    if is_live_display:
        is_finished = False

    final_home_score: int | None = None
    final_away_score: int | None = None
    if is_finished:
        final_home_score, final_away_score = _extract_score(p, match_by_id)
        if not _finished_card_debug_logged:
            logger.debug(
                "Finished card debug: home_team=%s away_team=%s home_score=%s away_score=%s result=%s status=%s",
                str(p.get("home") or p.get("home_team") or ""),
                str(p.get("away") or p.get("away_team") or ""),
                str(final_home_score),
                str(final_away_score),
                str(p.get("result") or ""),
                str(p.get("status") or ""),
            )
            _finished_card_debug_logged = True

    card_class = "card aftr-pick-card"
    if is_live_display:
        card_class = "card aftr-pick-card aftr-pick-card--live"
    elif result == "WIN":
        card_class = "card pick-win aftr-pick-card"
    elif result == "LOSS":
        card_class = "card pick-loss aftr-pick-card"
    elif result == "PUSH":
        card_class = "card pick-push aftr-pick-card"

    risk = _risk_label_from_conf(p)
    badge_html = (
        f'<span class="pick-badge">{html_lib.escape(result)}</span>'
        f'<span class="pick-badge risk {html_lib.escape(risk.lower())}">{html_lib.escape(risk)}</span>'
    )

    # Front side: teams block. Live / finished: score between teams; else vs.
    live_hs: int | None = None
    live_as: int | None = None
    if is_live_display and isinstance(match_for_state, dict):
        live_hs, live_as = _extract_score_from_match(match_for_state)

    if is_live_display and live_hs is not None and live_as is not None:
        teams_html = f"""
    <div class="aftr-teams aftr-teams-live">
      <div class="aftr-team aftr-team-left">{home_part}</div>
      <div class="aftr-score-inline aftr-score-inline-live">
        <span class="aftr-score-home">{live_hs}</span>
        <span class="aftr-score-sep">-</span>
        <span class="aftr-score-away">{live_as}</span>
      </div>
      <div class="aftr-team aftr-team-right">{away_part}</div>
    </div>
    """
    elif is_finished and final_home_score is not None and final_away_score is not None:
        teams_html = f"""
    <div class="aftr-teams aftr-teams-finished">
      <div class="aftr-team aftr-team-left">{home_part}</div>
      <div class="aftr-score-inline">
        <span class="aftr-score-home">{final_home_score}</span>
        <span class="aftr-score-sep">-</span>
        <span class="aftr-score-away">{final_away_score}</span>
      </div>
      <div class="aftr-team aftr-team-right">{away_part}</div>
    </div>
    """
    else:
        teams_html = f"""
    <div class="aftr-teams">
      <div class="aftr-team aftr-team-left">{home_part}</div>
      <div class="aftr-vs">vs</div>
      <div class="aftr-team aftr-team-right">{away_part}</div>
    </div>
    """

    # CONF BAR
    conf_i = _safe_int(p.get("confidence"))
    conf_bar = ""
    if conf_i is not None:
        conf_i = max(1, min(10, int(conf_i)))
        ticks = []
        for i in range(1, 11):
            cls = "conf-tick on" if i <= conf_i else "conf-tick"
            ticks.append(f'<span class="{cls}"></span>')

        level_class = "conf-low"
        if conf_i >= 8:
            level_class = "conf-high"
        elif conf_i >= 5:
            level_class = "conf-mid"

        conf_bar = f"""
        <div class="conf-wrap {level_class}">
            <div class="conf-label"><b>CONF {conf_i}/10</b></div>
            <div class="conf-track">
                {''.join(ticks)}
            </div>
        </div>
        """

    # CANDIDATES (top 3)
    candidates = p.get("candidates") or []
    if not isinstance(candidates, list):
        candidates = []
    top3 = [c for c in candidates if isinstance(c, dict)][:3]

    cand_lines = []
    for c in top3:
        mkt = html_lib.escape((c.get("market") or "—"))
        prob = _safe_float(c.get("prob", 0))
        prob_pct = round(prob * 100, 1)
        width_pct = max(0.0, min(100.0, prob_pct))

        fill_cls = "fill-low"
        if prob_pct >= 75:
            fill_cls = "fill-high"
        elif prob_pct >= 55:
            fill_cls = "fill-mid"

        cand_lines.append(f"""
        <div class="cand-row">
            <div class="cand-head">
                <span class="cand-mkt">{mkt}</span>
                <span class="cand-pct">{prob_pct}%</span>
            </div>
            <div class="cand-track">
                <div class="cand-fill {fill_cls}" data-w="{width_pct}"></div>
            </div>
        </div>
        """)

    cand_block = "\n".join(cand_lines) if cand_lines else "<div class='cand-line muted'>Sin candidatos</div>"

    # Odds (football only; only when at least one field present — NBA cards unchanged)
    odds_decimal = p.get("odds_decimal")
    implied_prob = p.get("implied_prob")
    edge_val = p.get("edge")
    _bt = p.get("bookmaker_title")
    bookmaker_title = str(_bt).strip() if _bt is not None else ""
    odds_parts = []
    odds_display = _pick_odds_display_value(p)
    if odds_display != "—":
        if odds_display.startswith("Impl"):
            odds_parts.append(odds_display)
        else:
            odds_parts.append(f"Odds {odds_display}")
    if edge_val is not None:
        try:
            ev = float(edge_val)
            sign = "+" if ev >= 0 else ""
            odds_parts.append(f"Ventaja {sign}{ev * 100:.1f}%")
        except (TypeError, ValueError):
            pass
    odds_line_html = ""
    if odds_parts:
        odds_line_html = '<div class="pick-odds muted">' + html_lib.escape(" • ".join(odds_parts))
        if bookmaker_title:
            odds_line_html += ' <span class="pick-bookmaker">' + html_lib.escape(bookmaker_title) + "</span>"
        odds_line_html += "</div>"

    # Compact "prob • odds" line for premium front (same source as home cards).
    odds_compact = ""
    oc = _pick_odds_display_value(p)
    if oc != "—":
        odds_compact = oc
    if odds_compact:
        prob_odds_html = f"""
        <div class="aftr-prob-odds">
          <span class="aftr-prob">{best_prob_pct}%</span>
          <span class="aftr-dot">•</span>
          <span class="aftr-odds">{html_lib.escape(odds_compact)}</span>
        </div>
        """
    else:
        prob_odds_html = f"""
        <div class="aftr-prob-odds">
          <span class="aftr-prob">{best_prob_pct}%</span>
        </div>
        """

    # AFTR Score block: use new fields when present, else legacy _aftr_score
    aftr_score_val = p.get("aftr_score")
    if aftr_score_val is None:
        aftr_score_val = _aftr_score(p)
    else:
        try:
            aftr_score_val = int(round(float(aftr_score_val)))
        except (TypeError, ValueError):
            aftr_score_val = _aftr_score(p)
    _t = p.get("tier")
    tier = (str(_t).strip().lower() if _t is not None else "pass") or "pass"
    tier_colors = {"elite": "#FFD700", "strong": "#00C853", "risky": "#FF9800", "pass": "#9E9E9E"}
    tier_color = tier_colors.get(tier, "#9E9E9E")
    edge_display = p.get("edge")
    if edge_display is not None:
        try:
            edge_str = f"{float(edge_display) * 100:.2f}%"
        except (TypeError, ValueError):
            edge_str = "—"
    else:
        edge_str = "—"
    conf_val = p.get("confidence_level") or p.get("confidence")
    if conf_val is None:
        conf_level_raw = ""
    else:
        conf_level_raw = str(conf_val).strip()
    conf_level = conf_level_raw.upper() if conf_level_raw else ""
    tier_label = "watch" if tier == "pass" else tier
    if edge_str != "—":
        try:
            val = float(str(edge_display).replace(",", ".")) * 100
            edge_badge = f"+{val:.1f}%" if val >= 0 else f"{val:.1f}%"
        except (TypeError, ValueError):
            edge_badge = edge_str
    else:
        edge_badge = ""
    conf_badge_text = f"{conf_level} CONF" if conf_level else "CONF EN PROCESO"
    aftr_badges = []
    aftr_badges.append(f'<span class="aftr-badge aftr-badge-tier" style="border-color:{tier_color};color:{tier_color};">{html_lib.escape(tier_label)}</span>')
    if edge_badge:
        aftr_badges.append(f'<span class="aftr-badge aftr-badge-edge">{html_lib.escape(edge_badge)} EDGE</span>')
    aftr_badges.append(f'<span class="aftr-badge aftr-badge-conf">{html_lib.escape(conf_badge_text)}</span>')
    aftr_badges_html = "".join(aftr_badges)
    aftr_block_html = f"""
      <div class="aftr-score-block" style="border-left: 4px solid {tier_color};">
        <div class="aftr-score-label">AFTR Score</div>
        <div class="aftr-score-num">{aftr_score_val}</div>
        <div class="aftr-badges">{aftr_badges_html}</div>
      </div>"""

    # Top meta row (league, kickoff time, tier) for premium front.
    kickoff_time = format_match_kickoff_ar(p.get("utcDate"))
    if kickoff_time == "—" and isinstance(match_for_state, dict):
        kickoff_time = format_match_kickoff_ar(match_for_state.get("utcDate"))
    if is_live_display and isinstance(match_for_state, dict):
        kickoff_time = _format_live_status_line(match_for_state)
    meta_time_class = "aftr-meta-time" + (" aftr-meta-live" if is_live_display else "")
    league_code = (p.get("_league") or p.get("league") or "").strip()
    league_label = settings.leagues.get(league_code, league_code) if league_code else "AFTR"
    tier_meta_badge_html = (
        f'<span class="aftr-meta-tier-pill" style="border-color:{tier_color};color:{tier_color};">{html_lib.escape(tier_label)}</span>'
    )
    if is_finished and tier_label == "watch":
        # Finished result cards are already covered by WIN/LOSS/PUSH badge; hide WATCH for a cleaner UI.
        tier_meta_badge_html = ""
    pick_id_val = _pick_id_for_card(p, best)
    pick_id_attr = html_lib.escape(pick_id_val)
    market_val = (best or {}).get("market") or p.get("best_market") or ""
    market_attr = html_lib.escape(str(market_val))
    edge_raw = p.get("edge")
    edge_attr = html_lib.escape(str(edge_raw)) if edge_raw is not None else ""
    if is_finished:
        if final_home_score is None or final_away_score is None:
            try:
                logger.debug(
                    "Missing final score extract for pick_id=%s match_id=%s result=%s status=%s",
                    str(p.get("id") or p.get("pick_id") or ""),
                    str(p.get("match_id") or ""),
                    str(p.get("result") or ""),
                    str(p.get("status") or ""),
                )
            except Exception:
                pass

        outcome_badge = result if result in ("WIN", "LOSS", "PUSH") else "FINALIZADO"
        prob_line_html = ""
        if best_prob_present:
            prob_line_html = f'<div class="pick-finished-prob">{best_prob_pct:.1f}%</div>'
        pick_actions_html = f"""
      <div class="pick-finished-status pick-main-highlight">
        <div class="pick-finished-top">
          <div class="pick-finished-market">{html_lib.escape(str(best_market))}</div>
          {prob_line_html}
        </div>
        <div class="pick-finished-badge-row">
          <span class="pick-badge">{html_lib.escape(outcome_badge)}</span>
        </div>
      </div>"""
    else:
        pick_actions_html = f"""
      <div class="pick-actions aftr-actions">
        <button type="button" class="btn-favorite-pick pill pick-action-btn"
          data-pick-id="{pick_id_attr}" data-market="{market_attr}" data-aftr-score="{aftr_score_val}"
          data-tier="{html_lib.escape(tier)}" data-edge="{edge_attr}"
          data-home-team="{home_team_attr}" data-away-team="{away_team_attr}">⭐ Guardar</button>
        <button type="button" class="btn-follow-pick pill pick-action-btn pick-action-follow"
          data-pick-id="{pick_id_attr}" data-market="{market_attr}" data-aftr-score="{aftr_score_val}"
          data-tier="{html_lib.escape(tier)}" data-edge="{edge_attr}"
          data-home-team="{home_team_attr}" data-away-team="{away_team_attr}">📈 Seguir pick</button>
      </div>"""
    mainpick_html = ""
    if not is_finished:
        mainpick_html = f"""
      <div class="aftr-mainpick pick-main-highlight">
        <div class="aftr-market">{html_lib.escape(str(best_market))}</div>
        {prob_odds_html}
      </div>"""

    # Visual hierarchy:
    # - Prediction (market + prob) is primary.
    # - Result badge (finished) comes right below prediction.
    # - AFTR score is secondary (demoted) and shown after prediction/buttons.
    score_and_actions_html = ""
    if is_finished:
        score_and_actions_html = pick_actions_html + aftr_block_html
    else:
        score_and_actions_html = aftr_block_html + pick_actions_html

    front_html = f"""
    <div class="{card_class}">
      <div class="aftr-topmeta">
        <span class="aftr-meta-league">{html_lib.escape(league_label)}</span>
        <span class="{meta_time_class}">{html_lib.escape(kickoff_time)}</span>
        {tier_meta_badge_html}
      </div>
      {teams_html}
      {mainpick_html}
      {score_and_actions_html}
    </div>
    """

    market_for_back = (best or {}).get("market") or p.get("best_market") or ""
    back_html = _render_back_stats(p, market_for_back)

    return f"""
    <div class="flip-card" role="button" tabindex="0" aria-label="Ver stats" data-pick-id="{pick_id_attr}">
      <div class="flip-inner">
        <div class="flip-front">{front_html}</div>
        <div class="flip-back">{back_html}</div>
      </div>
    </div>
    """


@router.get("/ui", response_class=HTMLResponse, include_in_schema=False)
def ui_same(request: Request, league: str = Query(settings.default_league)):
    return dashboard(request, league)

# =========================================================
# Combos renderer
# =========================================================
def _uniq_combos(combos: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for c in combos or []:
        if not isinstance(c, dict):
            continue
        legs = c.get("legs") or []
        sig = "|".join(
            f"{(x.get('home') or '').strip()}-{(x.get('away') or '').strip()}:{(x.get('market') or '').strip()}"
            for x in legs if isinstance(x, dict)
        )
        if not sig:
            continue
        if sig in seen:
            continue
        seen.add(sig)
        out.append(c)
    return out

def _leg_sig(it: dict) -> str:
    if not isinstance(it, dict):
        return ""
    mid = it.get("match_id") or it.get("id") or ""
    mkt = (it.get("market") or "").strip().upper()
    return f"{mid}:{mkt}"

def _combo_sig(combo: dict) -> str:
    """Firma estable: mismos partidos+mercados => mismo combo."""
    if not isinstance(combo, dict):
        return ""
    legs = combo.get("legs") or []
    if not isinstance(legs, list):
        return ""
    parts = []
    for it in legs:
        if not isinstance(it, dict):
            continue
        mid = it.get("match_id") or it.get("id") or ""
        mkt = (it.get("market") or "").strip().upper()
        parts.append(f"{mid}:{mkt}")
    return "|".join(sorted(parts))


def _build_combo_of_the_day(
    upcoming_picks: list[dict],
    match_by_id: dict[Any, dict],
    match_key_fn: Callable[[dict], Any] | None = None,
) -> dict | None:
    """
    Build one combo from daily picks: up to 3 legs, AFTR Score >= 75, confidence >= 6,
    positive edge when available, at most one pick per match. Returns None if not enough valid picks.
    match_key_fn: optional; if provided, used to dedupe by (e.g. (league, match_id)) for global combo.
    match_by_id: dict keyed by int (league page) or by match_key_fn return (home page).
    """
    get_key = match_key_fn if match_key_fn is not None else (lambda p: _safe_int(p.get("match_id")) or _safe_int(p.get("id")))

    # Filter: strong enough and positive edge when edge present
    def _valid(p: dict) -> bool:
        if _aftr_score(p) < 75:
            return False
        if _safe_int(p.get("confidence"), 0) < 6:
            return False
        edge_val = p.get("edge")
        if edge_val is not None:
            try:
                if float(edge_val) <= 0:
                    return False
            except (TypeError, ValueError):
                return False
        return True

    candidates = [p for p in (upcoming_picks or []) if isinstance(p, dict) and _valid(p)]
    candidates.sort(key=lambda p: -_pick_score(p))

    used_match_keys: set[Any] = set()
    legs: list[dict] = []
    for p in candidates:
        if len(legs) >= 3:
            break
        key = get_key(p)
        if key is None or key in used_match_keys:
            continue
        m = (match_by_id or {}).get(key)
        mid = _safe_int(p.get("match_id")) or _safe_int(p.get("id"))
        home = p.get("home") or (m.get("home") if isinstance(m, dict) else "") or "—"
        away = p.get("away") or (m.get("away") if isinstance(m, dict) else "") or "—"
        market = (p.get("best_market") or "").strip() or "—"
        prob = _safe_float(p.get("best_prob"), 0)
        leg_entry: dict = {
            "home": home,
            "away": away,
            "market": market,
            "prob": prob,
            "odds_decimal": p.get("odds_decimal"),
            "home_crest": p.get("home_crest") or (m.get("home_crest") if isinstance(m, dict) else "") or "",
            "away_crest": p.get("away_crest") or (m.get("away_crest") if isinstance(m, dict) else "") or "",
            "match_id": mid,
            "utcDate": p.get("utcDate") or (m.get("utcDate") if isinstance(m, dict) else None),
        }
        if p.get("_league") is not None:
            leg_entry["_league"] = p.get("_league")
        legs.append(leg_entry)
        used_match_keys.add(key)

    if len(legs) < 2:
        return None

    # Combined odds (multiply when all legs have odds_decimal)
    combined_odds: float | None = None
    for leg in legs:
        od = leg.get("odds_decimal")
        if od is None:
            combined_odds = None
            break
        try:
            o = float(od)
            if combined_odds is None:
                combined_odds = o
            else:
                combined_odds *= o
        except (TypeError, ValueError):
            combined_odds = None
            break

    # Combined probability (product of leg probs)
    combined_prob = 1.0
    for leg in legs:
        combined_prob *= leg.get("prob") or 0
    combo_prob_pct = round(combined_prob * 100, 1)

    # Combo score 0–100: average of AFTR scores of the picks we used
    scores_for_legs = []
    for leg in legs:
        mid = leg.get("match_id")
        leg_league = leg.get("_league")
        prob = leg.get("prob") or 0
        for p in candidates:
            if leg_league is not None:
                if p.get("_league") == leg_league and (_safe_int(p.get("match_id")) == mid or _safe_int(p.get("id")) == mid):
                    scores_for_legs.append(_aftr_score(p))
                    break
            elif _safe_int(p.get("match_id")) == mid or _safe_int(p.get("id")) == mid:
                scores_for_legs.append(_aftr_score(p))
                break
        else:
            scores_for_legs.append(min(100, int(prob * 100)))
    combo_score = int(round(sum(scores_for_legs) / len(scores_for_legs))) if scores_for_legs else 0
    combo_score = max(0, min(100, combo_score))

    # Risk: Safe / Medium / Aggressive by combined probability
    if combined_prob >= 0.20:
        risk = "Safe"
    elif combined_prob >= 0.10:
        risk = "Medium"
    else:
        risk = "Aggressive"

    return {
        "legs": legs,
        "combo_prob_pct": combo_prob_pct,
        "combined_odds": combined_odds,
        "risk": risk,
        "combo_score": combo_score,
    }


def _build_combos_by_tier(
    upcoming_picks: list[dict],
    match_by_id: dict[Any, dict],
    match_key_fn: Callable[[dict], Any] | None = None,
    max_combos: int = 3,
) -> list[dict]:
    """
    Build up to max_combos (default 3) combos for home page: SAFE, MEDIUM, AGGRESSIVE.
    Each combo uses different matches; tiers are assigned by combined probability.
    Returns list of combo dicts (each with risk already set).
    """
    get_key = match_key_fn if match_key_fn is not None else (
        lambda p: _safe_int(p.get("match_id")) or _safe_int(p.get("id"))
    )

    def _valid(p: dict) -> bool:
        if _aftr_score(p) < 75:
            return False
        if _safe_int(p.get("confidence"), 0) < 6:
            return False
        edge_val = p.get("edge")
        if edge_val is not None:
            try:
                if float(edge_val) <= 0:
                    return False
            except (TypeError, ValueError):
                return False
        return True

    candidates = [p for p in (upcoming_picks or []) if isinstance(p, dict) and _valid(p)]
    candidates.sort(key=lambda p: -_pick_score(p))

    combos: list[dict] = []
    used_match_keys: set[Any] = set()

    for _ in range(max_combos):
        legs: list[dict] = []
        for p in candidates:
            if len(legs) >= 3:
                break
            key = get_key(p)
            if key is None or key in used_match_keys:
                continue
            m = (match_by_id or {}).get(key)
            mid = _safe_int(p.get("match_id")) or _safe_int(p.get("id"))
            home = p.get("home") or (m.get("home") if isinstance(m, dict) else "") or "—"
            away = p.get("away") or (m.get("away") if isinstance(m, dict) else "") or "—"
            market = (p.get("best_market") or "").strip() or "—"
            prob = _safe_float(p.get("best_prob"), 0)
            leg_entry: dict = {
                "home": home,
                "away": away,
                "market": market,
                "prob": prob,
                "odds_decimal": p.get("odds_decimal"),
                "home_crest": p.get("home_crest") or (m.get("home_crest") if isinstance(m, dict) else "") or "",
                "away_crest": p.get("away_crest") or (m.get("away_crest") if isinstance(m, dict) else "") or "",
                "match_id": mid,
                "utcDate": p.get("utcDate") or (m.get("utcDate") if isinstance(m, dict) else None),
            }
            if p.get("_league") is not None:
                leg_entry["_league"] = p.get("_league")
            legs.append(leg_entry)
            used_match_keys.add(key)

        if len(legs) < 2:
            break

        combined_odds: float | None = None
        for leg in legs:
            od = leg.get("odds_decimal")
            if od is None:
                combined_odds = None
                break
            try:
                o = float(od)
                combined_odds = o if combined_odds is None else combined_odds * o
            except (TypeError, ValueError):
                combined_odds = None
                break

        combined_prob = 1.0
        for leg in legs:
            combined_prob *= leg.get("prob") or 0
        combo_prob_pct = round(combined_prob * 100, 1)

        scores_for_legs = []
        for leg in legs:
            mid = leg.get("match_id")
            leg_league = leg.get("_league")
            for p in candidates:
                if leg_league is not None:
                    if p.get("_league") == leg_league and (_safe_int(p.get("match_id")) == mid or _safe_int(p.get("id")) == mid):
                        scores_for_legs.append(_aftr_score(p))
                        break
                elif _safe_int(p.get("match_id")) == mid or _safe_int(p.get("id")) == mid:
                    scores_for_legs.append(_aftr_score(p))
                    break
            else:
                scores_for_legs.append(min(100, int((leg.get("prob") or 0) * 100)))
        combo_score = int(round(sum(scores_for_legs) / len(scores_for_legs))) if scores_for_legs else 0
        combo_score = max(0, min(100, combo_score))

        if combined_prob >= 0.20:
            risk = "Safe"
        elif combined_prob >= 0.10:
            risk = "Medium"
        else:
            risk = "Aggressive"

        combos.append({
            "legs": legs,
            "combo_prob_pct": combo_prob_pct,
            "combined_odds": combined_odds,
            "risk": risk,
            "combo_score": combo_score,
        })

    return combos


def _combo_match_key_for_home(p: dict) -> tuple[str, int] | None:
    """Global match key for home combo dedupe: (league, match_id)."""
    if not isinstance(p, dict):
        return None
    league = (p.get("_league") or p.get("league") or "").strip()
    if not league:
        return None
    mid = _safe_int(p.get("match_id")) or _safe_int(p.get("id"))
    if mid is None:
        return None
    return (league, int(mid))


def _combo_leg_odds_value(p: dict) -> float | None:
    """Leg odds for combo math (approx): odds_decimal → best_fair → 1/best_prob."""
    if not isinstance(p, dict):
        return None
    od = p.get("odds_decimal")
    if od is not None:
        try:
            return float(od)
        except (TypeError, ValueError):
            pass
    bf = p.get("best_fair")
    if bf is not None:
        try:
            return float(bf)
        except (TypeError, ValueError):
            pass
    bp = p.get("best_prob")
    bp = _safe_float(bp)
    if bp is not None and bp > 0:
        try:
            return 1.0 / float(bp)
        except (TypeError, ValueError):
            return None
    return None


def _build_home_premium_combos(
    upcoming_picks: list[dict],
    match_by_key: dict[Any, dict],
    *,
    log_context: str | None = None,
) -> list[dict]:
    """
    Build exactly 3 premium combo cards with explicit roles:
      1) Combo del Día (today only, 3-5 legs)
      2) Combo 72h (today..+3 days, 3-5 legs)
      3) Combo Value (today..+3 days, 3-5 legs; prioritize edge/value)

    Dedupe:
      - never repeat the same match inside a combo
      - avoid repeating across combos when possible; if not enough inventory, allow cross-combo duplicates
        but still avoid duplicates within each combo.

    If strict filters yield no filled combos but picks exist, retries with relaxed thresholds and logs why.
    """
    today_local = datetime.now(AFTR_DISPLAY_TZ).date()
    upcoming = [p for p in (upcoming_picks or []) if isinstance(p, dict)]
    ctx = log_context or ""

    def _valid_strict(p: dict) -> bool:
        if _aftr_score(p) < 75:
            return False
        if _safe_int(p.get("confidence"), 0) < 6:
            return False
        edge_val = p.get("edge")
        if edge_val is not None:
            try:
                if float(edge_val) <= 0:
                    return False
            except (TypeError, ValueError):
                return False
        return True

    def _valid_relaxed(p: dict) -> bool:
        """Still quality-biased, but allows neutral/negative edge and slightly lower bars."""
        if _aftr_score(p) < 62:
            return False
        if _safe_int(p.get("confidence"), 0) < 5:
            return False
        return True

    def _in_local_window(p: dict, start_day: int, end_day: int) -> bool:
        d = _pick_local_date(p, match_by_key)
        if d is None:
            return False
        return (today_local + timedelta(days=start_day)) <= d <= (today_local + timedelta(days=end_day))

    def _target_leg_count(candidates: list[dict]) -> int:
        uniq = set()
        for p in candidates:
            k = _combo_match_key_for_home(p)
            if k:
                uniq.add(k)
        if len(uniq) >= 5:
            return 5
        if len(uniq) >= 4:
            return 4
        return 3

    def _select_legs(candidates: list[dict], target_count: int, global_used: set[tuple[str, int]]) -> list[dict]:
        legs: list[dict] = []
        used_local: set[tuple[str, int]] = set()

        for p in candidates:
            k = _combo_match_key_for_home(p)
            if not k or k in used_local or k in global_used:
                continue
            legs.append(p)
            used_local.add(k)
            if len(legs) >= target_count:
                break

        if len(legs) < 3:
            for p in candidates:
                k = _combo_match_key_for_home(p)
                if not k or k in used_local:
                    continue
                legs.append(p)
                used_local.add(k)
                if len(legs) >= target_count:
                    break
        return legs

    def _assemble(valid_fn) -> list[dict]:
        all_candidates = [p for p in upcoming if valid_fn(p)]

        def _pool(start_day: int, end_day: int) -> list[dict]:
            return [p for p in all_candidates if _in_local_window(p, start_day, end_day)]

        pools = {
            "day": _pool(0, 0),
            "72h": _pool(0, 3),
            "value": _pool(0, 3),
        }
        day_sorted = sorted(pools["day"], key=lambda p: -_pick_score(p))
        h72_sorted = sorted(pools["72h"], key=lambda p: -_pick_score(p))
        value_sorted = sorted(
            pools["value"],
            key=lambda p: (
                -float(_safe_float(p.get("edge"), 0) or 0),
                -_aftr_score(p),
                -_pick_score(p),
            ),
        )
        combo_specs = [
            {
                "key": "day",
                "title": "🔥 Combo del Día",
                "description": "Las mejores selecciones de hoy",
                "tier_badge": "Seguro",
                "tier_class": "seguro",
                "sorted": day_sorted,
            },
            {
                "key": "72h",
                "title": "⏳ Combo 72h",
                "description": "Ventana ampliada de oportunidades",
                "tier_badge": "Balanceado",
                "tier_class": "balanceado",
                "sorted": h72_sorted,
            },
            {
                "key": "value",
                "title": "💎 Combo Value",
                "description": "Más edge, más cuota, más riesgo controlado",
                "tier_badge": "Value",
                "tier_class": "value",
                "sorted": value_sorted,
            },
        ]
        global_used: set[tuple[str, int]] = set()
        combos: list[dict] = []
        for spec in combo_specs:
            candidates = spec.get("sorted") or []
            target_count = _target_leg_count(candidates)
            legs_picks = _select_legs(candidates, target_count, global_used)
            if len(legs_picks) < 3:
                combos.append(
                    {
                        **spec,
                        "legs": [],
                        "combined_odds": None,
                        "combo_prob_pct": None,
                        "combo_score": None,
                    }
                )
                continue

            for lp in legs_picks:
                k = _combo_match_key_for_home(lp)
                if k:
                    global_used.add(k)

            legs: list[dict] = []
            combined_prob = 1.0
            combined_odds: float | None = None
            odds_ok = True
            score_sum = 0

            for lp in legs_picks:
                mid = _safe_int(lp.get("match_id")) or _safe_int(lp.get("id"))
                league_code = (lp.get("_league") or lp.get("league") or "").strip()
                m = match_by_key.get((league_code, mid)) if mid is not None and league_code else None
                home = lp.get("home") or (m.get("home") if isinstance(m, dict) else "") or "—"
                away = lp.get("away") or (m.get("away") if isinstance(m, dict) else "") or "—"
                market = (lp.get("best_market") or lp.get("best_market_name") or "").strip() or "—"
                home_crest = lp.get("home_crest") or (m.get("home_crest") if isinstance(m, dict) else None)
                away_crest = lp.get("away_crest") or (m.get("away_crest") if isinstance(m, dict) else None)

                prob = _safe_float(lp.get("best_prob"), 0) or 0.0
                leg_odds = _combo_leg_odds_value(lp)
                aftr_sc = lp.get("aftr_score")
                if aftr_sc is None:
                    aftr_sc = _aftr_score(lp)
                try:
                    aftr_sc_int = int(round(float(aftr_sc)))
                except (TypeError, ValueError):
                    aftr_sc_int = _aftr_score(lp)

                legs.append(
                    {
                        "home": home,
                        "away": away,
                        "market": market,
                        "prob": prob,
                        "home_crest": home_crest,
                        "away_crest": away_crest,
                        "match_id": mid,
                        "odds_value": leg_odds,
                        "aftr_score": aftr_sc_int,
                        "utcDate": lp.get("utcDate") or (m.get("utcDate") if isinstance(m, dict) else None),
                    }
                )

                combined_prob *= prob
                score_sum += aftr_sc_int
                if leg_odds is None:
                    odds_ok = False
                else:
                    if combined_odds is None:
                        combined_odds = float(leg_odds)
                    else:
                        combined_odds *= float(leg_odds)

            combo_prob_pct = round(combined_prob * 100, 1)
            combo_score = int(round(score_sum / max(1, len(legs))))
            if not odds_ok:
                combined_odds = None

            combos.append(
                {
                    **spec,
                    "legs": legs,
                    "combo_prob_pct": combo_prob_pct,
                    "combined_odds": combined_odds,
                    "combo_score": combo_score,
                }
            )
        return combos

    combos = _assemble(_valid_strict)
    filled = sum(1 for c in combos if len(c.get("legs") or []) >= 3)

    if filled == 0 and upcoming:
        n_strict = len([p for p in upcoming if _valid_strict(p)])
        n_relaxed = len([p for p in upcoming if _valid_relaxed(p)])
        no_key = sum(1 for p in upcoming if _combo_match_key_for_home(p) is None)
        no_date = sum(1 for p in upcoming if _pick_local_date(p, match_by_key) is None)
        in_day = sum(1 for p in upcoming if _in_local_window(p, 0, 0))
        in_72 = sum(1 for p in upcoming if _in_local_window(p, 0, 3))
        logger.warning(
            "premium_combos[%s]: 0 filled combos | upcoming=%s strict_ok=%s relaxed_ok=%s "
            "no_match_key=%s no_local_date=%s in_today_window=%s in_72h_window=%s",
            ctx or "—",
            len(upcoming),
            n_strict,
            n_relaxed,
            no_key,
            no_date,
            in_day,
            in_72,
        )

    if filled == 0 and len(upcoming) >= 3 and n_relaxed >= 3:
        combos_r = _assemble(_valid_relaxed)
        if sum(1 for c in combos_r if len(c.get("legs") or []) >= 3) > 0:
            logger.warning(
                "premium_combos[%s]: using RELAXED thresholds (AFTR≥62, CONF≥5, any edge)",
                ctx or "—",
            )
            return combos_r

    return combos


def _render_home_premium_combo_card(combo: dict) -> str:
    """Render one of the 3 home premium combo cards."""
    if not combo or not isinstance(combo, dict):
        return ""
    title = combo.get("title") or "Combo"
    description = combo.get("description") or ""
    tier_badge = combo.get("tier_badge") or "—"
    tier_class = combo.get("tier_class") or "seguro"
    legs = combo.get("legs") or []
    n = len(legs)

    prob_pct = combo.get("combo_prob_pct")
    prob_str = f"{prob_pct:.1f}%" if prob_pct is not None else "—"
    combined_odds = combo.get("combined_odds")
    odds_str = f"~{combined_odds:.2f}" if combined_odds is not None else "—"
    score = combo.get("combo_score")
    score_str = str(score) if score is not None else "—"

    if n == 0:
        return f"""
    <div class="card combo-card home-premium-combo-card">
      <div class="combo-head">
        <div class="combo-title">{html_lib.escape(title)}</div>
        <span class="combo-tier home-combo-tier home-combo-tier--{html_lib.escape(tier_class)}">{html_lib.escape(tier_badge)}</span>
      </div>
      <div class="combo-sub">
        <div class="home-combo-desc">{html_lib.escape(description)}</div>
        <div class="home-combo-empty muted">No hay inventario suficiente.</div>
      </div>
    </div>"""

    rows = []
    for it in legs:
        if not isinstance(it, dict):
            continue
        home = it.get("home") or "—"
        away = it.get("away") or "—"
        market = it.get("market") or "—"
        prob = _safe_float(it.get("prob"), 0) or 0.0
        p_pct = round(prob * 100, 0)
        home_part = _team_with_crest(it.get("home_crest"), home)
        away_part = _team_with_crest(it.get("away_crest"), away)
        rows.append(
            f"""
          <div class="combo-leg">
            <div class="combo-leg-top">
              <span class="combo-match">
                {home_part}
                <span class="vs">vs</span>
                {away_part}
              </span>
              <span class="combo-pct">{p_pct:.0f}%</span>
            </div>
            <div class="combo-market">{html_lib.escape(str(market))}</div>
            {_combo_leg_kickoff_html(it)}
          </div>
        """
        )

    return f"""
    <div class="card combo-card home-premium-combo-card">
      <div class="combo-head">
        <div class="combo-title">{html_lib.escape(title)}</div>
        <span class="combo-tier home-combo-tier home-combo-tier--{html_lib.escape(tier_class)}">{html_lib.escape(tier_badge)}</span>
      </div>
      <div class="combo-sub">
        <div class="home-combo-desc">{html_lib.escape(description)}</div>
        <div class="home-combo-stats muted">
          <span><b>{n}</b> selecciones</span>
          <span>Cuota total: <b>{html_lib.escape(odds_str)}</b></span>
          <span>Prob: <b>{html_lib.escape(prob_str)}</b></span>
          <span>AFTR combo: <b>{html_lib.escape(score_str)}</b></span>
        </div>
      </div>
      <div class="combo-legs">
        {''.join(rows)}
      </div>
    </div>
    """


def _render_combo_of_the_day(combo: dict) -> str:
    """Render the Combo of the Day section (same style as combo-card)."""
    if not combo or not isinstance(combo, dict):
        return ""
    legs = combo.get("legs") or []
    if not legs:
        return ""
    risk = html_lib.escape(str(combo.get("risk") or "—"))
    score = combo.get("combo_score")
    score_str = str(score) if score is not None else "—"
    prob_pct = combo.get("combo_prob_pct")
    prob_str = f"{prob_pct}%" if prob_pct is not None else "—"
    combined_odds = combo.get("combined_odds")
    odds_str = f" • Odds combinadas: {combined_odds:.2f}" if combined_odds is not None else ""

    rows = []
    for it in legs:
        if not isinstance(it, dict):
            continue
        home = it.get("home") or "—"
        away = it.get("away") or "—"
        market = it.get("market") or "—"
        p = round(float(it.get("prob") or 0) * 100, 0)
        home_part = _team_with_crest(it.get("home_crest"), home)
        away_part = _team_with_crest(it.get("away_crest"), away)
        rows.append(f"""
          <div class="combo-leg">
            <div class="combo-leg-top">
              <span class="combo-match">
                {home_part}
                <span class="vs">vs</span>
                {away_part}
              </span>
              <span class="combo-pct">{p:.0f}%</span>
            </div>
            <div class="combo-market">{html_lib.escape(str(market))}</div>
            {_combo_leg_kickoff_html(it)}
          </div>
        """)

    return f"""
    <div class="card combo-card combo-of-the-day">
      <div class="combo-head">
        <div class="combo-title">🔥 AFTR Combo del Día</div>
        <span class="combo-tier {risk.lower()}">{risk}</span>
      </div>
      <div class="combo-sub">Prob total: <b>{prob_str}</b>{odds_str} • Puntuación combo: <b>{score_str}</b></div>
      <div class="combo-legs">
        {''.join(rows)}
      </div>
    </div>
    """


def _render_combo_card(combo: dict | None, tier_label: str) -> str:
    """Render one combo card for home page (SAFE / MEDIUM / AGGRESSIVE slot)."""
    tier_lower = tier_label.lower()
    if not combo or not isinstance(combo, dict):
        return f'''
    <div class="card combo-card combo-card-slot combo-tier-{tier_lower}">
      <div class="combo-head">
        <div class="combo-title">{html_lib.escape(tier_label)}</div>
        <span class="combo-tier {tier_lower}">{html_lib.escape(tier_label)}</span>
      </div>
      <div class="combo-empty muted">No {html_lib.escape(tier_label)} combo today.</div>
    </div>'''
    legs = combo.get("legs") or []
    risk = html_lib.escape(str(combo.get("risk") or tier_label))
    score = combo.get("combo_score")
    score_str = str(score) if score is not None else "—"
    prob_pct = combo.get("combo_prob_pct")
    prob_str = f"{prob_pct}%" if prob_pct is not None else "—"
    combined_odds = combo.get("combined_odds")
    odds_str = f" • Odds {combined_odds:.2f}" if combined_odds is not None else ""

    rows = []
    for it in legs:
        if not isinstance(it, dict):
            continue
        home = it.get("home") or "—"
        away = it.get("away") or "—"
        market = it.get("market") or "—"
        p = round(float(it.get("prob") or 0) * 100, 0)
        home_part = _team_with_crest(it.get("home_crest"), home)
        away_part = _team_with_crest(it.get("away_crest"), away)
        rows.append(f"""
          <div class="combo-leg">
            <div class="combo-leg-top">
              <span class="combo-match">
                {home_part}
                <span class="vs">vs</span>
                {away_part}
              </span>
              <span class="combo-pct">{p:.0f}%</span>
            </div>
            <div class="combo-market">{html_lib.escape(str(market))}</div>
            {_combo_leg_kickoff_html(it)}
          </div>
        """)

    return f"""
    <div class="card combo-card combo-card-slot combo-tier-{tier_lower}">
      <div class="combo-head">
        <div class="combo-title">{html_lib.escape(tier_label)}</div>
        <span class="combo-tier {tier_lower}">{risk}</span>
      </div>
      <div class="combo-sub">Prob: <b>{prob_str}</b>{odds_str} • AFTR score: <b>{score_str}</b></div>
      <div class="combo-legs">
        {''.join(rows)}
      </div>
    </div>
    """


def _render_combo_box(combo: dict) -> str:
    if not isinstance(combo, dict):
        return ""

    legs = combo.get("legs") or []
    if not isinstance(legs, list) or not legs:
        return "<div class='muted'>No hay combinada disponible.</div>"

    tier = html_lib.escape(str(combo.get("tier") or "—"))
    name = html_lib.escape(str(combo.get("name") or "Combinada"))
    prob = html_lib.escape(str(combo.get("combo_prob_pct") or "—"))

    fair = combo.get("fair")
    fair_txt = f" • cuota ~ {html_lib.escape(str(fair))}" if fair is not None else ""

    rows = []
    for it in legs:
        if not isinstance(it, dict):
            continue
        home = it.get("home") or "—"
        away = it.get("away") or "—"
        market = it.get("market") or "—"
        p = round(float(it.get("prob") or 0) * 100, 0)
        home_part = _team_with_crest(it.get("home_crest"), home)
        away_part = _team_with_crest(it.get("away_crest"), away)
        rows.append(f"""
          <div class="combo-leg">
            <div class="combo-leg-top">
              <span class="combo-match">
                {home_part}
                <span class="vs">vs</span>
                {away_part}
              </span>
              <span class="combo-pct">{p:.0f}%</span>
            </div>
            <div class="combo-market">{html_lib.escape(str(market))}</div>
            {_combo_leg_kickoff_html(it)}
          </div>
        """)

    return f"""
    <div class="card combo-card">
      <div class="combo-head">
        <div class="combo-title">{name}</div>
        <span class="combo-tier {tier.lower()}">{tier}</span>
      </div>
      <div class="combo-sub">Prob total: <b>{prob}%</b>{fair_txt}</div>
      <div class="combo-legs">
        {''.join(rows)}
      </div>
    </div>
    """

# Featured leagues for home page (cards + big matches)
FEATURED_LEAGUE_CODES = ["PL", "CL", "PD", "SA", "NBA"]

# Manual league logo mapping: static images in /static/leagues/. If file is missing, onerror shows initial letter.
LEAGUE_LOGO_PATHS = {
    "PL": "/static/leagues/pl.png",
    "CL": "/static/leagues/cl.png",
    "PD": "/static/leagues/pd.png",
    "SA": "/static/leagues/sa.png",
    "NBA": "/static/leagues/nba.png",
}
# Optional fallback image when league PNG is missing (else CSS fallback with initial is used)
LEAGUE_LOGO_FALLBACK_PATH = "/static/leagues/fallback.svg"

# Team logos: /static/teams/{slug}.png; slug from team name. Fallback when missing.
TEAM_LOGO_FALLBACK_PATH = "/static/teams/default.svg"


def _team_logo_slug(name: str) -> str:
    """Normalize team name to a slug for static logo path: lowercase, spaces to hyphens, remove accents.
    E.g. 'Eintracht Frankfurt' -> 'eintracht-frankfurt'."""
    if not name or not isinstance(name, str):
        return ""
    # Remove accents: NFD decomposes, then drop combining characters
    s = unicodedata.normalize("NFD", name)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = "".join(c for c in s if c.isalnum() or c in " -")
    s = s.strip().replace(" ", "-").lower()
    while "--" in s:
        s = s.replace("--", "-")
    return s.strip("-")


def _team_logo_path(team_name: str) -> str:
    """Return static path for team logo; use fallback if slug empty."""
    slug = _team_logo_slug(team_name)
    if not slug:
        return TEAM_LOGO_FALLBACK_PATH
    return f"/static/teams/{slug}.png"


# Home page top nav: (code, display_name). Uses LEAGUE_LOGO_PATHS; fallback = initial in circle.
HOME_NAV_LEAGUES = [
    ("PL", "Premier League"),
    ("CL", "UEFA Champions League"),
    ("PD", "LaLiga"),
    ("SA", "Serie A"),
    ("NBA", "NBA"),
]


def _debug_log_live_match_candidates(league_code: str, matches: list[dict]) -> None:
    """
    Temporary diagnostics: kickoff in the past, not explicitly final — log raw status fields vs isMatchLive.
    Enable: AFTR_LIVE_DEBUG=1
    """
    if os.getenv("AFTR_LIVE_DEBUG", "").strip().lower() not in ("1", "true", "yes", "on"):
        return
    now = datetime.now(timezone.utc)
    fin_like = frozenset(
        {"FINISHED", "FINAL", "FT", "AWARDED", "CANCELLED", "POSTPONED", "SETTLED", "FINALIZADO"},
    )
    for m in matches:
        if not isinstance(m, dict):
            continue
        st = _match_live_status_token(m)
        if st in fin_like:
            continue
        dt = _parse_utcdate_maybe(m.get("utcDate"))
        if dt is None or dt > now:
            continue
        raw_bundle: dict[str, Any] = {
            "status": m.get("status"),
            "match_status": m.get("match_status"),
            "state": m.get("state"),
            "minute": m.get("minute"),
            "elapsed": m.get("elapsed"),
            "time_elapsed": m.get("time_elapsed"),
            "match_minute": m.get("match_minute"),
            "live": m.get("live"),
        }
        fx = m.get("fixture")
        if isinstance(fx, dict):
            raw_bundle["fixture.status"] = fx.get("status")
        logger.info(
            "AFTR_LIVE_DEBUG league=%s home=%s away=%s kickoff=%s status_fields=%s token=%s isMatchLive=%s isMatchFinished=%s",
            league_code,
            m.get("home"),
            m.get("away"),
            m.get("utcDate"),
            raw_bundle,
            st,
            isMatchLive(m),
            isMatchFinished(m),
        )


def _load_all_leagues_data(
    league_codes: list[str] | None = None,
) -> tuple[list[dict], dict[Any, dict], list[dict], list[dict], dict[str, list[dict]], dict[str, list[dict]]]:
    """
    Load picks and matches for all (or given) leagues. Returns:
    - all_picks: every pick with _league set
    - match_by_key: (league, match_id) -> match dict for global combo
    - all_settled: picks with result WIN/LOSS/PUSH
    - all_upcoming: picks with result PENDING
    - picks_by_league: league_code -> list of picks (with _league)
    - matches_by_league: league_code -> list of matches
    """
    codes = league_codes or list(settings.leagues.keys())
    all_picks: list[dict] = []
    match_by_key: dict[Any, dict] = {}
    picks_by_league: dict[str, list[dict]] = {}
    matches_by_league: dict[str, list[dict]] = {}

    for code in codes:
        raw_matches = read_json_with_fallback(f"daily_matches_{code}.json") or []
        raw_picks = read_json_with_fallback(f"daily_picks_{code}.json") or []
        if not isinstance(raw_matches, list):
            raw_matches = []
        if not isinstance(raw_picks, list):
            raw_picks = []
        matches = [m for m in raw_matches if isinstance(m, dict)]
        picks = [p for p in raw_picks if isinstance(p, dict)]

        logger.info(
            "load_all_leagues: %s raw_matches=%s raw_picks=%s",
            code, len(matches), len(picks),
        )

        for m in matches:
            mid = _safe_int(m.get("match_id") or m.get("id"))
            if mid is not None:
                match_by_key[(code, mid)] = m
        matches_by_league[code] = matches
        _debug_log_live_match_candidates(code, matches)

        filtered_for_league: list[dict] = []
        for p in picks:
            p = dict(p)
            p["_league"] = code
            if _is_pick_valid(p):
                filtered_for_league.append(p)
        if len(picks) > 0 and len(filtered_for_league) == 0:
            logger.warning(
                "load_all_leagues: %s had %s raw picks but 0 after _is_pick_valid; using raw picks as fallback",
                code, len(picks),
            )
            for p in picks:
                p = dict(p)
                p["_league"] = code
                filtered_for_league.append(p)
        logger.info("load_all_leagues: %s after filter (or fallback) picks=%s", code, len(filtered_for_league))

        for p in filtered_for_league:
            all_picks.append(p)
            picks_by_league.setdefault(code, []).append(p)

    all_settled: list[dict] = []
    all_upcoming: list[dict] = []
    for p in all_picks:
        league_code = (p.get("_league") or p.get("league") or "").strip()
        mid = _safe_int(p.get("match_id") or p.get("id"))
        match_obj = match_by_key.get((league_code, mid)) if league_code and mid is not None else None
        finished = isMatchFinished(p) or (isMatchFinished(match_obj) if isinstance(match_obj, dict) else False)
        if finished:
            all_settled.append(p)
        else:
            all_upcoming.append(p)
    logger.info(
        "load_all_leagues: total all_picks=%s all_settled=%s all_upcoming=%s leagues_with_picks=%s",
        len(all_picks), len(all_settled), len(all_upcoming), list(picks_by_league.keys()),
    )

    return all_picks, match_by_key, all_settled, all_upcoming, picks_by_league, matches_by_league


@router.get("/", response_class=HTMLResponse)
def index_or_league(request: Request, league: str | None = Query(None)):
    """Show global home when no league query; otherwise show league dashboard."""
    if league is None or (isinstance(league, str) and league.strip() == ""):
        return home_page(request)
    return dashboard(request, league.strip())


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
            <form action="/auth/login" method="post" onsubmit="console.log('FORM SUBMIT');">
              <input type="email" name="email" required>
              <input type="password" name="password" required>
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
            <div class="home-hero-kpi"><span>ROI GLOBAL</span><strong>{roi_str}</strong></div>
            <div class="home-hero-kpi"><span>GANANCIA NETA</span><strong>{net:+.1f}u</strong></div>
            <div class="home-hero-kpi"><span>TASA DE ACIERTO</span><strong>{winrate_str}</strong></div>
            <div class="home-hero-kpi"><span>PICKS TOTALES</span><strong>{total_picks}</strong></div>
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
      <div class="home-picks-grid">
        {''.join(top_pick_cards) if top_pick_cards else '<p class="home-empty muted">No hay picks pendientes.</p>'}
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
        <h2 class="home-h2 perf-panel-title">Rendimiento AFTR</h2>
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


def dashboard(request: Request, league: str):
    league = league if settings.is_valid_league(league) else settings.default_league
    unsupported = get_unsupported_leagues()
    unsupported_football = {c for c in unsupported if getattr(settings, "league_sport", {}).get(c) != "basketball"}
    if league in unsupported_football:
        league = settings.default_league

    auth_param = (request.query_params.get("auth") or "").strip().lower()
    signup_modal_style = "display:flex" if auth_param == "register" else "display:none"
    login_modal_style = "display:flex" if auth_param == "login" else "display:none"

    uid = get_user_id(request)
    user = get_user_by_id(uid) if uid else None
    if uid and not user:
        # broken cookie: uid not in DB; treat as logged out (middleware clears cookie)
        uid, user = None, None

    auth_html = ""
    if user:
        display_name = html_lib.escape((user.get("username") or user.get("email") or ""))
        auth_html = (
            f'<span class="plan-badge">{display_name}</span>'
            f'<a class="plan-logout" href="/account">Mi cuenta</a>'
            f'<a class="plan-logout" href="/auth/logout">Salir</a>'
        )
    else:
        # On league dashboard, also route to auth pages so the modal logic can run via ?auth=...
        auth_html = (
            '<a class="pill" href="/?auth=login">Entrar</a>'
            '<a class="pill" href="/?auth=register">Crear cuenta</a>'
        )

    is_admin_user = is_admin(user, request)
    can_see_all_picks_val = can_see_all_picks(user, request)
    plan = get_active_plan(uid) if uid else settings.plan_free
    is_free_mode = league in settings.free_leagues

    plan_badge = auth_html
    if is_admin_user:
        plan_badge = '<span class="plan-badge admin">ADMIN</span>' + auth_html
    elif plan == settings.plan_pro:
        plan_badge = '<span class="plan-badge pro">PRO</span>' + auth_html
    elif is_premium_active(user) or plan == settings.plan_premium:
        plan_badge = '<span class="plan-badge premium">PREMIUM</span>' + auth_html

    user_premium = bool(uid and (is_premium_active(user) or plan == settings.plan_premium))

    league_carousel_dashboard_html = (
        '<div class="home-carousel-strip" role="navigation" aria-label="Elegir liga">'
        + _build_home_league_snap_carousel_html(
            request,
            unsupported_football,
            carousel_id="leagueCarouselDash",
            active_league=league,
        )
        + "</div>"
    )

    welcome_banner = ""
    if request.query_params.get("msg") == "cuenta_creada" and user:
        name = user.get("username") or user.get("email") or ""
        welcome_banner = f'<div class="welcome-banner">Cuenta creada con éxito. Bienvenido, {html_lib.escape(name)}.</div>'

    admin_users_link = '<a href="/admin/users">Usuarios</a>' if is_admin_user else ''

    cache_meta = read_cache_meta()
    cache_status_html = _format_cache_status(cache_meta)

    raw_matches = read_json_with_fallback(f"daily_matches_{league}.json") or []
    raw_picks = read_json_with_fallback(f"daily_picks_{league}.json") or []
    matches = [m for m in raw_matches if isinstance(m, dict)]
    raw_picks_list = [p for p in raw_picks if isinstance(p, dict)]
    picks = [p for p in raw_picks_list if _is_pick_valid(p)]
    if len(raw_picks_list) > 0 and len(picks) == 0:
        logger.warning(
            "dashboard: %s had %s raw picks but 0 after _is_pick_valid; using raw picks as fallback",
            league, len(raw_picks_list),
        )
        picks = raw_picks_list
    # Force canonical league code on every pick so combo date windows + match_by_key align
    # (cached picks may carry a different _league/league string — setdefault was not enough for PL).
    for p in picks:
        if isinstance(p, dict):
            p["_league"] = league
    logger.info(
        "dashboard: league=%s raw_matches=%s raw_picks=%s after_filter_picks=%s",
        league, len(matches), len(raw_picks_list), len(picks),
    )
    if len(raw_picks_list) > 0 and len(picks) == 0:
        logger.warning("dashboard: had raw_picks but 0 after filter; fallback applied.")
    elif len(picks) == 0:
        logger.warning("dashboard: no picks (raw was empty for league=%s).", league)

    match_by_id: dict[int, dict] = {}
    for m in matches:
        if not isinstance(m, dict):
            continue
        mid = m.get("match_id")
        if mid is None:
            mid = m.get("id")
        mid_i = _safe_int(mid)
        if mid_i is not None:
            match_by_id[mid_i] = m

    upcoming_picks: list[dict] = []
    settled_picks: list[dict] = []
    for p in picks:
        mid = _safe_int(p.get("match_id") or p.get("id"))
        match_obj = match_by_id.get(mid) if mid is not None else None
        finished = isMatchFinished(p) or (isMatchFinished(match_obj) if isinstance(match_obj, dict) else False)
        if finished:
            settled_picks.append(p)
        else:
            upcoming_picks.append(p)

    def _model_rank(p: dict) -> int:
        return 0 if (p.get("model") or "").strip().upper() == "B" else 1

    upcoming_sorted = sorted(
        upcoming_picks,
        key=lambda p: (_model_rank(p), -_pick_score(p)),
    )
    selections = top_picks_with_variety(upcoming_sorted, top_n=10, max_repeats_per_market=3)

    days_with_matches = group_matches_by_day(matches, days=7)
    upcoming_picks_by_day = group_upcoming_picks_by_day(upcoming_picks, days=7)
    matches_by_date = {str(b.get("date", "")): b for b in days_with_matches}
    picks_by_date = {str(b.get("date", "")): b for b in upcoming_picks_by_day}
    today_iso = datetime.now().astimezone().date().isoformat()
    all_dates = sorted(set(matches_by_date.keys()) | set(picks_by_date.keys()))
    upcoming_days = []
    for date_str in all_dates:
        m_block = matches_by_date.get(date_str) or {}
        p_block = picks_by_date.get(date_str) or {}
        label = m_block.get("label") or p_block.get("label") or date_str
        upcoming_days.append({
            "date": date_str,
            "label": label,
            "matches": m_block.get("matches") or [],
            "picks": p_block.get("picks") or [],
        })
    default_upcoming_day = today_iso if any(d.get("date") == today_iso for d in upcoming_days) else (upcoming_days[0].get("date") if upcoming_days else "ALL")

    settled_sorted = sorted(settled_picks, key=lambda p: _parse_utcdate_str(p.get("utcDate")), reverse=True)
    settled_groups = group_picks_recent_by_day_desc(settled_sorted, days=7)
    spark_points = _roi_spark_points(settled_groups)
    market_rows = _profit_by_market(settled_picks)

    # Performance strip (above chart): ROI, acumulado chart, último día
    league_last_spark = spark_points[-1] if spark_points else {}
    league_perf_accum = round(float(league_last_spark.get("v", 0) or 0), 2)
    league_perf_day = round(float(league_last_spark.get("day", 0) or 0), 2)
    _lr = [p for p in settled_picks if _result_norm(p) in ("WIN", "LOSS", "PUSH")]
    _lb = _lr if _lr else list(settled_picks)
    _lts = sum(_pick_stake_units(p) for p in _lb) if _lb else 0.0
    if _lb and _lts > 0:
        _ltp = sum(_unit_delta(p) for p in _lb)
        league_strip_roi_pct = round((_ltp / _lts) * 100.0, 1)
        league_strip_roi_str = f"{league_strip_roi_pct:+.1f}%"
    else:
        league_strip_roi_pct = None
        league_strip_roi_str = "—"
    if league_strip_roi_pct is None:
        league_perf_trend = "neutral"
    elif league_strip_roi_pct > 0:
        league_perf_trend = "up"
    elif league_strip_roi_pct < 0:
        league_perf_trend = "down"
    else:
        league_perf_trend = "flat"
    league_arrow_up = league_perf_trend == "up"
    league_arrow_down = league_perf_trend == "down"
    league_accum_pos = league_perf_accum > 0
    league_accum_neg = league_perf_accum < 0
    league_day_pos = league_perf_day > 0
    league_day_neg = league_perf_day < 0
    has_league_chart = bool(spark_points)
    if has_league_chart:
        league_roi_chart_body = (
            '<canvas id="roiSpark" aria-hidden="true"></canvas>\n'
            '          <div id="roiTip" class="roi-tip" style="display:none;"></div>'
        )
    else:
        league_roi_chart_body = (
            '<div class="perf-chart-empty-state" role="status">'
            '<p class="perf-chart-empty-title">Sin datos suficientes todavía</p>'
            '<p class="perf-chart-empty-sub muted">La curva aparece cuando haya picks resueltos en los últimos días.</p>'
            "</div>"
        )
    _lp = []
    if league_strip_roi_pct is not None:
        if league_strip_roi_pct > 0:
            _lp.append("perf-stat-tile--pos")
        elif league_strip_roi_pct < 0:
            _lp.append("perf-stat-tile--neg")
        else:
            _lp.append("perf-stat-tile--flat")
    else:
        _lp.append("perf-stat-tile--neutral")
    league_primary_tile_class = " ".join(_lp)
    league_arrow_up_style = "display:inline" if league_arrow_up else "display:none"
    league_arrow_down_style = "display:inline" if league_arrow_down else "display:none"

    # Premium combos UI (same component as homepage)
    # - match_by_key keyed by (league, match_id); _league forced above on all picks

    match_by_key = {}
    for mid_i, m in match_by_id.items():
        match_by_key[(league, mid_i)] = m

    premium_combos = _build_home_premium_combos(
        upcoming_picks,
        match_by_key,
        log_context=f"league={league}",
    )
    combos_section_html = "\n".join(_render_home_premium_combo_card(c) for c in premium_combos)
    league_combos_html = f"""
    <section class="home-section">
      <h2 class="home-h2">Combos de Hoy</h2>
      <div class="home-combos-grid">
        {combos_section_html}
      </div>
    </section>
    """

    page_html = f"""
    <html>
    <head>
      <meta charset="utf-8"/>
      <title>AFTR Pick</title>
      <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
      <link rel="stylesheet" href="/static/style.css?v=22">
      <link rel="icon" type="image/png" href="/static/logo_aftr.png">

      <link rel="manifest" href="/static/manifest.webmanifest">
      <meta name="theme-color" content="#0b0f14">

      <!-- iOS -->
      <meta name="apple-mobile-web-app-capable" content="yes">
      <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
      <meta name="apple-mobile-web-app-title" content="AFTR">
      <link rel="apple-touch-icon" href="/static/pwa/icon-192.png">
      <link rel="apple-touch-startup-image" href="/static/pwa/splash-1290x2796.png" media="(device-width: 430px) and (device-height: 932px) and (-webkit-device-pixel-ratio: 3)">
      <link rel="apple-touch-startup-image" href="/static/pwa/splash-1179x2556.png" media="(device-width: 393px) and (device-height: 852px) and (-webkit-device-pixel-ratio: 3)">
      <link rel="apple-touch-startup-image" href="/static/pwa/splash-1242x2688.png" media="(device-width: 414px) and (device-height: 896px) and (-webkit-device-pixel-ratio: 3)">
    </head>

    <body>
    """ + AUTH_BOOTSTRAP_SCRIPT + f"""

      <!-- Premium Modal (afuera de .page) -->
      <div id="premium-modal" class="modal-backdrop" style="display:none;">
        <div class="modal">
          <div class="modal-head">
            <div class="modal-title">⭐ AFTR Premium</div>
            <button class="modal-x" onclick="closePremium()">✕</button>
          </div>

          <div class="modal-body">
            <p class="modal-subtitle">Desbloqueá el motor de apuestas con IA</p>
            <div class="modal-section">Qué incluye</div>
            <ul class="modal-list">
              <li>Todos los picks del día</li>
              <li>Picks con alto AFTR Score</li>
              <li>Apuestas de valor con ventaja positiva</li>
              <li>Picks de todas las ligas</li>
              <li>Análisis avanzado de partidos</li>
              <li>Combinadas de valor inteligentes</li>
              <li>Acceso anticipado a picks</li>
            </ul>

            <div class="modal-price">
              <span class="price-main">$9.99</span>
              <span class="price-sub">/ mes</span>
            </div>
            <p class="modal-cancel">Cancelá cuando quieras</p>

            """ + ('<div class="premium-badge">⭐ Premium activo</div>' if user_premium else '<button class="pill modal-cta" onclick="activatePremium(\'PREMIUM\')">Activar Premium</button>') + """
          </div>
        </div>
      </div>

      <!-- ✅ CONTENIDO CENTRADO -->
      <div class="page">
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
          <form action="/auth/login" method="post" onsubmit="console.log('FORM SUBMIT');">
            <input type="email" name="email" required>
            <input type="password" name="password" required>
            <button type="submit">Entrar</button>
          </form>
          <div class="modal-line" style="margin-top: 12px;">
            <a href="#" onclick="closeLoginModal(); openForgotModal(); return false;" class="muted" style="font-size: 13px;">¿Olvidaste tu contraseña?</a>
          </div>
        </div>
      </div>
    </div>

    <div id="forgot-modal" class="modal-backdrop" style="display:none;">
      <div class="modal">
        <div class="modal-head">
          <div class="modal-title">Recuperar contraseña</div>
          <button class="modal-x" onclick="closeForgotModal()">✕</button>
        </div>
        <div class="modal-body">
          <div id="forgot-error" class="modal-line" style="color:#c00; display:none;"></div>
          <div id="forgot-success" class="modal-line" style="color:#0a0; display:none;"></div>
          <div class="modal-line">
            <input type="email" id="forgot-email" class="email-input" placeholder="tu@email.com">
          </div>
          <button class="pill modal-cta" onclick="forgotSubmit()" style="width:100%;">Enviar enlace</button>
          <p class="muted" style="font-size: 12px; margin-top: 12px;">Si el email existe en AFTR, recibirás un enlace para restablecer la contraseña. Revisá la bandeja y spam.</p>
        </div>
      </div>
    </div>

    <div id="premium-success-modal" class="modal-backdrop premium-success-backdrop" style="display:none;">
      <div class="modal premium-success-modal">
        <div class="premium-success-content">
          <div class="premium-success-icon">✨</div>
          <h3 class="premium-success-title">Premium activado</h3>
          <p class="premium-success-sub">Bienvenido a AFTR Elite</p>
          <p class="premium-success-detail">Todos los picks desbloqueados</p>
          <button type="button" class="pill modal-cta" onclick="closePremiumSuccess()">Continuar</button>
        </div>
        <button class="modal-x premium-success-x" onclick="closePremiumSuccess()" aria-label="Cerrar">✕</button>
      </div>
    </div>
        """
    
    page_html += """
    <div id="welcome-modal" class="modal-backdrop" style="display:none;">
      <div class="modal">
        <div class="modal-head">
          <div class="modal-title">👋 Bienvenido a AFTR</div>
          <button class="modal-x" onclick="closeWelcome()">✕</button>
        </div>

        <div class="modal-body">
          <div class="modal-line"><b>Cómo se usa:</b> elegís liga ➜ mirás Top Picks ➜ tocás una card para ver stats.</div>
          <div class="modal-line"><b>FREE:</b> 3 picks por liga free + resultados y rendimiento.</div>
          <div class="modal-line"><b>PREMIUM:</b> todas las picks + más ligas + combinadas + más data.</div>

          <button class="pill modal-cta" onclick="closeWelcome()">Entendido ✅</button>
          """ + ('<div class="premium-badge" style="margin-top:10px;">⭐ Premium activo</div>' if user_premium else '<button class="pill" style="width:100%; margin-top:10px;" onclick="closeWelcome(); openPremium();">Ver Premium ⭐</button>') + """
        </div>
      </div>
    </div>
    """

    page_html += f"""
      <div class="top top-pro top-pro--with-carousel">
        <div class="brand">
          <img src="/static/logo_aftr.png" class="logo-aftr" alt="AFTR" />
          <div class="brand-text">
            <div class="brand-title">AFTR</div>
            <div class="brand-tag">Motor de apuestas con IA</div>  
          </div>
          {plan_badge}
        </div>
        {welcome_banner}
        {cache_status_html}
        <div class="top-actions top-actions--carousel" role="navigation" aria-label="Liga y enlaces">
          {league_carousel_dashboard_html}
          <div class="links">
            <a href="/">Inicio</a>
            <a href="/?league={league}">Panel</a>
            <a href="/api/matches?league={league}" target="_blank">Matches JSON</a>
            <a href="/api/picks?league={league}" target="_blank">Picks JSON</a>
            {admin_users_link}
          </div>
        </div>
      </div>
    """

    page_html += """
    <script>
      function openWelcome(){
        var m = document.getElementById('welcome-modal');
        if (m) m.style.display = 'flex';
      }

      function closeWelcome(){
        var m = document.getElementById('welcome-modal');
        if (m) m.style.display = 'none';
        try { localStorage.setItem('aftr_welcome_seen', '1'); } catch(e){}
      }

      document.addEventListener('DOMContentLoaded', function(){
        var seen = null;
        try { seen = localStorage.getItem('aftr_welcome_seen'); } catch(e){}
        if (!seen) {
          setTimeout(openWelcome, 450);
        }
      });
    </script>
    <script>

    window.openLoginModal = function() {
      var m = document.getElementById("login-modal");
      if (m) m.style.display = "flex";
    };
    window.closeLoginModal = function() {
      var m = document.getElementById("login-modal");
      if (m) m.style.display = "none";
    };
    window.openSignupModal = function() {
      var m = document.getElementById("signup-modal");
      if (m) m.style.display = "flex";
    };
    window.closeSignupModal = function() {
      var m = document.getElementById("signup-modal");
      if (m) m.style.display = "none";
    };
    function openForgotModal(){
      var m = document.getElementById("forgot-modal");
      if (m) { m.style.display = "flex"; document.getElementById("forgot-error").style.display = "none"; document.getElementById("forgot-success").style.display = "none"; }
    }
    function closeForgotModal(){
      var m = document.getElementById("forgot-modal");
      if (m) m.style.display = "none";
    }
    function forgotSubmit(){
      var email = (document.getElementById("forgot-email") || {}).value.trim();
      var errEl = document.getElementById("forgot-error");
      var okEl = document.getElementById("forgot-success");
      errEl.style.display = "none";
      okEl.style.display = "none";
      if (!email || email.indexOf("@") < 1) { errEl.textContent = "Introduce un email válido."; errEl.style.display = "block"; return; }
      var url = (window.location.origin || (window.location.protocol + "//" + window.location.host)) + "/auth/forgot-password";
      fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, credentials: "include", body: JSON.stringify({ email: email }) })
        .then(function(r){ return r.json(); })
        .then(function(d){
          if (d.ok) { okEl.textContent = "Listo. Si el email existe, recibirás instrucciones."; okEl.style.display = "block"; }
          else { errEl.textContent = d.error === "email_invalido" ? "Introduce un email válido." : (d.error || "Error."); errEl.style.display = "block"; }
        })
        .catch(function(){ errEl.textContent = "Error de conexión."; errEl.style.display = "block"; });
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

    function showPremiumSuccess(){
      var m = document.getElementById("premium-success-modal");
      if (m) { m.style.display = "flex"; window._premiumSuccessTimeout = setTimeout(closePremiumSuccess, 5000); }
    }
    function closePremiumSuccess(){
      var m = document.getElementById("premium-success-modal");
      if (m) m.style.display = "none";
      if (window._premiumSuccessTimeout) { clearTimeout(window._premiumSuccessTimeout); window._premiumSuccessTimeout = null; }
      var params = new URLSearchParams(window.location.search);
      if (params.get("msg") === "premium_activated") {
        params.delete("msg");
        var qs = params.toString();
        window.location.href = window.location.pathname + (qs ? "?" + qs : "");
      }
    }
    document.addEventListener("DOMContentLoaded",function(){
      var params = new URLSearchParams(window.location.search);
      var authParam = params.get("auth");
      if (authParam === "login" && typeof window.openLoginModal === "function") {
        window.openLoginModal();
      } else if (authParam === "register" && typeof window.openSignupModal === "function") {
        window.openSignupModal();
      } else if (params.get("msg") === "premium_activated" && typeof window.showPremiumSuccess === "function") {
        window.showPremiumSuccess();
      }
    })

    </script>
    """

    # summary bar + premium ROI / performance block
    page_html += f"""
      <div class="league-dash-panel">
      <div id="summary-bar" class="summary-bar league-kpi-strip" data-league="{league}">
        <div class="kpi-grid league-kpi-grid">
          <div class="kpi-card"><span class="kpi-label">ROI</span><span class="kpi-value" id="kpi-roi">—</span></div>
          <div class="kpi-card"><span class="kpi-label">Selecciones totales</span><span class="kpi-value" id="kpi-total">—</span></div>
          <div class="kpi-card"><span class="kpi-label">Gana</span><span class="kpi-value" id="kpi-wins">—</span></div>
          <div class="kpi-card"><span class="kpi-label">Pérdidas</span><span class="kpi-value" id="kpi-losses">—</span></div>
          <div class="kpi-card"><span class="kpi-label">Pendiente</span><span class="kpi-value" id="kpi-pending">—</span></div>
          <div class="kpi-card"><span class="kpi-label">Beneficio neto</span><span class="kpi-value" id="kpi-net">—</span></div>
        </div>
      </div>
      <section class="league-perf-block perf-panel-premium" aria-label="Rendimiento">
        <div class="perf-panel-head">
          <h2 class="perf-panel-title">Rendimiento</h2>
          <p class="perf-panel-sub muted">Últimos días · unidades netas en esta liga</p>
        </div>
        <div class="perf-panel-glass league-perf-chart-shell">
          <div class="perf-strip-stats" role="group" aria-label="Resumen de rendimiento">
            <div class="perf-stat-tile perf-stat-tile--primary {league_primary_tile_class}">
              <span class="perf-stat-arrow perf-stat-arrow--up" aria-hidden="true" style="{league_arrow_up_style}">↑</span>
              <span class="perf-stat-arrow perf-stat-arrow--down" aria-hidden="true" style="{league_arrow_down_style}">↓</span>
              <span class="perf-stat-value" id="perf-strip-roi">{league_strip_roi_str}</span>
              <span class="perf-stat-label">ROI total</span>
            </div>
            <div class="perf-stat-tile{' perf-stat-tile--pos' if league_accum_pos else ''}{' perf-stat-tile--neg' if league_accum_neg else ''}">
              <span class="perf-stat-value" id="perf-strip-accum">{league_perf_accum:+.2f}u</span>
              <span class="perf-stat-label">Profit acumulado</span>
            </div>
            <div class="perf-stat-tile{' perf-stat-tile--pos' if league_day_pos else ''}{' perf-stat-tile--neg' if league_day_neg else ''}">
              <span class="perf-stat-value" id="perf-strip-day">{league_perf_day:+.2f}u</span>
              <span class="perf-stat-label">Último día</span>
            </div>
          </div>
          <div class="roi-spark-head perf-chart-head-inner">
            <div>
              <div class="roi-spark-title">Curva acumulada</div>
              <div class="roi-spark-sub muted">Pasá el mouse para ver el detalle por día</div>
            </div>
          </div>
          <div class="roi-spark-canvas perf-chart-canvas-wrap">
            {league_roi_chart_body}
          </div>
        </div>
      </section>
      </div>

      <script>
        window.AFTR_ROI_POINTS = {json.dumps(spark_points)};
      </script>
    
      <script>
        if ("serviceWorker" in navigator) {{
          window.addEventListener("load", function () {{
            navigator.serviceWorker.register("/static/sw.js").catch(function(){{}});
          }});
        }}
      </script>
    """

    # Profit por mercado — compact grid (2 cols on desktop), aligned with dashboard width
    market_slice = market_rows[:8]
    market_cards_html = ""
    for r in market_slice:
        wr = f" • {r['winrate']}%" if r["winrate"] is not None else ""
        net_cls = "market-compact-net--pos" if r["net_units"] >= 0 else "market-compact-net--neg"
        market_cards_html += f"""
          <div class="market-compact-card card">
            <div class="market-compact-top">
              <div class="market-compact-name">{html_lib.escape(str(r["market"]))}</div>
              <div class="market-compact-net {net_cls}">{("+" if r["net_units"]>=0 else "")}{r["net_units"]}u</div>
            </div>
            <div class="market-compact-meta muted">{r["picks"]} picks · W{r["wins"]}-L{r["losses"]}{wr}</div>
            <div class="market-bar market-bar--compact">
              <div class="market-fill" data-u="{r["net_units"]}"></div>
              <div class="market-val">{("+" if r["net_units"]>=0 else "") + str(r["net_units"])}u</div>
            </div>
          </div>"""
    if not market_cards_html:
        market_cards_html = '<p class="muted market-compact-empty">Sin datos de mercado todavía.</p>'
    page_html += f"""
      <section class="league-market-section">
        <h2 class="league-market-title home-h2">💰 Ganancia por mercado</h2>
        <p class="league-market-intro muted">Unidades netas por mercado (histórico resuelto en esta liga).</p>
        <div class="market-compact-grid">
          {market_cards_html}
        </div>
      </section>
    """

    # Combos after performance blocks (ROI + mercado)
    page_html += league_combos_html

    # Upcoming: day filter by date (value=YYYY-MM-DD); default "Hoy" when available
    upcoming_filter_opts = ['<option value="ALL">Todos</option>']
    for d in upcoming_days:
        date_val = d.get("date", "")
        lab = d.get("label", "")
        sel = ' selected' if date_val == default_upcoming_day else ''
        upcoming_filter_opts.append(f'<option value="{html_lib.escape(date_val)}"{sel}>{html_lib.escape(lab)}</option>')
    upcoming_filter_options_html = "".join(upcoming_filter_opts)
    page_html += f"""
      <div class="filterbar">
        <span class="filter-label">Día</span>
        <select id="upcoming-filter" class="day-select" data-default-day="{html_lib.escape(default_upcoming_day)}">
          {upcoming_filter_options_html}
        </select>
      </div>
    """
    if not picks:
        page_html += '<div class="coming-soon muted">Próximamente nuevos picks</div>'
    elif not upcoming_days:
        if not matches:
            page_html += "<p class='muted'>No hay matches JSON para esta liga (todavía).</p>"
        else:
            page_html += "<p class='muted'>No hay partidos ni picks en los próximos 7 días.</p>"
    else:
        for day_block in upcoming_days:
            date_str = str(day_block.get("date", ""))
            label = str(day_block.get("label", ""))
            day_matches = day_block.get("matches") or []
            day_picks = day_block.get("picks") or []
            count_m = len(day_matches)
            count_p = len(day_picks)

            page_html += f"""
            <h3 class="day-title day-title-upcoming" data-day="{html_lib.escape(date_str)}">
              {html_lib.escape(label)} ({count_m} partido{"s" if count_m != 1 else ""}, {count_p} pick{"s" if count_p != 1 else ""})
            </h3>
            <div class="day-block upcoming-block" data-day="{html_lib.escape(date_str)}">
            <div class="grid">
            """
            for m in day_matches:
                if not isinstance(m, dict):
                    continue
                home_part = _team_with_crest(m.get("home_crest"), m.get("home", ""))
                away_part = _team_with_crest(m.get("away_crest"), m.get("away", ""))
                page_html += f"""
              <div class="card">
                <div class="row">{home_part} <span class="vs">vs</span> {away_part}</div>
                <div class="meta" data-utc="{html_lib.escape(str(m.get('utcDate','')))}">
                  {html_lib.escape(format_match_kickoff_ar(m.get("utcDate")))}
                </div>
              </div>
                """
            page_html += "</div>"
            if day_picks:
                page_html += """
            <div class="day-picks-wrap" style="margin-top:14px;">
              <h4 class="muted" style="margin-bottom:8px; font-size:13px;">Selecciones</h4>
            <div class="grid">
            """
                day_picks_sorted = sorted(
                    day_picks,
                    key=lambda p: (-(p.get("aftr_score") or 0), _model_rank(p), -_pick_score(p)),
                )
                pick_list = top_picks_with_variety(
                    day_picks_sorted,
                    top_n=20,
                    max_repeats_per_market=5,
                )
                if can_see_all_picks_val:
                    for p, best in pick_list:
                        page_html += _render_pick_card(p, best, match_by_id=match_by_id)
                else:
                    if not is_free_mode:
                        page_html += _locked_grid(8, "Esta liga es Premium")
                    else:
                        for p, best in pick_list[:3]:
                            page_html += _render_pick_card(p, best, match_by_id=match_by_id)
                        page_html += _premium_unlock_card()
                page_html += "</div></div>"
            page_html += "</div>"

    # =========================================================
    # RESULTADOS (últimos 7 días) — grouped by date
    # =========================================================
    settled_filter_opts = ['<option value="ALL">Todos</option>']
    for day_block in (settled_groups or []):
        date_str = str(day_block.get("date", ""))
        label = str(day_block.get("label", ""))
        settled_filter_opts.append(f'<option value="{html_lib.escape(date_str)}">{html_lib.escape(label)}</option>')
    settled_filter_options_html = "".join(settled_filter_opts)

    page_html += """
      <h2 style="margin-top:22px;">✅ Resultados recientes (últimos 7 días)</h2>
      <div class="filterbar">
        <span class="filter-label">Día</span>
        <select id="settled-filter" class="day-select" data-default-day="ALL">
    """ + settled_filter_options_html + """
        </select>
      </div>
      <div class="section settled">
    """

    page_html += """
      <div class="tabs" id="settled-tabs">
        <button class="tab active" data-target="ALL">Todos</button>
      </div>
    """

    if not settled_sorted:
        page_html += "<p class='muted'>Todavía no hay picks resueltas para mostrar.</p>"
    elif not settled_groups:
        page_html += "<p class='muted'>No hay picks resueltas dentro de los últimos 7 días.</p>"
    else:
        for day_block in settled_groups:
            date_str = str(day_block.get("date", ""))
            label = str(day_block.get("label", ""))
            day_items = day_block.get("matches", []) or []
            count = len(day_items)

            page_html += f"""
            <h3 class="day-title day-title-settled" data-day="{html_lib.escape(date_str)}">
              {html_lib.escape(label)} ({count} pick{"s" if count != 1 else ""})
            </h3>
            <div class="grid day-block settled-block" data-day="{html_lib.escape(date_str)}">
            """

            for p in day_items:
                if not isinstance(p, dict):
                    continue
                page_html += _render_pick_card(p, None, match_by_id=match_by_id)

            page_html += "</div>"

    page_html += """
         <script>
            (function(){
              function clamp(n,a,b){ return Math.max(a, Math.min(b, n)); }

              function paintMarketBars(){
                var els = document.querySelectorAll('.market-fill[data-u]');
                if(!els.length) return;

                var vals = [];
                els.forEach(function(el){
                  vals.push(Number(el.getAttribute('data-u') || 0));
                });

                var maxAbs = 0;
                vals.forEach(function(v){ maxAbs = Math.max(maxAbs, Math.abs(v)); });
                if(maxAbs === 0) maxAbs = 1;

                els.forEach(function(el){
                  var u = Number(el.getAttribute('data-u') || 0);
                  var pct = clamp((Math.abs(u) / maxAbs) * 100, 6, 100);

                  el.classList.remove('pos','neg');
                  el.classList.add(u >= 0 ? 'pos' : 'neg');

                  el.style.display = 'block';
                  el.style.width = '0%';
                  void el.offsetWidth;

                  requestAnimationFrame(function(){
                    el.style.width = pct + '%';
                  });
                });
              }

              window.AFTR_paintMarketBars = paintMarketBars;

              function boot(){
                document.documentElement.style.overflow = '';
                document.body.style.overflow = '';
                paintMarketBars();
                setTimeout(paintMarketBars, 80);
                setTimeout(paintMarketBars, 250);
              }

              if(document.readyState === "loading") {
                document.addEventListener("DOMContentLoaded", boot);
              } else {
                boot();
              }

              window.addEventListener("load", boot);
            })();
            </script>
      """
    page_html += """
         </div> <!-- /section settled -->

            <script>
              function openPremium(){
                var m = document.getElementById('premium-modal');
                if (m) m.style.display = 'flex';
                document.documentElement.style.overflow = 'hidden';
                document.body.style.overflow = 'hidden';
              }

              function closePremium(){
                var m = document.getElementById('premium-modal');
                if (m) m.style.display = 'none';
                document.documentElement.style.overflow = '';
                document.body.style.overflow = '';
              }

              function activatePremium(plan){
                var url = (window.location.origin || (window.location.protocol + '//' + window.location.host)) + '/billing/create-checkout-session';
                fetch(url, {
                  method: 'POST',
                  headers: { 'Content-Type': 'application/json' },
                  credentials: 'include',
                  body: JSON.stringify({})
                })
                .then(function(r){ return r.json().then(function(d){ return { ok: r.ok, data: d }; }); })
                .then(function(result){
                  if (result.ok && result.data && result.data.url) {
                    window.location.href = result.data.url;
                  } else if (result.data && result.data.error === 'need_login') {
                    closePremium();
                    openLoginModal();
                  } else {
                    alert('No se pudo iniciar el checkout de Premium. Intenta de nuevo más tarde.');
                  }
                })
                .catch(function(){
                  alert('Error de conexión con el servidor de pagos.');
                });
              }

              document.addEventListener('click', function(e){
                var m = document.getElementById('premium-modal');
                if (!m || m.style.display !== 'flex') return;
                if (e.target === m) closePremium();
              });

              document.addEventListener('click', function(e){
                var fc = e.target.closest && e.target.closest('.flip-card');
                if (!fc) return;
                if (e.target.closest('a,button')) return;
                fc.classList.toggle('is-flipped');
              });

              document.addEventListener('keydown', function(e){
                if (e.key !== 'Enter' && e.key !== ' ') return;
                var fc = document.activeElement && document.activeElement.closest && document.activeElement.closest('.flip-card');
                if (!fc) return;
                e.preventDefault();
                fc.classList.toggle('is-flipped');
              });

              (function pickActions(){
                var base = window.location.origin || (window.location.protocol + '//' + window.location.host);
                window.__userLoggedIn = null;
                function checkLogin(){
                  if (window.__userLoggedIn !== null) return Promise.resolve(window.__userLoggedIn);
                  return fetch(base + '/user/me', { credentials: 'include' }).then(function(r){ return r.json(); }).then(function(d){
                    window.__userLoggedIn = !!(d && d.ok && d.user);
                    return window.__userLoggedIn;
                  }).catch(function(){ window.__userLoggedIn = false; return false; });
                }
                function toast(msg){
                  var el = document.createElement('div');
                  el.className = 'pick-toast';
                  el.style.cssText = 'position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--card-bg,#1a1a1a);color:#fff;padding:10px 18px;border-radius:8px;font-size:0.9rem;z-index:9999;box-shadow:0 4px 12px rgba(0,0,0,0.3);';
                  el.textContent = msg;
                  document.body.appendChild(el);
                  setTimeout(function(){ if (el.parentNode) el.parentNode.removeChild(el); }, 2500);
                }
                function doFavorite(btn){
                  var pickId = btn.getAttribute('data-pick-id');
                  if (!pickId) return;
                  checkLogin().then(function(loggedIn){
                    if (!loggedIn){ alert('Iniciá sesión para usar esta función'); return; }
                    if (btn.disabled) return;
                    btn.disabled = true;
                    var payload = { pick_id: pickId };
                    var market = btn.getAttribute('data-market'); if (market) payload.market = market;
                    var aftr = btn.getAttribute('data-aftr-score'); if (aftr !== null && aftr !== '') payload.aftr_score = parseInt(aftr, 10);
                    var tier = btn.getAttribute('data-tier'); if (tier) payload.tier = tier;
                    var edge = btn.getAttribute('data-edge'); if (edge !== null && edge !== '') payload.edge = parseFloat(edge);
                    var home = btn.getAttribute('data-home-team'); if (home) payload.home_team = home;
                    var away = btn.getAttribute('data-away-team'); if (away) payload.away_team = away;
                    fetch(base + '/user/favorite', { method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include', body: JSON.stringify(payload) })
                      .then(function(r){ return r.json(); })
                      .then(function(d){ if (d && d.ok){ btn.textContent = 'Guardado ✅'; toast('Pick guardada'); } else { btn.disabled = false; toast(d && d.error || 'Error'); } })
                      .catch(function(){ btn.disabled = false; toast('Error de conexión'); });
                  });
                }
                function doFollow(btn){
                  var pickId = btn.getAttribute('data-pick-id');
                  if (!pickId) return;
                  checkLogin().then(function(loggedIn){
                    if (!loggedIn){ alert('Iniciá sesión para usar esta función'); return; }
                    if (btn.disabled) return;
                    btn.disabled = true;
                    var payload = { pick_id: pickId };
                    var market = btn.getAttribute('data-market'); if (market) payload.market = market;
                    var aftr = btn.getAttribute('data-aftr-score'); if (aftr !== null && aftr !== '') payload.aftr_score = parseInt(aftr, 10);
                    var tier = btn.getAttribute('data-tier'); if (tier) payload.tier = tier;
                    var edge = btn.getAttribute('data-edge'); if (edge !== null && edge !== '') payload.edge = parseFloat(edge);
                    var home = btn.getAttribute('data-home-team'); if (home) payload.home_team = home;
                    var away = btn.getAttribute('data-away-team'); if (away) payload.away_team = away;
                    fetch(base + '/user/follow-pick', { method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include', body: JSON.stringify(payload) })
                      .then(function(r){ return r.json(); })
                      .then(function(d){ if (d && d.ok){ btn.textContent = 'Siguiendo 📈'; toast('Pick seguida'); } else { btn.disabled = false; toast(d && d.error || 'Error'); } })
                      .catch(function(){ btn.disabled = false; toast('Error de conexión'); });
                  });
                }
                function applyPersistedState(){
                  checkLogin().then(function(loggedIn){
                    if (!loggedIn) return;
                    Promise.all([
                      fetch(base + '/user/favorites', { credentials: 'include' }).then(function(r){ return r.json(); }),
                      fetch(base + '/user/followed-ids', { credentials: 'include' }).then(function(r){ return r.json(); })
                    ]).then(function(results){
                      var favData = results[0];
                      var followedData = results[1];
                      var favoriteIds = {};
                      var followedIds = {};
                      if (favData && favData.ok && Array.isArray(favData.favorites)) favData.favorites.forEach(function(x){ favoriteIds[x.pick_id] = true; });
                      if (followedData && followedData.ok && Array.isArray(followedData.pick_ids)) followedData.pick_ids.forEach(function(id){ followedIds[id] = true; });
                      document.querySelectorAll('.btn-favorite-pick').forEach(function(btn){
                        var id = btn.getAttribute('data-pick-id');
                        if (id && favoriteIds[id]){ btn.textContent = 'Guardado ✅'; }
                      });
                      document.querySelectorAll('.btn-follow-pick').forEach(function(btn){
                        var id = btn.getAttribute('data-pick-id');
                        if (id && followedIds[id]){ btn.textContent = 'Siguiendo 📈'; }
                      });
                    }).catch(function(){});
                  });
                }
                document.addEventListener('click', function(e){
                  var fav = e.target.closest && e.target.closest('.btn-favorite-pick');
                  if (fav){ e.preventDefault(); e.stopPropagation(); doFavorite(fav); return; }
                  var fol = e.target.closest && e.target.closest('.btn-follow-pick');
                  if (fol){ e.preventDefault(); e.stopPropagation(); doFollow(fol); return; }
                });
                if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', applyPersistedState);
                else applyPersistedState();
              })();
            </script>
      """
    page_html +="""
      <script>
        (function(){
          function clamp(n,a,b){ return Math.max(a, Math.min(b, n)); }

          function drawSpark(canvasId, points){
            var c = document.getElementById(canvasId);
            var tip = document.getElementById("roiTip");
            if(!c || !points || !points.length) return;

            var ctx = c.getContext('2d');
            var parent = c.parentElement;
            var w = Math.max(320, parent ? parent.clientWidth : c.width);
            var h = 140;
            c.width = w;
            c.height = h;

            // valores
            var vals = points.map(p => Number(p.v || 0));
            var min = Math.min.apply(null, vals);
            var max = Math.max.apply(null, vals);
            if (min === max) { min -= 1; max += 1; }

            var padX = 18, padY = 22;
            var innerW = w - padX*2;
            var innerH = h - padY*2;

            function clamp(n,a,b){ return Math.max(a, Math.min(b, n)); }

            function xAt(i){
              if(points.length === 1) return padX + innerW/2;
              return padX + (innerW * (i/(points.length-1)));
            }
            function yAt(v){
              var t = (v - min) / (max - min);
              return padY + innerH - (t * innerH);
            }

            var pathPts = points.map(function(p, i){
              return { x: xAt(i), y: yAt(Number(p.v || 0)) };
            });

            function redraw(hoverIndex){
              ctx.clearRect(0,0,w,h);

              // grid
              ctx.globalAlpha = 0.28;
              ctx.strokeStyle = "rgba(255,255,255,0.18)";
              ctx.lineWidth = 1;
              for (var i=0;i<3;i++){
                var y = padY + (innerH * (i/2));
                ctx.beginPath(); ctx.moveTo(padX, y); ctx.lineTo(padX+innerW, y); ctx.stroke();
              }
              ctx.globalAlpha = 1;

              // línea 0
              var y0 = yAt(0);
              ctx.globalAlpha = 0.55;
              ctx.strokeStyle = "rgba(255,255,255,0.25)";
              ctx.setLineDash([6,6]);
              ctx.beginPath(); ctx.moveTo(padX, y0); ctx.lineTo(padX+innerW, y0); ctx.stroke();
              ctx.setLineDash([]);
              ctx.globalAlpha = 1;

              // area fill
              ctx.beginPath();
              pathPts.forEach(function(pt, i){
                if(i===0) ctx.moveTo(pt.x, pt.y);
                else ctx.lineTo(pt.x, pt.y);
              });
              ctx.lineTo(pathPts[pathPts.length-1].x, padY+innerH);
              ctx.lineTo(pathPts[0].x, padY+innerH);
              ctx.closePath();

              var grad = ctx.createLinearGradient(0, padY, 0, padY+innerH);
              grad.addColorStop(0, "rgba(120,170,255,0.22)");
              grad.addColorStop(1, "rgba(120,170,255,0.00)");
              ctx.fillStyle = grad;
              ctx.fill();

              // línea
              ctx.lineWidth = 3;
              ctx.strokeStyle = "rgba(120,170,255,0.95)";
              ctx.beginPath();
              pathPts.forEach(function(pt, i){
                if(i===0) ctx.moveTo(pt.x, pt.y);
                else ctx.lineTo(pt.x, pt.y);
              });
              ctx.stroke();

              // puntos (verde/rojo por neto del día)
              pathPts.forEach(function(pt, i){
                var day = Number(points[i].day || 0);
                var col = day > 0 ? "rgba(34,197,94,0.95)"
                        : (day < 0 ? "rgba(239,68,68,0.95)"
                        : "rgba(255,255,255,0.85)");
                ctx.fillStyle = col;
                ctx.beginPath(); ctx.arc(pt.x, pt.y, 3.2, 0, Math.PI*2); ctx.fill();
              });

              // etiqueta
              var last = points[points.length-1];
              var lastV = Number(last.v || 0);
              var lastDay = Number(last.day || 0);
              ctx.fillStyle = "rgba(255,255,255,0.90)";
              ctx.font = "12px system-ui, -apple-system, Segoe UI, Roboto";
              ctx.fillText(
                "Acum: " + (lastV>=0?"+":"") + lastV.toFixed(2) + "u"
                + "  |  Último día: " + (lastDay>=0?"+":"") + lastDay.toFixed(2) + "u",
                padX, 14
              );

              // highlight hover
              if(hoverIndex != null && hoverIndex >= 0){
                var pt = pathPts[hoverIndex];

                ctx.globalAlpha = 0.55;
                ctx.strokeStyle = "rgba(255,255,255,0.20)";
                ctx.lineWidth = 1;
                ctx.beginPath(); ctx.moveTo(pt.x, padY); ctx.lineTo(pt.x, padY+innerH); ctx.stroke();
                ctx.globalAlpha = 1;

                ctx.fillStyle = "rgba(120,170,255,1)";
                ctx.beginPath(); ctx.arc(pt.x, pt.y, 6, 0, Math.PI*2); ctx.fill();
                ctx.fillStyle = "rgba(255,255,255,0.95)";
                ctx.beginPath(); ctx.arc(pt.x, pt.y, 3, 0, Math.PI*2); ctx.fill();
              }
            }

            function nearestIndex(mx){
              var best = 0, bestDist = Infinity;
              for(var i=0;i<pathPts.length;i++){
                var d = Math.abs(pathPts[i].x - mx);
                if(d < bestDist){ bestDist = d; best = i; }
              }
              return best;
            }

            function showTip(i, clientX, clientY){
              if(!tip) return;
              var p = points[i];
              tip.innerHTML =
                "<div><b>" + (p.label || "Día") + "</b></div>"
                + "<div class='muted'>Neto: " + ((Number(p.day||0)>=0?"+":"") + Number(p.day||0).toFixed(2)) + "u</div>"
                + "<div>Acum: " + ((Number(p.v||0)>=0?"+":"") + Number(p.v||0).toFixed(2)) + "u</div>";
              tip.style.display = "block";

              var rect = c.getBoundingClientRect();
              var x = clientX - rect.left;
              var y = clientY - rect.top;

              var tx = clamp(x + 12, 8, rect.width - 220);
              var ty = clamp(y - 10, 8, rect.height - 70);

              tip.style.left = tx + "px";
              tip.style.top = ty + "px";
            }

            function hideTip(){
              if(tip) tip.style.display = "none";
              redraw(-1);
            }

            // inicial
            redraw(-1);

            // eventos
            c.onmousemove = function(e){
              var rect = c.getBoundingClientRect();
              var mx = e.clientX - rect.left;

              if(mx < padX || mx > (padX+innerW)){
                hideTip();
                return;
              }

              var i = nearestIndex(mx);
              redraw(i);
              showTip(i, e.clientX, e.clientY);
            };
            c.onmouseleave = hideTip;
          }

          function boot(){
            var pts = window.AFTR_ROI_POINTS || [];
            drawSpark("roiSpark", pts);
            window.addEventListener("resize", function(){
              drawSpark("roiSpark", window.AFTR_ROI_POINTS || []);
            });
          }

  if(document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
</script>

      <script>
        (function () {
          function fillSelect(selectId, blocksSelector, titleSelector, keepOptionsAndDefault) {
            var sel = document.getElementById(selectId);
            if (!sel) return;

            var blocks = Array.prototype.slice.call(document.querySelectorAll(blocksSelector));
            var titles = Array.prototype.slice.call(document.querySelectorAll(titleSelector));

            function apply() {
              var val = sel.value || 'ALL';
              blocks.forEach(function (b) {
                var d = b.getAttribute('data-day') || '';
                b.style.display = (val === 'ALL' || d === val) ? '' : 'none';
              });
              titles.forEach(function (t) {
                var d = t.getAttribute('data-day') || '';
                t.style.display = (val === 'ALL' || d === val) ? '' : 'none';
              });
            }

            if (keepOptionsAndDefault && sel.options.length > 1) {
              var def = sel.getAttribute('data-default-day');
              if (def) sel.value = def;
              sel.addEventListener('change', apply);
              apply();
              return;
            }

            var days = [];
            blocks.forEach(function (b) {
              var d = b.getAttribute('data-day') || '';
              if (d && days.indexOf(d) === -1) days.push(d);
            });
            while (sel.options.length > 1) sel.remove(1);
            days.forEach(function (d) {
              var opt = document.createElement('option');
              opt.value = d;
              opt.textContent = d;
              sel.appendChild(opt);
            });

            sel.addEventListener('change', apply);
            apply();
          }

          fillSelect('upcoming-filter', '.upcoming-block', '.day-title-upcoming', true);
          fillSelect('settled-filter', '.settled-block', '.day-title-settled', true);
        })();
      </script>

      <!-- (dejé el resto de tus scripts tal cual, podés pegarlos debajo si siguen en tu archivo) -->

      <script>
    document.addEventListener('DOMContentLoaded', function() {
      var bar = document.getElementById('summary-bar');
      if (!bar) return;

      var league = bar.getAttribute('data-league') || '';
      if (!league) return;

      fetch('/api/stats/summary?league=' + encodeURIComponent(league))
        .then(function(r){ return r.ok ? r.json() : null; })
        .then(function(d){
          if (!d) return;

          var settled = (d.wins || 0) + (d.losses || 0) + (d.push || 0);

          var roiEl = document.getElementById('kpi-roi');
          if (roiEl) roiEl.textContent = (settled > 0 && d.roi != null) ? (d.roi + '%') : '—';

          var pr = document.getElementById('perf-strip-roi');
          var ptile = document.querySelector('.league-perf-block .perf-stat-tile--primary');
          if (pr && ptile) {
            var rtxt = (settled > 0 && d.roi != null) ? (String(d.roi).indexOf('%') >= 0 ? String(d.roi) : (d.roi + '%')) : '—';
            pr.textContent = rtxt;
            var rn = (settled > 0 && d.roi != null) ? Number(d.roi) : NaN;
            ptile.classList.remove('perf-stat-tile--pos','perf-stat-tile--neg','perf-stat-tile--flat','perf-stat-tile--neutral');
            var au = ptile.querySelector('.perf-stat-arrow--up');
            var ad = ptile.querySelector('.perf-stat-arrow--down');
            if (au) au.style.display = 'none';
            if (ad) ad.style.display = 'none';
            if (rtxt === '—' || isNaN(rn)) {
              ptile.classList.add('perf-stat-tile--neutral');
            } else if (rn > 0) {
              ptile.classList.add('perf-stat-tile--pos');
              if (au) au.style.display = 'inline';
            } else if (rn < 0) {
              ptile.classList.add('perf-stat-tile--neg');
              if (ad) ad.style.display = 'inline';
            } else {
              ptile.classList.add('perf-stat-tile--flat');
            }
          }

          var el;
          el = document.getElementById('kpi-total'); if (el) el.textContent = (d.total_picks != null) ? d.total_picks : '—';
          el = document.getElementById('kpi-wins'); if (el) el.textContent = (d.wins != null) ? d.wins : '—';
          el = document.getElementById('kpi-losses'); if (el) el.textContent = (d.losses != null) ? d.losses : '—';
          el = document.getElementById('kpi-pending'); if (el) el.textContent = (d.pending != null) ? d.pending : '—';

          var netEl = document.getElementById('kpi-net');
          var netCard = netEl && netEl.closest('.kpi-card');
          if (d.net_units != null && netEl) {
            var n = Number(d.net_units);
            netEl.textContent = (n >= 0 ? '+' : '') + n.toFixed(2);
            if (netCard) netCard.classList.add(n >= 0 ? 'pos' : 'neg');
          } else if (netEl) {
            netEl.textContent = '—';
          }
        })
        .catch(function(){});
    });
    </script>

    <script>
    (function(){
      function clamp(n,a,b){ return Math.max(a, Math.min(b, n)); }

      // FRONT: cand-fill (candidatos)
      function paintFront(){
        document.querySelectorAll('.flip-front .cand-fill[data-w]').forEach(function(el){
          var w = el.getAttribute('data-w');
          var pct = clamp(Number(w || 0), 0, 100);
          el.style.width = pct + '%';
        });
      }

      // BACK: bar-fill (stats)
      function paintBackBars(flipCard){
        if (!flipCard) return;
        flipCard.querySelectorAll('.flip-back .bar-fill[data-w]').forEach(function(el){
          var w = el.getAttribute('data-w');
          var pct = clamp(Number(w || 0), 0, 100);

          // reset + anim
          el.style.width = '0%';
          void el.offsetWidth;
          requestAnimationFrame(function(){
            el.style.width = pct + '%';
          });
        });
      }

      // pinta front al cargar
      document.addEventListener('DOMContentLoaded', function(){
        paintFront();
        setTimeout(paintFront, 60);
      });

      // si tu flip se hace por click y togglea .is-flipped
      document.addEventListener('click', function(e){
        var fc = e.target.closest && e.target.closest('.flip-card');
        if (!fc) return;
        if (e.target.closest('a,button')) return;

        requestAnimationFrame(function(){
          if (fc.classList.contains('is-flipped')) {
            paintBackBars(fc);
          } else {
            paintFront();
          }
        });
      });

      // soporte teclado (enter/space)
      document.addEventListener('keydown', function(e){
        if (e.key !== 'Enter' && e.key !== ' ') return;
        var fc = document.activeElement && document.activeElement.closest && document.activeElement.closest('.flip-card');
        if (!fc) return;

        requestAnimationFrame(function(){
          if (fc.classList.contains('is-flipped')) {
            paintBackBars(fc);
          } else {
            paintFront();
          }
        });
      });

      // por si querés llamarlo manual
      window.AFTR_paintFront = paintFront;
      window.AFTR_paintBackBars = function(){
        document.querySelectorAll('.flip-card.is-flipped').forEach(paintBackBars);
      };
    })();
    </script>

    </div> <!-- /page -->
    </body>
    </html>
    """

    return page_html


def _account_header(request: Request):
    """Build (user, auth_html, plan_badge) for account/admin pages."""
    uid = get_user_id(request)
    user = get_user_by_id(uid) if uid else None
    if uid and not user:
        # broken cookie: uid not in DB; treat as logged out (middleware clears cookie)
        uid, user = None, None
    auth_html = ""
    if user:
        display_name = html_lib.escape((user.get("username") or user.get("email") or ""))
        auth_html = (
            f'<span class="plan-badge">{display_name}</span>'
            f'<a class="plan-logout" href="/account">Mi cuenta</a>'
            f'<a class="plan-logout" href="/auth/logout">Salir</a>'
        )
    else:
        auth_html = (
            '<a class="pill" href="/?auth=login">Entrar</a>'
            '<a class="pill" href="/?auth=register">Crear cuenta</a>'
        )
    is_admin_user = is_admin(user, request)
    plan_badge = auth_html
    if is_admin_user:
        plan_badge = '<span class="plan-badge admin">ADMIN</span>' + auth_html
    return user, auth_html, plan_badge


def _account_created_display(created_at) -> str:
    """Format created_at for display (e.g. 2025-03-15 -> 15/03/2025 or 'desde 2025')."""
    if not created_at:
        return "—"
    s = str(created_at).strip()[:10]
    if len(s) >= 10:
        try:
            y, m, d = s[:4], s[5:7], s[8:10]
            return f"{d}/{m}/{y}"
        except Exception:
            pass
    return html_lib.escape(s)


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request):
    """Phase 2 account dashboard: greeting, plan, stats (from /user/*), quick actions, history preview."""
    user, _, plan_badge = _account_header(request)

    header_html = """
    <div class="account-header-row">
      <h1 class="account-header-title">Mi cuenta</h1>
      <a href="/" class="account-header-home">🏠 Volver al inicio</a>
    </div>"""

    if not user:
        body = header_html + """
    <div class="page account-page" style="max-width: 560px; margin: 24px auto;">
      <div class="card account-card" style="padding: 28px; text-align: center; background: var(--card-bg, #1a1a1a); border-radius: 12px;">
        <p class="account-greeting muted" style="font-size: 1.1rem; margin-bottom: 20px;">Iniciá sesión para ver tu cuenta</p>
        <div style="display: flex; gap: 12px; justify-content: center; flex-wrap: wrap;">
          <a href="/?auth=login" class="pill" style="padding: 10px 20px;">Entrar</a>
          <a href="/?auth=register" class="pill" style="padding: 10px 20px;">Crear cuenta</a>
        </div>
      </div>
    </div>"""
        return _simple_page("Mi cuenta — AFTR", body)

    display_name = (user.get("username") or user.get("email") or "Usuario").strip()
    display_name = html_lib.escape(display_name)
    email_display = html_lib.escape(str(user.get("email") or "").strip())
    created_display = _account_created_display(user.get("created_at"))
    uid = user.get("id")
    is_premium = is_premium_active(user) or (get_active_plan(uid) if uid else "") in (settings.plan_premium, settings.plan_pro)
    plan_label = "⭐ AFTR Premium activo" if is_premium else "Free plan"
    plan_class = "account-plan-premium" if is_premium else "account-plan-free"
    upgrade_cta = (
        ""
        if is_premium
        else '<p class="account-hero-upgrade muted"><a href="/?open=premium" class="account-hero-upgrade-link">Desbloquear Premium</a></p>'
    )

    premium_upsell_card = ""
    if not is_premium:
        premium_upsell_card = """
      <div class="card account-premium-teaser account-premium-cta">
        <div class="account-premium-teaser-glow" aria-hidden="true"></div>
        <h3 class="account-premium-teaser-title">Desbloqueá AFTR Premium</h3>
        <ul class="account-premium-teaser-list muted">
          <li>Picks elite y strong</li>
          <li>AFTR Score completo</li>
          <li>Historial y seguimiento avanzado</li>
        </ul>
        <a href="/?open=premium" class="pill account-premium-teaser-cta">Desbloquear Premium</a>
      </div>"""

    premium_wrapper_class = " account-page--premium" if is_premium else ""
    hero_class = "account-hero account-hero-premium" if is_premium else "account-hero"
    account_email_line = (
        f'<p class="account-email muted">{email_display}</p>' if email_display else ""
    )
    body = header_html + f"""
    <div class="page account-page account-page-wrapper{premium_wrapper_class}">
      <div class="card {hero_class} account-hero-card">
        <div class="account-hero-card-shine" aria-hidden="true"></div>
        <div class="account-hero-ident">
          <p class="account-greeting">Hola, {display_name}</p>
          {account_email_line}
        </div>
        <div class="account-hero-badge-row">
          {(
            '<span class="account-premium-badge-gold"><span class="account-premium-badge-gold-inner">Premium</span></span>'
            if is_premium
            else '<span class="account-type-pill account-type-pill--free">Free</span>'
          )}
          <span class="account-created muted">Miembro desde {created_display}</span>
        </div>
        {upgrade_cta}
        <div class="account-hero-summary">
          <div class="account-summary-item">
            <span class="account-summary-label muted">Seguidos</span>
            <span id="hero-followed" class="account-summary-value js-stat-anim">0</span>
          </div>
          <div class="account-summary-item">
            <span class="account-summary-label muted">Favoritos</span>
            <span id="hero-favorites" class="account-summary-value js-stat-anim">0</span>
          </div>
          <div class="account-summary-item">
            <span class="account-summary-label muted">Winrate</span>
            <span id="hero-winrate" class="account-summary-value js-stat-anim">—</span>
          </div>
          <div class="account-summary-item">
            <span class="account-summary-label muted">ROI</span>
            <span id="hero-roi" class="account-summary-value js-stat-anim">0%</span>
          </div>
          <div class="account-summary-item">
            <span class="account-summary-label muted">Racha</span>
            <span id="hero-streak" class="account-summary-value">—</span>
          </div>
        </div>
      </div>
{premium_upsell_card}

      <section class="account-block account-block--stats">
      <h3 class="account-section-title"><span class="account-section-title-accent">Dashboard</span></h3>
      <div id="account-stats" class="account-stats account-stats-grid">
        <div class="account-stat-card"><span class="account-stat-label muted">Seguidos</span><span id="stat-followed" class="account-stat-value js-stat-anim">0</span></div>
        <div class="account-stat-card"><span class="account-stat-label muted">Favoritos</span><span id="stat-favorites" class="account-stat-value js-stat-anim">0</span></div>
        <div class="account-stat-card"><span class="account-stat-label muted">Victorias</span><span id="stat-wins" class="account-stat-value js-stat-anim">0</span></div>
        <div class="account-stat-card"><span class="account-stat-label muted">Pérdidas</span><span id="stat-losses" class="account-stat-value js-stat-anim">0</span></div>
        <div class="account-stat-card"><span class="account-stat-label muted">Pendientes</span><span id="stat-pending" class="account-stat-value js-stat-anim">0</span></div>
        <div class="account-stat-card account-stat-card--pulse"><span class="account-stat-label muted">ROI</span><span id="stat-roi" class="account-stat-value js-stat-anim">0%</span></div>
        <div class="account-stat-card account-stat-card--pulse"><span class="account-stat-label muted">Winrate</span><span id="stat-winrate" class="account-stat-value js-stat-anim">—</span></div>
        <div class="account-stat-card"><span class="account-stat-label muted">Racha actual</span><span id="stat-streak" class="account-stat-value">—</span></div>
      </div>
      </section>

      <div class="account-actions">
        <a href="#seguidas" class="pill account-action-pill">Seguidas activas</a>
        <a href="#favoritos" class="pill account-action-pill">Favoritos</a>
        <a href="#historial" class="pill account-action-pill">Historial</a>
        <a href="/auth/logout" class="pill account-action-pill">Cerrar sesión</a>
      </div>

      <section id="seguidas" class="account-section account-section--picks">
        <h3 class="account-section-title"><span class="account-section-title-accent">Activas</span> · seguidas</h3>
        <div id="account-active-picks" class="account-favorites">
          <p class="muted">Cargando…</p>
        </div>
      </section>

      <section id="favoritos" class="account-section account-section--picks">
        <h3 class="account-section-title"><span class="account-section-title-accent">Favoritos</span></h3>
        <div id="account-favorites" class="account-favorites">
          <p class="muted">Cargando…</p>
        </div>
      </section>

      <section id="historial" class="account-section account-section--picks">
        <h3 class="account-section-title"><span class="account-section-title-accent">Historial</span> · reciente</h3>
        <div id="account-history" class="account-history">
          <p class="muted">Cargando…</p>
        </div>
      </section>

      <section id="account-insights" class="account-section account-section--insights">
        <h3 class="account-section-title"><span class="account-section-title-accent">Insights</span> · vos</h3>
        <div class="account-insights-grid">
          <div class="account-insight-card">
            <div class="account-insight-label muted">Mejor mercado</div>
            <div id="insight-best-market" class="account-insight-value">—</div>
          </div>
          <div class="account-insight-card">
            <div class="account-insight-label muted">Más seguido</div>
            <div id="insight-top-league" class="account-insight-value">—</div>
          </div>
          <div class="account-insight-card">
            <div class="account-insight-label muted">Racha actual</div>
            <div id="insight-streak" class="account-insight-value">—</div>
          </div>
          <div class="account-insight-card" style="grid-column: 1 / -1;">
            <div class="account-insight-label muted">Actividad reciente</div>
            <div id="insight-recent-activity" class="account-activity-list">Cargando…</div>
          </div>
        </div>
      </section>
    </div>
    <script>
    (function(){{
      var base = window.location.origin || (window.location.protocol + "//" + window.location.host);
      var knownLeagues = {json.dumps(list(settings.leagues.keys()))};
      var __accountDidCountUp = false;
      function esc(s) {{ if (s == null || s === "") return ""; return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }}
      function prefersReducedMotion() {{
        return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      }}
      function easeOutCubic(t) {{ return 1 - Math.pow(1 - t, 3); }}
      function animateValueEl(el, finalStr) {{
        if (!el || prefersReducedMotion()) return;
        var s = String(finalStr).trim();
        if (s === "—" || s === "") return;
        var m = /^([+-]?\\d+(?:\\.\\d+)?)\\s*(%)?$/.exec(s.replace(",", "."));
        if (!m) return;
        var end = parseFloat(m[1]);
        if (isNaN(end)) return;
        var suffix = m[2] || "";
        var start = 0;
        var t0 = performance.now();
        var dur = 580;
        function step(now) {{
          var p = easeOutCubic(Math.min(1, (now - t0) / dur));
          var cur = start + (end - start) * p;
          if (suffix === "%") el.textContent = cur.toFixed(1) + "%";
          else el.textContent = String(Math.round(cur));
          if (p < 1) requestAnimationFrame(step);
          else el.textContent = s;
        }}
        if (suffix === "%") el.textContent = "0.0%";
        else el.textContent = "0";
        requestAnimationFrame(step);
      }}
      function runInitialStatCountUp() {{
        if (__accountDidCountUp || prefersReducedMotion()) return;
        __accountDidCountUp = true;
        ["stat-followed","stat-favorites","stat-wins","stat-losses","stat-pending","stat-roi","stat-winrate","hero-followed","hero-favorites","hero-roi","hero-winrate"].forEach(function(id) {{
          var el = document.getElementById(id);
          if (!el) return;
          animateValueEl(el, el.textContent);
        }});
      }}
      function accountPickHeadHtml(market, teams, viewHref) {{
        var h = "<div class=\\"account-pick-head\\">";
        h += "<div class=\\"account-pick-head-main\\">";
        h += "<div class=\\"account-pick-title\\">" + market + "</div>";
        h += "<div class=\\"account-pick-subtitle\\">" + teams + "</div></div>";
        if (viewHref) h += "<a class=\\"account-pick-open\\" href=\\"" + esc(viewHref) + "\\" aria-label=\\"Abrir pick\\"><span class=\\"account-pick-open-icon\\" aria-hidden=\\"true\\"></span></a>";
        h += "</div>";
        return h;
      }}
      function fetchJSON(url) {{
        return fetch(url, {{ credentials: "include" }}).then(function(r) {{ return r.json(); }});
      }}
      function tierNice(t){{
        var v = (t == null ? "" : String(t)).trim().toLowerCase();
        if (!v) return "—";
        if (v === "pass" || v === "watch") return "WATCH";
        if (v === "elite") return "ELITE";
        if (v === "strong") return "STRONG";
        if (v === "risky") return "RISKY";
        return v.toUpperCase();
      }}
      function edgeNice(edgeVal){{
        if (edgeVal == null || edgeVal === "") return "—";
        var n = Number(edgeVal);
        if (isNaN(n)) return "—";
        var pct = n * 100;
        var sign = pct >= 0 ? "+" : "";
        return sign + pct.toFixed(1) + "%";
      }}
      function fmtDate(iso){{
        var s = (iso || "").toString();
        if (!s) return "—";
        return s.slice(0,10);
      }}
      function timeAgo(iso){{
        var s = (iso || "").toString();
        if (!s) return "—";
        var d = new Date(s);
        if (isNaN(d.getTime())) return fmtDate(s);
        var diffMs = Date.now() - d.getTime();
        if (diffMs < 0) diffMs = 0;
        var mins = Math.floor(diffMs / (60*1000));
        var hrs = Math.floor(diffMs / (60*60*1000));
        var days = Math.floor(diffMs / (24*60*60*1000));
        if (mins < 60) return "hace " + mins + "m";
        if (hrs < 24) return "hace " + hrs + "h";
        return "hace " + days + "d";
      }}
      function leagueFromPickId(pickId){{
        try {{
          var s = (pickId || "").toString();
          var parts = s.split("|");
          if (parts && parts.length > 0) return String(parts[0] || "").trim();
          return "";
        }} catch(e) {{
          return "";
        }}
      }}
      function panelHrefForPick(pickId){{
        var code = leagueFromPickId(pickId);
        if (!code) return "";
        if (Array.isArray(knownLeagues) && knownLeagues.length > 0) {{
          if (knownLeagues.indexOf(code) < 0) return "";
        }}
        return "/?league=" + encodeURIComponent(code);
      }}
      function computeCurrentStreak(historyList){{
        // historyList is expected newest-first (as returned by /user/history)
        var list = Array.isArray(historyList) ? historyList : [];
        var firstResolved = null;
        for (var i=0;i<list.length;i++) {{
          var r = (list[i] && list[i].result) ? String(list[i].result).toUpperCase() : "PENDING";
          if (r === "WIN" || r === "LOSS") {{ firstResolved = r; break; }}
          if (r === "PUSH") {{ firstResolved = "PUSH"; break; }}
        }}
        if (!firstResolved || firstResolved === "PUSH") {{
          return {{ count: 0, label: "—", kind: null }};
        }}
        var count = 0;
        for (var j=0;j<list.length;j++) {{
          var r2 = (list[j] && list[j].result) ? String(list[j].result).toUpperCase() : "PENDING";
          if (r2 === firstResolved) count++;
          else break;
        }}
        var label = count + " " + (firstResolved === "WIN" ? "WIN" : "LOSS");
        return {{ count: count, label: label, kind: firstResolved }};
      }}
      function updateStatEls(s) {{
        var el = function(id) {{ return document.getElementById(id); }};
        var num = function(v) {{ return (v != null && v !== "") ? Number(v) : 0; }};
        var roiVal = s.roi != null ? Number(s.roi) : 0;
        var totalWL = (num(s.wins) + num(s.losses));
        var winrate = totalWL > 0 ? (num(s.wins) / totalWL) * 100.0 : null;

        if (el("stat-followed")) el("stat-followed").textContent = num(s.followed_picks);
        if (el("stat-favorites")) el("stat-favorites").textContent = num(s.favorites_count);
        if (el("stat-wins")) el("stat-wins").textContent = num(s.wins);
        if (el("stat-losses")) el("stat-losses").textContent = num(s.losses);
        if (el("stat-pending")) el("stat-pending").textContent = num(s.pending);
        if (el("stat-roi")) el("stat-roi").textContent = roiVal.toFixed(1) + "%";
        if (el("stat-winrate")) el("stat-winrate").textContent = winrate != null ? winrate.toFixed(1) + "%" : "—";

        if (el("hero-followed")) el("hero-followed").textContent = num(s.followed_picks);
        if (el("hero-favorites")) el("hero-favorites").textContent = num(s.favorites_count);
        if (el("hero-roi")) el("hero-roi").textContent = roiVal.toFixed(1) + "%";
        if (el("hero-winrate")) el("hero-winrate").textContent = winrate != null ? winrate.toFixed(1) + "%" : "—";
      }}
      function refreshStats() {{
        fetchJSON(base + "/user/stats").then(function(stats) {{
          if (stats && stats.ok && stats.stats) updateStatEls(stats.stats);
        }});
      }}
      document.addEventListener("click", function(e) {{
        var btn = e.target.closest && e.target.closest(".btn-remove-fav");
        if (btn) {{
          e.preventDefault();
          var pickId = btn.getAttribute("data-pick-id");
          if (!pickId) return;
          btn.disabled = true;
          fetch(base + "/user/unfavorite", {{ method: "POST", headers: {{ "Content-Type": "application/json" }}, credentials: "include", body: JSON.stringify({{ pick_id: pickId }}) }})
            .then(function(r) {{ return r.json(); }})
            .then(function(d) {{
              if (d && d.ok) {{
                var card = btn.closest(".account-pick-card");
                if (card) card.remove();
                refreshStats();
                var cont = document.getElementById("account-favorites");
                if (cont && !cont.querySelector(".account-pick-card")) {{
                  cont.innerHTML = "<div class=\\"card\\" style=\\"padding: 20px;\\"><p class=\\"muted\\" style=\\"margin: 0;\\">No tenés favoritos todavía.</p></div>";
                }}
              }} else btn.disabled = false;
            }})
            .catch(function() {{ btn.disabled = false; }});
          return;
        }}
        var unfollowBtn = e.target.closest && e.target.closest(".btn-unfollow");
        if (unfollowBtn) {{
          e.preventDefault();
          var pickId = unfollowBtn.getAttribute("data-pick-id");
          if (!pickId) return;
          unfollowBtn.disabled = true;
          fetch(base + "/user/unfollow", {{ method: "POST", headers: {{ "Content-Type": "application/json" }}, credentials: "include", body: JSON.stringify({{ pick_id: pickId }}) }})
            .then(function(r) {{ return r.json(); }})
            .then(function(d) {{
              if (d && d.ok) {{
                var card = unfollowBtn.closest(".account-pick-card");
                if (card) card.remove();
                refreshStats();
                  var cont = document.getElementById("account-history");
                  if (cont && !cont.querySelector(".account-pick-card")) {{
                    cont.innerHTML = "<div class=\\"card\\" style=\\"padding: 20px;\\"><p class=\\"muted\\" style=\\"margin: 0;\\">No hay historial reciente.</p></div>";
                  }}
                  var activeCont = document.getElementById("account-active-picks");
                  if (activeCont && !activeCont.querySelector(".account-pick-card")) {{
                    activeCont.innerHTML = "<div class=\\"card\\" style=\\"padding: 20px;\\"><p class=\\"muted\\" style=\\"margin: 0;\\">No tenés picks activos.</p></div>";
                  }}
              }} else unfollowBtn.disabled = false;
            }})
            .catch(function() {{ unfollowBtn.disabled = false; }});
        }}
      }});
      Promise.all([fetchJSON(base + "/user/stats"), fetchJSON(base + "/user/history"), fetchJSON(base + "/user/favorites")]).then(function(results) {{
        var stats = results[0];
        var history = results[1];
        var favorites = results[2];
        var histList = (history && history.ok && Array.isArray(history.history)) ? history.history : [];
        var favList = (favorites && favorites.ok && Array.isArray(favorites.favorites)) ? favorites.favorites : [];
        if (stats && stats.ok && stats.stats) updateStatEls(stats.stats);
        runInitialStatCountUp();

        // Current streak + insights (best-effort from available history/favorites data)
        var streak = computeCurrentStreak(histList);
        var streakTxt = (streak && streak.count > 0) ? streak.label : "—";
        if (document.getElementById("stat-streak")) document.getElementById("stat-streak").textContent = streakTxt;
        if (document.getElementById("hero-streak")) document.getElementById("hero-streak").textContent = streakTxt;
        if (document.getElementById("insight-streak")) document.getElementById("insight-streak").textContent = streakTxt;

        // Best market (most frequent market across history + favorites)
        var marketCounts = {{}};
        [histList, favList].forEach(function(arr){{
          (arr || []).forEach(function(it){{
            var m = (it && it.market) ? String(it.market).trim() : "";
            if (!m || m === "-") return;
            marketCounts[m] = (marketCounts[m] || 0) + 1;
          }});
        }});
        var bestMarket = "—";
        var bestMarketN = 0;
        Object.keys(marketCounts).forEach(function(k){{
          if (marketCounts[k] > bestMarketN) {{ bestMarketN = marketCounts[k]; bestMarket = k; }}
        }});
        if (document.getElementById("insight-best-market")) document.getElementById("insight-best-market").textContent = esc(bestMarket);

        // Most followed league (infer from pick_id prefix)
        var leagueCounts = {{}};
        var allItems = (histList || []).concat(favList || []);
        allItems.forEach(function(it){{
          var pid = it ? it.pick_id : null;
          var code = leagueFromPickId(pid);
          if (!code) return;
          if (Array.isArray(knownLeagues) && knownLeagues.length > 0 && knownLeagues.indexOf(code) < 0) return;
          leagueCounts[code] = (leagueCounts[code] || 0) + 1;
        }});
        var topLeague = "—";
        var topLeagueN = 0;
        Object.keys(leagueCounts).forEach(function(k){{
          if (leagueCounts[k] > topLeagueN) {{ topLeagueN = leagueCounts[k]; topLeague = k; }}
        }});
        if (document.getElementById("insight-top-league")) document.getElementById("insight-top-league").textContent = esc(topLeague);

        // Recent activity (top 3) — card layout: status + date, market, match line
        var recentHtml = "";
        (histList || []).slice(0, 3).forEach(function(item){{
          if (!item) return;
          var r = (item.result || "PENDING").toString().toUpperCase();
          var resultClass = r.toLowerCase();
          var market = esc(item.market || "—");
          var dt = esc((item.created_at || "").slice(0,10));
          var home = esc(item.home || "");
          var away = esc(item.away || "");
          var matchLine = (home && away) ? (home + " vs " + away) : "";
          var finCls = (r === "WIN" || r === "LOSS" || r === "PUSH") ? " account-status-finished" : "";
          recentHtml += "<div class=\\"account-activity-feed-card\\">" +
            "<div class=\\"account-activity-feed-top\\">" +
            "<span class=\\"account-status-badge account-status-" + esc(resultClass) + finCls + "\\">" + esc(r) + "</span>" +
            "<span class=\\"account-activity-feed-date\\">" + (dt || "—") + "</span>" +
            "</div>" +
            "<div class=\\"account-activity-feed-market\\">" + market + "</div>" +
            (matchLine ? "<div class=\\"account-activity-feed-match muted\\">" + matchLine + "</div>" : "") +
            "</div>";
        }});
        var activityEl = document.getElementById("insight-recent-activity");
        if (activityEl) activityEl.innerHTML = recentHtml || "<span class=\\"muted\\">—</span>";

        if (favorites && favorites.ok && Array.isArray(favorites.favorites)) {{
          var list = favorites.favorites;
          var container = document.getElementById("account-favorites");
          if (container) {{
            if (list.length === 0) {{
              container.innerHTML = "<div class=\\"card\\" style=\\"padding: 20px;\\"><p class=\\"muted\\" style=\\"margin: 0;\\">No tenés favoritos todavía.</p></div>";
            }} else {{
              var html = "";
              list.forEach(function(item) {{
                var market = esc(item.market || "—");
                var aftrScore = item.aftr_score != null ? item.aftr_score : "—";
                var tierTxt = tierNice(item.tier);
                var tierKey = (item.tier || "").toString().trim().toLowerCase();
                if (tierKey === "pass") tierKey = "watch";
                if (!tierKey) tierKey = "watch";
                var edgeTxt = edgeNice(item.edge);
                var edgeNum = item.edge != null ? Number(item.edge) : null;
                var edgeKey = edgeTxt === "—" ? "neutral" : (edgeNum >= 0 ? "pos" : "neg");
                var home = esc(item.home || "");
                var away = esc(item.away || "");
                var teams = (home && away) ? (home + " vs " + away) : "Partido no disponible";
                var savedWhen = timeAgo(item.created_at);
                var pickId = esc(item.pick_id || "");
                var viewHref = panelHrefForPick(item.pick_id || "");
                var highlightClass = (tierKey === "elite" || tierKey === "strong") ? " account-pick-highlight" : "";
                html += "<div class=\\"account-pick-card account-pick-fav" + highlightClass + "\\" data-pick-id=\\"" + pickId + "\\">";
                html += accountPickHeadHtml(market, teams, viewHref);
                html += "<div class=\\"account-badge-row\\">";
                html += "<span class=\\"account-mini-badge account-badge-aftr\\">AFTR <b>" + esc(aftrScore) + "</b></span>";
                html += "<span class=\\"account-mini-badge account-badge-tier account-badge-tier-" + esc(tierKey) + "\\">" + esc(tierTxt) + "</span>";
                html += "<span class=\\"account-mini-badge account-badge-edge account-badge-edge-" + esc(edgeKey) + "\\">" + esc(edgeTxt) + " EDGE</span>";
                html += "</div>";
                html += "<div class=\\"account-pick-bottom\\">";
                html += "<span class=\\"account-pick-muted\\">Guardado " + savedWhen + "</span>";
                if (viewHref) {{
                  html += "<a class=\\"account-card-link\\" href=\\"" + esc(viewHref) + "\\">Ver en panel</a>";
                }}
                html += "<button type=\\"button\\" class=\\"btn-remove-fav account-card-action account-card-action--warn\\" data-pick-id=\\"" + pickId + "\\">Quitar favoritos</button>";
                html += "</div>";
                html += "</div>";
              }});
              container.innerHTML = html;
            }}
          }}
        }} else {{
          var favEl = document.getElementById("account-favorites");
          if (favEl) favEl.innerHTML = "<div class=\\"card\\" style=\\"padding: 20px;\\"><p class=\\"muted\\" style=\\"margin: 0;\\">No tenés favoritos todavía.</p></div>";
        }}

        // Active followed picks (PENDING only)
        var activeCont = document.getElementById("account-active-picks");
        if (activeCont) {{
          var active = (histList || []).filter(function(it){{
            var r = (it && it.result) ? String(it.result).toUpperCase() : "PENDING";
            return r === "PENDING";
          }});
          if (!active.length) {{
            activeCont.innerHTML = "<div class=\\"card\\" style=\\"padding: 20px;\\"><p class=\\"muted\\" style=\\"margin: 0;\\">No tenés picks activos.</p></div>";
          }} else {{
            var html = "";
            active.slice(0, 6).forEach(function(item){{
              var market = esc(item.market || "—");
              var aftrScore = item.aftr_score != null ? item.aftr_score : "—";
              var tierTxt = tierNice(item.tier);
              var tierKey = (item.tier || "").toString().trim().toLowerCase();
              if (tierKey === "pass") tierKey = "watch";
              if (!tierKey) tierKey = "watch";
              var edgeTxt = edgeNice(item.edge);
              var edgeNum = item.edge != null ? Number(item.edge) : null;
              var edgeKey = edgeTxt === "—" ? "neutral" : (edgeNum >= 0 ? "pos" : "neg");
              var home = esc(item.home || "");
              var away = esc(item.away || "");
              var teams = (home && away) ? (home + " vs " + away) : "Partido no disponible";
              var result = "PENDING";
              var date = esc((item.created_at || "").slice(0, 10));
              var pickId = esc(item.pick_id || "");
              var viewHref = panelHrefForPick(item.pick_id || "");
              var highlightClassA = (tierKey === "elite" || tierKey === "strong") ? " account-pick-highlight" : "";

              html += "<div class=\\"account-pick-card account-pick-active" + highlightClassA + "\\" data-pick-id=\\"" + pickId + "\\">";
              html += accountPickHeadHtml(market, teams, viewHref);
              html += "<div class=\\"account-badge-row\\">";
              html += "<span class=\\"account-mini-badge account-badge-aftr\\">AFTR <b>" + esc(aftrScore) + "</b></span>";
              html += "<span class=\\"account-mini-badge account-badge-tier account-badge-tier-" + esc(tierKey) + "\\">" + esc(tierTxt) + "</span>";
              html += "<span class=\\"account-mini-badge account-badge-edge account-badge-edge-" + esc(edgeKey) + "\\">" + esc(edgeTxt) + " EDGE</span>";
              html += "</div>";
              html += "<div class=\\"account-pick-bottom\\">";
              html += "<span class=\\"account-status-badge account-status-pending account-live-pulse\\"><span class=\\"account-live-dot\\" aria-hidden=\\"true\\"></span>Pendiente</span>";
              html += "<span class=\\"account-pick-date\\">" + date + "</span>";
              if (viewHref) {{
                html += "<a class=\\"account-card-link\\" href=\\"" + esc(viewHref) + "\\">Panel</a>";
              }}
              html += "<button type=\\"button\\" class=\\"btn-unfollow account-card-action account-card-action--warn\\" data-pick-id=\\"" + pickId + "\\">Dejar de seguir</button>";
              html += "</div>";
              html += "</div>";
            }});
            activeCont.innerHTML = html;
          }}
        }}

        if (history && history.ok && Array.isArray(history.history)) {{
          var list = history.history.filter(function(item){{
            var r = (item && item.result) ? String(item.result).toUpperCase() : "PENDING";
            return r !== "PENDING";
          }});
          var container = document.getElementById("account-history");
          if (!container) return;
          if (list.length === 0) {{
            container.innerHTML = "<div class=\\"card\\" style=\\"padding: 20px;\\"><p class=\\"muted\\" style=\\"margin: 0;\\">No hay historial reciente.</p></div>";
          }} else {{
            var html = "";
            list.forEach(function(item) {{
              var market = esc(item.market || "—");
              var aftrScore = item.aftr_score != null ? item.aftr_score : "—";
              var tierTxt = tierNice(item.tier);
              var tierKey = (item.tier || "").toString().trim().toLowerCase();
              if (tierKey === "pass") tierKey = "watch";
              if (!tierKey) tierKey = "watch";
              var edgeTxt = edgeNice(item.edge);
              var edgeNum = item.edge != null ? Number(item.edge) : null;
              var edgeKey = edgeTxt === "—" ? "neutral" : (edgeNum >= 0 ? "pos" : "neg");
              var home = esc(item.home || "");
              var away = esc(item.away || "");
              var finalScore = esc(item.final_score || "");
              var teams = (home && away) ? (home + " vs " + away) : "Partido no disponible";
              if(finalScore){{
                if(home && away) teams = teams + " · " + finalScore;
                else teams = "Final " + finalScore;
              }}
              var result = (item.result || "PENDING").toUpperCase();
              var date = esc((item.created_at || "").slice(0, 10));
              var pickId = esc(item.pick_id || "");
              var viewHref = panelHrefForPick(item.pick_id || "");
              var highlightClassH = (tierKey === "elite" || tierKey === "strong") ? " account-pick-highlight" : "";
              html += "<div class=\\"account-pick-card account-pick-history" + highlightClassH + "\\" data-pick-id=\\"" + pickId + "\\">";
              html += accountPickHeadHtml(market, teams, viewHref);
              html += "<div class=\\"account-badge-row\\">";
              html += "<span class=\\"account-mini-badge account-badge-aftr\\">AFTR <b>" + esc(aftrScore) + "</b></span>";
              html += "<span class=\\"account-mini-badge account-badge-tier account-badge-tier-" + esc(tierKey) + "\\">" + esc(tierTxt) + "</span>";
              html += "<span class=\\"account-mini-badge account-badge-edge account-badge-edge-" + esc(edgeKey) + "\\">" + esc(edgeTxt) + " EDGE</span>";
              html += "</div>";
              html += "<div class=\\"account-pick-bottom account-history-bottom\\">";
              html += "<span class=\\"account-status-badge account-status-" + esc(result.toLowerCase()) + " account-status-finished\\">" + result + "</span>";
              html += "<span class=\\"account-pick-date\\">" + date + "</span>";
              if (viewHref) {{
                html += "<a class=\\"account-card-link\\" href=\\"" + esc(viewHref) + "\\">Ver en panel</a>";
              }}
              html += "<button type=\\"button\\" class=\\"btn-unfollow account-card-action account-card-action--warn\\" data-pick-id=\\"" + pickId + "\\">Dejar de seguir</button>";
              html += "</div>";
              html += "</div>";
            }});
            container.innerHTML = html;
          }}
        }} else {{
          var c = document.getElementById("account-history");
          if (c) c.innerHTML = "<div class=\\"card\\" style=\\"padding: 20px;\\"><p class=\\"muted\\" style=\\"margin: 0;\\">No se pudo cargar el historial.</p></div>";
        }}
      }}).catch(function() {{
        var c = document.getElementById("account-history");
        if (c) c.innerHTML = "<p class=\\"muted\\" style=\\"margin: 0;\\">Error al cargar datos.</p>";
        var favEl = document.getElementById("account-favorites");
        if (favEl) favEl.innerHTML = "<div class=\\"card\\" style=\\"padding: 16px;\\"><p class=\\"muted\\" style=\\"margin: 0;\\">Error al cargar favoritos.</p></div>";
      }});
    }})();
    </script>"""
    return _simple_page("Mi cuenta — AFTR", body)


@router.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request):
    """Admin-only: list users (id, email, username, role, subscription_status, created_at). No password hashes."""
    user, _, plan_badge = _account_header(request)
    if not user:
        return RedirectResponse(url="/?auth=login", status_code=302)
    if not is_admin(user, request):
        return RedirectResponse(url="/", status_code=302)
    from app.db import get_conn
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT id, email, username, role, subscription_status, created_at
           FROM users ORDER BY id"""
    )
    rows = cur.fetchall()
    conn.close()
    user_rows = []
    for r in rows:
        uid = r["id"]
        email = html_lib.escape(str(r.get("email") or ""))
        username = html_lib.escape(str(r.get("username") or "—"))
        role = html_lib.escape(str(r.get("role") or "—"))
        sub = html_lib.escape(str(r.get("subscription_status") or "—"))
        created = r.get("created_at") or "—"
        if isinstance(created, str) and len(created) >= 10:
            created = created[:10]
        created = html_lib.escape(str(created))
        user_rows.append(f"<tr><td>{uid}</td><td>{email}</td><td>{username}</td><td>{role}</td><td>{sub}</td><td>{created}</td></tr>")
    table_body = "\n".join(user_rows)
    body = f"""
    <div class="top top-pro">
      <div class="brand">
        <img src="/static/logo_aftr.png" class="logo-aftr" alt="AFTR" />
        <div class="brand-text">
          <div class="brand-title">AFTR</div>
          <div class="brand-tag">Admin — Usuarios</div>
        </div>
        {plan_badge}
      </div>
    </div>
    <div class="top-actions">
      <div class="links">
        <a href="/">Panel</a>
        <a href="/account">Mi cuenta</a>
      </div>
    </div>
    <div class="page" style="max-width: 900px; margin: 24px auto;">
      <h2>Usuarios</h2>
      <p class="muted">Base de datos: aftr.sqlite3 — tabla: users. Sin contraseñas.</p>
      <div class="card" style="padding: 12px; overflow-x: auto;">
        <table style="width:100%; border-collapse: collapse; font-size: 13px;">
          <thead><tr><th style="text-align:left;">id</th><th style="text-align:left;">email</th><th style="text-align:left;">username</th><th>role</th><th>suscripción</th><th>created_at</th></tr></thead>
          <tbody>{table_body}</tbody>
        </table>
      </div>
    </div>
    """
    return _simple_page("Usuarios — AFTR Admin", body)


def _simple_page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>{html_lib.escape(title)}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="stylesheet" href="/static/style.css?v=22">
  <link rel="icon" type="image/png" href="/static/logo_aftr.png">
</head>
<body>
  {body}
  """ + AUTH_BOOTSTRAP_SCRIPT + """
</body>
</html>"""


@router.get("/reset-password", response_class=HTMLResponse)
def reset_password_form_page(request: Request, token: str = Query("")):
    """Reset password form (token in query)."""
    if not token or not token.strip():
        return RedirectResponse(url="/?msg=reset_token_invalido", status_code=302)
    tok = html_lib.escape(token)
    body = f"""
    <div class="top top-pro">
      <div class="brand">
        <img src="/static/logo_aftr.png" class="logo-aftr" alt="AFTR" />
        <div class="brand-text">
          <div class="brand-title">AFTR</div>
          <div class="brand-tag">Nueva contraseña</div>
        </div>
      </div>
    </div>
    <div class="page" style="max-width: 400px; margin: 24px auto;">
      <h2>Nueva contraseña</h2>
      <div id="reset-error" class="modal-line" style="color:#c00; display:none;"></div>
      <form id="reset-form" onsubmit="return submitReset(event);">
        <input type="hidden" name="token" value="{tok}">
        <div class="modal-line">
          <input type="password" id="reset-password" class="email-input" placeholder="Nueva contraseña" required autocomplete="new-password">
        </div>
        <div class="modal-line">
          <input type="password" id="reset-confirm" class="email-input" placeholder="Confirmar contraseña" required autocomplete="new-password">
        </div>
        <button type="submit" class="pill modal-cta" style="width:100%;">Actualizar contraseña</button>
      </form>
      <p class="muted" style="margin-top: 16px;"><a href="/">Volver al inicio</a></p>
    </div>
    <script>
    function submitReset(e) {{
      e.preventDefault();
      var token = document.querySelector('input[name=token]').value;
      var password = document.getElementById('reset-password').value;
      var confirm = document.getElementById('reset-confirm').value;
      var errEl = document.getElementById('reset-error');
      errEl.style.display = 'none';
      if (!password) {{ errEl.textContent = 'La contraseña es obligatoria.'; errEl.style.display = 'block'; return false; }}
      if (password !== confirm) {{ errEl.textContent = 'Las contraseñas no coinciden.'; errEl.style.display = 'block'; return false; }}
      fetch((window.location.origin || (window.location.protocol + '//' + window.location.host)) + '/auth/reset-password', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        credentials: 'include',
        body: JSON.stringify({{ token: token, password: password, confirm_password: confirm }})
      }})
      .then(function(r) {{ return r.json().then(function(d) {{ return {{ ok: r.ok, data: d }}; }}); }})
      .then(function(result) {{
        if (result.ok && result.data.ok) {{
          window.location.href = '/?msg=password_actualizada';
        }} else {{
          var msg = result.data.error || 'Error al actualizar.';
          if (result.data.error === 'token_invalido_o_expirado') msg = 'El enlace expiró o ya fue usado. Pedí uno nuevo.';
          else if (result.data.error === 'password_demasiado_larga') msg = 'La contraseña es demasiado larga. Usá hasta 72 caracteres.';
          errEl.textContent = msg;
          errEl.style.display = 'block';
        }}
      }})
      .catch(function() {{ errEl.textContent = 'Error de conexión.'; errEl.style.display = 'block'; }});
      return false;
    }}
    </script>
    """
    return _simple_page("Nueva contraseña — AFTR", body)