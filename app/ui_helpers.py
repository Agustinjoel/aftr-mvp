"""
Utilidades puras del módulo UI: safe casts, parsers de fecha, helpers de mercado,
manejo de cookies de sesión y plan. Sin dependencias de renderizado HTML.
"""
from __future__ import annotations

import html as html_lib
from datetime import datetime, timezone
from typing import Any

from fastapi import Request
from fastapi.responses import RedirectResponse
from itsdangerous import URLSafeSerializer, BadSignature

from app.timefmt import parse_utc_instant
from config.settings import settings


# =========================================================
# Auth modal JS bootstrap (inyectado en todas las páginas)
# =========================================================

AUTH_BOOTSTRAP_JS = r"""
window.openLoginModal = window.openLoginModal || function () {
  var m = document.getElementById("login-modal");
  if (m) { m.style.display = "flex"; return; }
  window.location.href = "/?auth=login";
};
window.closeLoginModal = window.closeLoginModal || function () {
  var m = document.getElementById("login-modal");
  if (m) m.style.display = "none";
};
window.openSignupModal = window.openSignupModal || function () {
  var m = document.getElementById("signup-modal");
  if (m) { m.style.display = "flex"; return; }
  window.location.href = "/?auth=register";
};
window.closeSignupModal = window.closeSignupModal || function () {
  var m = document.getElementById("signup-modal");
  if (m) m.style.display = "none";
};
"""
AUTH_BOOTSTRAP_SCRIPT = "<script>" + AUTH_BOOTSTRAP_JS + "</script>"


# =========================================================
# Safe casts
# =========================================================

def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return float(default)


def _safe_int(x: Any, default: Any = None) -> Any:
    try:
        if x is None:
            return default
        return int(x)
    except Exception:
        return default


# =========================================================
# Parsers de fecha
# =========================================================

def _parse_utcdate_str(s: Any) -> datetime:
    """Parsea ISO string a datetime UTC. Fallback a now() si falla."""
    try:
        if isinstance(s, str) and s:
            if s.endswith("Z"):
                return datetime.fromisoformat(s.replace("Z", "+00:00"))
            return datetime.fromisoformat(s)
    except Exception:
        pass
    return datetime.now(timezone.utc)


def _parse_utcdate_maybe(s: object) -> datetime | None:
    """Parsea ISO string a datetime UTC. Devuelve None si falla (sin fallback)."""
    return parse_utc_instant(s)


# =========================================================
# Helpers de mercado
# =========================================================

def _norm_market(m: str | None) -> str:
    m = (m or "").strip()
    if not m:
        return "UNKNOWN"
    up = m.upper()
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
    return m


def _pick_market(p: dict) -> str:
    m = p.get("best_market")
    if m:
        return _norm_market(m)
    cands = p.get("candidates") or []
    if isinstance(cands, list) and cands:
        m2 = (cands[0] or {}).get("market")
        if m2:
            return _norm_market(m2)
    return "UNKNOWN"


# =========================================================
# Validación de pick
# =========================================================

def _is_pick_valid(p: dict) -> bool:
    """True si el pick tiene los datos mínimos para mostrarse en la UI."""
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


# =========================================================
# Cookies de plan
# =========================================================

def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(settings.secret_key, salt="aftr-premium")


def _get_user_id(request: Request) -> int | None:
    raw = (getattr(request, "cookie", None) or {}).get("aftr_user")
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


def _set_plan_cookie(resp: RedirectResponse, plan: str) -> None:
    plan = (plan or settings.plan_free).upper()
    if plan not in (settings.plan_free, settings.plan_premium, settings.plan_pro):
        plan = settings.plan_free
    token = _serializer().dumps({"plan": plan})
    resp.set_cookie(
        key="aftr_plan",
        value=token,
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _clear_plan_cookie(resp: RedirectResponse) -> None:
    resp.delete_cookie("aftr_plan", path="/")


# =========================================================
# Cache status HTML
# =========================================================

def _format_cache_status(meta: dict) -> str:
    """Genera HTML para la barra de estado: Última actualización / Actualizando datos."""
    if meta.get("refresh_running"):
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


# =========================================================
# League pills (navegación)
# =========================================================

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
    """Liga a destacar en el home carousel (por URL o default)."""
    raw = (request.query_params.get("league") or "").strip()
    if raw and settings.is_valid_league(raw):
        return raw
    return settings.default_league
