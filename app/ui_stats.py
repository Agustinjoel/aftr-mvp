"""
Rendering del back del flip card: stats de equipo, forma, barras de tendencias.
Sin dependencias de FastAPI ni de otros módulos de la app (solo html + ui_helpers).
"""
from __future__ import annotations

import html as html_lib

from app.ui_helpers import _safe_float


# =========================================================
# Primitivos de stats
# =========================================================

def _stat_line(label: str, home_val, away_val) -> str:
    return (
        f'<div class="statline">'
        f'<div class="statlabel">{html_lib.escape(label)}</div>'
        f'<div class="statval">{html_lib.escape(str(home_val))}</div>'
        f'<div class="teamcol">{html_lib.escape(str(away_val))}</div>'
        f'</div>'
    )


def _wdl_badge(letter: str) -> str:
    l   = (letter or "").upper().strip()
    cls = "wdl-w" if l == "W" else ("wdl-l" if l == "L" else "wdl-d")
    return f'<span class="wdl {cls}">{html_lib.escape(l or "—")}</span>'


def _pct_class(pct: float) -> str:
    if pct >= 75:
        return "fill-high"
    if pct >= 55:
        return "fill-mid"
    return "fill-low"


def _market_key(m: str) -> str:
    m = (m or "").strip().upper()
    if m in ("1", "X", "2", "1X", "X2", "12"):
        return "RES"
    if "OVER" in m or "O/" in m or "O/U" in m:
        return "OVER"
    if "BTTS" in m or "AMBOS" in m:
        return "BTTS"
    return "GEN"


def _to_pct01(x) -> float | None:
    """Convierte prob 0..1 a porcentaje 0..100."""
    try:
        if x is None:
            return None
        return max(0.0, min(100.0, float(x) * 100.0))
    except Exception:
        return None


# =========================================================
# Barras y chips de forma
# =========================================================

def _bar_single(label: str, left_pct: float | None, right_pct: float | None) -> str:
    """Dos barras separadas, cada una muestra su % real (no relativo)."""
    left_txt  = f"{round(left_pct)}%"  if left_pct  is not None else "—"
    right_txt = f"{round(right_pct)}%" if right_pct is not None else "—"
    left_w    = max(0.0, min(100.0, float(left_pct)))  if left_pct  is not None else 0.0
    right_w   = max(0.0, min(100.0, float(right_pct))) if right_pct is not None else 0.0
    left_cls  = _pct_class(float(left_pct  or 0.0))
    right_cls = _pct_class(float(right_pct or 0.0))

    return (
        f'<div class="bar-row">'
        f'<div class="bar-head">'
        f'<span>{html_lib.escape(label)}</span>'
        f'<span class="muted">{left_txt} • {right_txt}</span>'
        f'</div>'
        f'<div class="bar-track">'
        f'<div class="bar-fill left {left_cls}" data-w="{left_w}"></div>'
        f'</div>'
        f'<div class="bar-track" style="margin-top:8px;">'
        f'<div class="bar-fill right {right_cls}" data-w="{right_w}"></div>'
        f'</div>'
        f'</div>'
    )


def _chips_from_form(form_str: str, max_n: int = 5) -> str:
    parts = [x.strip().upper() for x in (form_str or "").replace("-", " ").split() if x.strip()]
    parts = parts[:max_n]
    out   = []
    for x in parts:
        if x == "W":
            out.append('<span class="chip w">W</span>')
        elif x == "D":
            out.append('<span class="chip d">D</span>')
        elif x == "L":
            out.append('<span class="chip l">L</span>')
    return "".join(out) if out else '<span class="muted">—</span>'


# =========================================================
# Back del flip card
# =========================================================

