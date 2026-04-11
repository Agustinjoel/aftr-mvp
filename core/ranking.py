"""
AFTR Ranking Engine — estadísticas históricas de performance del sistema.

Provee:
  - compute_global_stats()     → KPIs globales: win%, ROI, net units, racha
  - compute_league_breakdown() → stats por liga
  - compute_market_breakdown() → stats por tipo de mercado (1X2, Over, BTTS)
  - compute_cumulative_curve() → curva acumulada de unidades (para gráfico)
  - compute_recent_form()      → últimos N picks resueltos
  - compute_streaks()          → racha actual y máxima
  - get_full_ranking_report()  → todo en un dict, cacheado
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("aftr.ranking")

# Cache en memoria: (data, timestamp)
_ranking_cache: tuple[dict, float] | None = None
_CACHE_TTL = 300  # 5 minutos


# ─── helpers ─────────────────────────────────────────────────────────────────

def _unit_profit(result: str, best_fair: float | None) -> float:
    r = (result or "").strip().upper()
    if r == "WIN":
        fair = float(best_fair or 0)
        return max(fair - 1.0, 1.0) if fair > 1 else 1.0
    if r == "LOSS":
        return -1.0
    return 0.0  # PUSH


def _market_category(market: str) -> str:
    m = (market or "").strip().upper()
    if m in ("1", "X", "2", "1X", "X2", "12", "HOME WIN", "AWAY WIN", "DRAW",
             "LOCAL", "VISITANTE", "EMPATE"):
        return "1X2"
    if "OVER" in m or "UNDER" in m:
        return "Goles"
    if "BTTS" in m or "GG" in m or "NG" in m or "AMBOS" in m:
        return "BTTS"
    if "DNB" in m:
        return "DNB"
    return "Otro"


def _league_label(code: str) -> str:
    try:
        from config.settings import settings
        return settings.leagues.get(code, code)
    except Exception:
        return code


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


# ─── DB query ────────────────────────────────────────────────────────────────

def _load_resolved_picks() -> list[dict]:
    """Carga todos los picks resueltos (WIN/LOSS/PUSH) de la DB."""
    try:
        from app.db import get_conn, put_conn
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT
                    p.league,
                    p.match_id,
                    p.best_market,
                    p.best_prob,
                    p.best_fair,
                    p.confidence,
                    p.edge,
                    p.result,
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
            logger.exception("ranking: DB query failed")
            return []
        finally:
            put_conn(conn)
    except Exception as e:
        logger.warning("ranking: could not connect to DB: %s", e)
        return []


# ─── stats engines ───────────────────────────────────────────────────────────

def compute_global_stats(picks: list[dict]) -> dict:
    """KPIs globales."""
    wins = losses = push = 0
    net_units = 0.0
    for p in picks:
        r = (p.get("result") or "").strip().upper()
        delta = _unit_profit(r, p.get("best_fair"))
        net_units += delta
        if r == "WIN":
            wins += 1
        elif r == "LOSS":
            losses += 1
        elif r == "PUSH":
            push += 1

    settled = wins + losses + push
    winrate = round(wins / settled * 100.0, 1) if settled > 0 else 0.0
    roi = round(net_units / settled * 100.0, 1) if settled > 0 else 0.0

    return {
        "total": settled,
        "wins": wins,
        "losses": losses,
        "push": push,
        "winrate": winrate,
        "roi": roi,
        "net_units": round(net_units, 2),
        "avg_odds": _avg_odds(picks),
    }


def _avg_odds(picks: list[dict]) -> float:
    odds_list = [float(p["best_fair"]) for p in picks if p.get("best_fair") and float(p["best_fair"]) > 1]
    if not odds_list:
        return 0.0
    return round(sum(odds_list) / len(odds_list), 2)


def compute_league_breakdown(picks: list[dict]) -> list[dict]:
    """Stats por liga, ordenadas por net_units desc."""
    from collections import defaultdict
    leagues: dict[str, list[dict]] = defaultdict(list)
    for p in picks:
        code = (p.get("league") or "UNK").strip()
        leagues[code].append(p)

    result = []
    for code, ps in leagues.items():
        stats = compute_global_stats(ps)
        stats["league_code"] = code
        stats["league_name"] = _league_label(code)
        result.append(stats)

    result.sort(key=lambda x: x["net_units"], reverse=True)
    return result


def compute_market_breakdown(picks: list[dict]) -> list[dict]:
    """Stats por categoría de mercado."""
    from collections import defaultdict
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for p in picks:
        cat = _market_category(p.get("best_market") or "")
        by_cat[cat].append(p)

    result = []
    for cat, ps in by_cat.items():
        stats = compute_global_stats(ps)
        stats["market"] = cat
        result.append(stats)

    result.sort(key=lambda x: x["net_units"], reverse=True)
    return result


def compute_cumulative_curve(picks: list[dict]) -> list[dict]:
    """
    Curva acumulada de unidades en el tiempo.
    Retorna lista de {date, cumulative_units, picks} ordenada por fecha.
    """
    from collections import defaultdict
    by_date: dict[str, list[float]] = defaultdict(list)

    for p in picks:
        r = (p.get("result") or "").strip().upper()
        delta = _unit_profit(r, p.get("best_fair"))
        dt = _parse_date(p.get("utcDate"))
        date_str = dt.strftime("%Y-%m-%d") if dt else "unknown"
        by_date[date_str].append(delta)

    sorted_dates = sorted(d for d in by_date if d != "unknown")
    cumulative = 0.0
    curve = []
    for d in sorted_dates:
        for delta in by_date[d]:
            cumulative += delta
        curve.append({
            "date": d,
            "cumulative_units": round(cumulative, 2),
            "picks": len(by_date[d]),
        })
    return curve


def compute_recent_form(picks: list[dict], n: int = 10) -> list[dict]:
    """Últimos N picks resueltos (más recientes primero)."""
    sorted_picks = sorted(
        picks,
        key=lambda p: _parse_date(p.get("utcDate")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    result = []
    for p in sorted_picks[:n]:
        r = (p.get("result") or "").strip().upper()
        result.append({
            "home": p.get("home", ""),
            "away": p.get("away", ""),
            "market": p.get("best_market", ""),
            "result": r,
            "odds": p.get("best_fair"),
            "profit": round(_unit_profit(r, p.get("best_fair")), 2),
            "league": _league_label(p.get("league") or ""),
            "date": (p.get("utcDate") or "")[:10],
        })
    return result


def compute_streaks(picks: list[dict]) -> dict:
    """
    Calcula:
      - current_streak: racha actual (positivo=victorias, negativo=derrotas)
      - max_win_streak: racha máxima de victorias consecutivas
      - max_loss_streak: racha máxima de derrotas consecutivas
    """
    sorted_picks = sorted(
        picks,
        key=lambda p: _parse_date(p.get("utcDate")) or datetime.min.replace(tzinfo=timezone.utc),
    )

    current_streak = 0
    current_type: str | None = None
    max_win = 0
    max_loss = 0
    temp_win = 0
    temp_loss = 0

    for p in sorted_picks:
        r = (p.get("result") or "").strip().upper()
        if r == "PUSH":
            continue
        if r == "WIN":
            temp_win += 1
            temp_loss = 0
            if current_type == "WIN":
                current_streak += 1
            else:
                current_streak = 1
                current_type = "WIN"
            max_win = max(max_win, temp_win)
        elif r == "LOSS":
            temp_loss += 1
            temp_win = 0
            if current_type == "LOSS":
                current_streak -= 1
            else:
                current_streak = -1
                current_type = "LOSS"
            max_loss = max(max_loss, temp_loss)

    return {
        "current_streak": current_streak,
        "current_type": current_type or "NONE",
        "max_win_streak": max_win,
        "max_loss_streak": max_loss,
    }


def compute_monthly_breakdown(picks: list[dict]) -> list[dict]:
    """Stats agrupadas por mes (últimos 6 meses con datos)."""
    from collections import defaultdict
    by_month: dict[str, list[dict]] = defaultdict(list)

    for p in picks:
        dt = _parse_date(p.get("utcDate"))
        if dt:
            key = dt.strftime("%Y-%m")
            by_month[key].append(p)

    result = []
    for month in sorted(by_month.keys(), reverse=True)[:6]:
        ps = by_month[month]
        stats = compute_global_stats(ps)
        stats["month"] = month
        try:
            dt = datetime.strptime(month, "%Y-%m")
            month_labels = {
                1: "Ene", 2: "Feb", 3: "Mar", 4: "Abr",
                5: "May", 6: "Jun", 7: "Jul", 8: "Ago",
                9: "Sep", 10: "Oct", 11: "Nov", 12: "Dic",
            }
            stats["month_label"] = f"{month_labels[dt.month]} {dt.year}"
        except Exception:
            stats["month_label"] = month
        result.append(stats)

    result.sort(key=lambda x: x["month"])
    return result


# ─── full report (cached) ─────────────────────────────────────────────────────

def get_full_ranking_report(force: bool = False) -> dict:
    """
    Retorna el reporte completo de ranking. Cacheado por _CACHE_TTL segundos.
    force=True ignora el cache.
    """
    global _ranking_cache
    now = time.time()

    if not force and _ranking_cache is not None:
        data, ts = _ranking_cache
        if now - ts < _CACHE_TTL:
            return data

    picks = _load_resolved_picks()

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "global": compute_global_stats(picks),
        "streaks": compute_streaks(picks),
        "recent_form": compute_recent_form(picks, n=10),
        "by_league": compute_league_breakdown(picks),
        "by_market": compute_market_breakdown(picks),
        "by_month": compute_monthly_breakdown(picks),
        "curve": compute_cumulative_curve(picks),
    }

    _ranking_cache = (report, now)
    return report


def invalidate_cache() -> None:
    """Invalida el cache de ranking (llamar después de settlement de picks)."""
    global _ranking_cache
    _ranking_cache = None
