"""
Cálculos puros sobre picks: scoring, staking, grouping y normalización de resultados.
Sin dependencias de renderizado HTML ni de FastAPI Request.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.timefmt import parse_utc_instant
from app.ui_helpers import _safe_float, _safe_int, _parse_utcdate_str, _pick_market
from core.poisson import market_priority


# =========================================================
# Normalización de resultado
# =========================================================

def _result_norm(p: dict) -> str:
    r = (p.get("result") or "").strip().upper()
    spanish_to_english = {
        "EMPUJAR": "PUSH",
        "EMPATAR": "PUSH",
        "EMPATE":  "PUSH",
        "DRAW":    "PUSH",
        "GANAR":   "WIN",
        "GANA":    "WIN",
        "PERDER":  "LOSS",
        "PIERDE":  "LOSS",
    }
    if r in ("WIN", "LOSS", "PUSH", "PENDING"):
        return r
    if r in spanish_to_english:
        return spanish_to_english[r]
    return "PENDING"


# =========================================================
# Stake / unit helpers
# =========================================================

def _suggest_units(p: dict) -> str:
    c = _safe_int(p.get("confidence"))
    if c is None:
        return "—"
    if c >= 8:
        return "1.0u"
    if c >= 5:
        return "0.6u"
    return "0.3u"


def _unit_delta(p: dict) -> float:
    """Delta de unidades para una pick resuelta.
    Usa profit_units/net_units si existen; sino WIN=+1, LOSS=-1, PUSH=0.
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
    """Stake en unidades para denominar ROI. Prefiere campos explícitos; default 1u."""
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


# =========================================================
# Risk label
# =========================================================

def _risk_label_from_conf(p: dict) -> str:
    c = _safe_int(p.get("confidence"))
    if c is None:
        return "—"
    if c >= 8:
        return "SAFE"
    if c >= 5:
        return "MEDIUM"
    return "SPICY"


# =========================================================
# Pick scoring
# =========================================================

def _pick_score(p: dict) -> float:
    """Ranking score: prob + conf always; edge added when present (football with odds)."""
    bp = _safe_float(p.get("best_prob"))          # 0..1
    edge_val = p.get("edge")
    conf = _safe_float(p.get("confidence")) / 10.0  # 0..1

    score = (bp * 0.65) + (conf * 0.15)
    if edge_val is not None:
        try:
            score += float(edge_val) * 1.20
        except (TypeError, ValueError):
            pass

    if (p.get("model") or "").strip().upper() == "B":
        score += 0.03

    return score


def _aftr_score(p: dict) -> int:
    """
    AFTR Score 0-100 para mostrar en cards.
    Usa best_prob (principal) + confidence (secundario) + edge positivo como bonus.
    Compatible con NBA (sin edge).
    """
    bp = _safe_float(p.get("best_prob"), 0)
    conf_raw = _safe_int(p.get("confidence"))
    if conf_raw is None:
        conf_norm = 0.5
    else:
        conf_norm = max(0, min(1, (conf_raw - 1) / 9.0))  # 1-10 -> 0-1
    score = (bp * 60) + (conf_norm * 30)           # 0-60 + 0-30 = 0-90
    edge_val = p.get("edge")
    if edge_val is not None:
        try:
            e = float(edge_val)
            if e > 0:
                score += min(e * 100, 10)          # bonus hasta 10 pts para edge positivo
        except (TypeError, ValueError):
            pass
    return max(0, min(100, int(round(score))))


# =========================================================
# Profit by market
# =========================================================

def _profit_by_market(settled_picks: list[dict]) -> list[dict]:
    """
    Lista ordenada por profit desc:
    [{ market, picks, wins, losses, push, winrate, net_units }]
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
    for _, b in buckets.items():
        settled = b["wins"] + b["losses"]
        winrate = (b["wins"] / settled * 100.0) if settled > 0 else None
        out.append({
            "market":    b["market"],
            "picks":     b["picks"],
            "wins":      b["wins"],
            "losses":    b["losses"],
            "push":      b["push"],
            "winrate":   round(winrate, 1) if winrate is not None else None,
            "net_units": round(b["net_units"], 3),
        })

    out.sort(key=lambda x: (x["net_units"], x["picks"]), reverse=True)
    return out


# =========================================================
# ROI chart points
# =========================================================

def _roi_spark_points(settled_groups: list[dict]) -> list[dict]:
    """
    Datos para el gráfico: [{ date, label, v (profit acumulado), day (neto del día) }]
    en orden cronológico.
    """
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
            "date":  date_str,
            "label": label,
            "v":     round(cum, 3),
            "day":   round(day_net, 3),
        })
    return pts


# =========================================================
# Local date for a pick
# =========================================================

def _pick_local_date(p: dict, match_by_key: dict[Any, dict] | None) -> date | None:
    """Fecha de calendario del partido en America/Argentina/Buenos_Aires, o None."""
    from app.timefmt import AFTR_DISPLAY_TZ

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


# =========================================================
# Top picks con variedad de mercados
# =========================================================

def top_picks_with_variety(
    picks: list,
    top_n: int = 10,
    max_repeats_per_market: int = 3,
) -> list[tuple[dict, dict]]:
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


# =========================================================
# Agrupación por día (upcoming y recientes)
# =========================================================

_WEEKDAY_LABELS = ("Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo")


def _label_for_date(d: date, today: date) -> str:
    if d == today:
        return "Hoy"
    if d == today - timedelta(days=1):
        return "Ayer"
    return d.isoformat()


def group_upcoming_picks_by_day(picks: list[dict], days: int = 7) -> list[dict]:
    """
    Agrupa picks futuros por fecha local.
    Devuelve [{ date, label, picks }]; label: "Hoy" | "Mañana" | nombre del día.
    Solo incluye fechas en [hoy, hoy + days - 1].
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


def group_picks_recent_by_day_desc(items: list[dict], days: int = 7) -> list[dict]:
    """
    Agrupa picks recientes (resueltos) por fecha local, ordenados de más reciente a más viejo.
    Devuelve [{ date, label, matches }].
    """
    now = datetime.now(timezone.utc)
    today_local = now.astimezone().date()
    cutoff_local = today_local - timedelta(days=max(1, int(days)))

    buckets: dict[date, list[dict]] = {}
    for p in items or []:
        if not isinstance(p, dict):
            continue
        dt = _parse_utcdate_str(p.get("utcDate"))
        local_d = dt.astimezone().date() if dt.tzinfo else dt.date()
        if local_d < cutoff_local:
            continue
        buckets.setdefault(local_d, []).append(p)

    out = []
    for d in sorted(buckets.keys(), reverse=True):
        out.append({
            "date":    d.isoformat(),
            "label":   _label_for_date(d, today_local),
            "matches": buckets[d],
        })
    return out