def _render_back_stats(p: dict, market: str = "") -> str:
    """Cara posterior del flip card: forma reciente, GF/GA y tendencias de mercado."""
    stats_home = p.get("stats_home") if isinstance(p.get("stats_home"), dict) else {}
    stats_away = p.get("stats_away") if isinstance(p.get("stats_away"), dict) else {}

    # Basketball: bloque compacto simplificado
    if (p.get("model") or "").strip().upper() == "BASKETBALL":
        form_h = stats_home.get("form", "")
        form_a = stats_away.get("form", "")
        pick_text = market or "—"
        form_chips_h = _chips_from_form(str(form_h), 5) if form_h else ""
        form_chips_a = _chips_from_form(str(form_a), 5) if form_a else ""
        return (
            f'<div class="back-card back-compact">'
            f'<div class="back-sub">Pick: <b>{html_lib.escape(str(pick_text))}</b></div>'
            f'<div class="back-divider"></div>'
            f'<div class="back-form-compact">'
            f'<div class="back-form-head">'
            f'<span class="back-form-title">ÚLTIMOS</span>'
            f'<span class="back-form-sub muted">5</span>'
            f'</div>'
            f'<div class="back-form-row">'
            f'<div class="back-form-team">'
            f'<div class="back-form-legend muted">H</div>'
            f'<div class="back-form-chips">{form_chips_h}</div>'
            f'</div>'
            f'<div class="back-form-team right">'
            f'<div class="back-form-legend muted">A</div>'
            f'<div class="back-form-chips">{form_chips_a}</div>'
            f'</div>'
            f'</div>'
            f'</div>'
            f'</div>'
        )

    gf_h   = stats_home.get("gf", "—")
    ga_h   = stats_home.get("ga", "—")
    form_h = stats_home.get("form", "")
    gf_a   = stats_away.get("gf", "—")
    ga_a   = stats_away.get("ga", "—")
    form_a = stats_away.get("form", "")

    over_h_pct = _to_pct01(stats_home.get("over25"))
    over_a_pct = _to_pct01(stats_away.get("over25"))
    btts_h_pct = _to_pct01(stats_home.get("btts"))
    btts_a_pct = _to_pct01(stats_away.get("btts"))

    # Insight contextual por mercado
    mk = _market_key(market)
    if mk == "OVER":
        insight = "Enfoque: más de 2.5 y presión ofensiva"
    elif mk == "BTTS":
        insight = "Enfoque: BTTS por empuje en ambos lados"
    elif mk == "RES":
        insight = "Enfoque: ataque vs defensa + forma"
    else:
        insight = "Enfoque: forma y tendencia estadística"

    def _safe_chips(v) -> str:
        if not v:
            return ""
        s = str(v).strip()
        if not s:
            return ""
        chips = _chips_from_form(s, 5)
        return "" if "—" in chips else chips

    form_html = (
        f'<div class="back-form-compact">'
        f'<div class="back-form-head">'
        f'<span class="back-form-title">ÚLTIMOS</span>'
        f'<span class="back-form-sub muted">5</span>'
        f'</div>'
        f'<div class="back-form-row">'
        f'<div class="back-form-team">'
        f'<div class="back-form-legend muted">H</div>'
        f'<div class="back-form-chips">{_safe_chips(form_h)}</div>'
        f'</div>'
        f'<div class="back-form-team right">'
        f'<div class="back-form-legend muted">A</div>'
        f'<div class="back-form-chips">{_safe_chips(form_a)}</div>'
        f'</div>'
        f'</div>'
        f'</div>'
    )

    def _has(v) -> bool:
        if v is None:
            return False
        s = str(v).strip()
        return bool(s) and s != "—" and s.lower() != "none"

    def _fmt(v) -> str:
        return html_lib.escape(str(v)) if _has(v) else "—"

    gfga_html = ""
    if _has(gf_h) or _has(gf_a) or _has(ga_h) or _has(ga_a):
        gfga_html = (
            f'<div class="back-gg-compact">'
            f'<div class="back-gg-col">'
            f'<div class="back-gg-label">GF</div>'
            f'<div class="back-gg-values">'
            f'<span class="back-gg-num">{_fmt(gf_h)}</span>'
            f'<span class="back-gg-vs">vs</span>'
            f'<span class="back-gg-num right">{_fmt(gf_a)}</span>'
            f'</div>'
            f'</div>'
            f'<div class="back-gg-col">'
            f'<div class="back-gg-label">GA</div>'
            f'<div class="back-gg-values">'
            f'<span class="back-gg-num">{_fmt(ga_h)}</span>'
            f'<span class="back-gg-vs">vs</span>'
            f'<span class="back-gg-num right">{_fmt(ga_a)}</span>'
            f'</div>'
            f'</div>'
            f'</div>'
        )

    btts_html = _bar_single("BTTS", btts_h_pct, btts_a_pct) if (btts_h_pct is not None or btts_a_pct is not None) else ""
    over_html = _bar_single("Más de 2.5", over_h_pct, over_a_pct) if (over_h_pct is not None or over_a_pct is not None) else ""
    trends_html = ""
    if btts_html or over_html:
        trends_html = f'<div class="back-bars back-bars-compact">{"".join(x for x in [btts_html, over_html] if x)}</div>'

    blocks  = [b for b in [form_html, gfga_html, trends_html] if b]
    sections = ""
    for b in blocks:
        if sections:
            sections += '<div class="back-divider"></div>'
        sections += b

    return (
        f'<div class="back-card back-compact">'
        f'{sections}'
        f'<div class="back-insight muted">{html_lib.escape(insight)}</div>'
        f'</div>'
    )
