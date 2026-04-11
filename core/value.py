"""
AFTR Value Betting Engine — detecta picks con edge positivo real.

Conceptos:
  - implied_prob: probabilidad implícita de la cuota justa (1 / best_fair)
  - model_prob:   probabilidad estimada por el modelo AFTR (best_prob)
  - edge:         model_prob - implied_prob  (positivo = valor a favor)
  - value_rating: 0-100, qué tan atractivo es el pick desde la perspectiva del valor
  - kelly_fraction: tamaño de apuesta óptimo según el criterio de Kelly
  - ev:           expected value por unidad apostada

Funciones públicas:
  - compute_value_metrics(pick)        → métricas de valor para un pick
  - filter_value_picks(picks, min_edge)→ picks con edge >= min_edge, ordenados por value_rating
  - get_todays_value_picks()           → top value picks del día desde el cache de picks diarios
  - value_rating_label(rating)         → "Alto" / "Medio" / "Bajo"
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("aftr.value")

# Umbral mínimo de edge para considerar un pick "con valor"
DEFAULT_MIN_EDGE = 0.04      # 4% de edge mínimo
HIGH_VALUE_EDGE  = 0.10      # 10%+ = valor alto
MID_VALUE_EDGE   = 0.06      # 6-10% = valor medio


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


# ─── core math ───────────────────────────────────────────────────────────────

def compute_value_metrics(pick: dict) -> dict:
    """
    Calcula todas las métricas de valor para un pick.

    pick debe tener:
      - best_prob  (float 0-1): probabilidad del modelo
      - best_fair  (float > 1): cuota justa estimada (equivalente a 1/implied_prob)

    Retorna dict con:
      - model_prob, implied_prob, edge, ev, kelly_fraction, value_rating, value_label
    """
    model_prob  = _safe_float(pick.get("best_prob"))
    best_fair   = _safe_float(pick.get("best_fair"))

    # Implied prob from fair odds
    if best_fair <= 1.0:
        implied_prob = model_prob  # no tenemos cuota real, edge = 0
    else:
        implied_prob = round(1.0 / best_fair, 4)

    edge = round(model_prob - implied_prob, 4)

    # Expected value por unidad: p * (odds - 1) - (1 - p) * 1
    # Con odds = best_fair
    if best_fair > 1.0:
        ev = round(model_prob * (best_fair - 1.0) - (1.0 - model_prob), 4)
    else:
        ev = 0.0

    # Kelly fraction: (p * (b+1) - 1) / b  donde b = best_fair - 1
    b = best_fair - 1.0
    if b > 0:
        kelly_full = (model_prob * (b + 1.0) - 1.0) / b
        # Usamos Kelly fraccionario al 25% para gestión conservadora del riesgo
        kelly_fraction = round(max(0.0, kelly_full * 0.25), 4)
    else:
        kelly_fraction = 0.0

    # Value rating 0-100
    value_rating = _compute_value_rating(edge, model_prob, best_fair)

    return {
        "model_prob":     round(model_prob, 4),
        "implied_prob":   implied_prob,
        "edge":           edge,
        "ev":             ev,
        "kelly_fraction": kelly_fraction,
        "value_rating":   value_rating,
        "value_label":    value_rating_label(value_rating),
        "has_value":      edge >= DEFAULT_MIN_EDGE,
    }


def _compute_value_rating(edge: float, model_prob: float, best_fair: float) -> int:
    """
    Rating de valor 0-100.
    Considera: magnitud del edge, probabilidad del modelo, y cuota (picks con cuota
    alta y edge positivo son más valiosos que cuota baja con el mismo edge).
    """
    if edge <= 0:
        return 0

    # Base: edge normalizado a 0-70 (edge >= 0.15 → 70)
    base = min(edge / 0.15, 1.0) * 70.0

    # Bonus por probabilidad alta del modelo (confianza): hasta 15 pts
    prob_bonus = min(model_prob / 0.70, 1.0) * 15.0

    # Bonus por cuota razonable (evitar trampas de cuotas muy altas): hasta 15 pts
    # Cuota óptima: 1.50-2.50. Muy baja (<1.30) o muy alta (>4.0) reduce el bonus.
    if 1.40 <= best_fair <= 2.80:
        odds_bonus = 15.0
    elif 1.30 <= best_fair < 1.40 or 2.80 < best_fair <= 4.0:
        odds_bonus = 8.0
    else:
        odds_bonus = 2.0

    rating = base + prob_bonus + odds_bonus
    return int(min(100, max(0, round(rating))))


def value_rating_label(rating: int) -> str:
    if rating >= 70:
        return "Alto"
    if rating >= 45:
        return "Medio"
    if rating >= 20:
        return "Bajo"
    return "Sin valor"


def value_rating_color(rating: int) -> str:
    """CSS color class para el badge de valor."""
    if rating >= 70:
        return "value-high"
    if rating >= 45:
        return "value-mid"
    if rating >= 20:
        return "value-low"
    return "value-none"


# ─── pick filtering ───────────────────────────────────────────────────────────

def filter_value_picks(picks: list[dict], min_edge: float = DEFAULT_MIN_EDGE) -> list[dict]:
    """
    Filtra picks con edge >= min_edge e inyecta métricas de valor.
    Retorna lista ordenada por value_rating desc.
    """
    result = []
    for p in picks:
        metrics = compute_value_metrics(p)
        if metrics["edge"] >= min_edge:
            enriched = dict(p)
            enriched["value"] = metrics
            result.append(enriched)

    result.sort(key=lambda x: x["value"]["value_rating"], reverse=True)
    return result


def get_todays_value_picks(
    min_edge: float = DEFAULT_MIN_EDGE,
    top_n: int = 10,
) -> list[dict]:
    """
    Lee los picks diarios de todos los leagues y retorna los top N con mejor valor.
    Solo incluye picks PENDING (sin resultado aún).
    """
    try:
        from config.settings import settings
        from data.cache import read_json_with_fallback
    except Exception as e:
        logger.warning("value: could not import settings/cache: %s", e)
        return []

    all_picks: list[dict] = []
    for code in settings.league_codes():
        try:
            picks_raw = read_json_with_fallback(f"daily_picks_{code}.json")
            if not isinstance(picks_raw, list):
                continue
            for p in picks_raw:
                if not isinstance(p, dict):
                    continue
                result = (p.get("result") or "PENDING").strip().upper()
                if result not in ("PENDING", "", None):
                    continue
                # Necesitamos best_prob y best_fair para calcular value
                if not p.get("best_prob") or not p.get("best_fair"):
                    # Intentar derivar desde candidates
                    candidates = p.get("candidates") or []
                    if candidates:
                        best = max(candidates, key=lambda c: float(c.get("prob") or 0))
                        p = dict(p)
                        p.setdefault("best_prob", best.get("prob"))
                        p.setdefault("best_fair", best.get("fair"))
                        p.setdefault("best_market", best.get("market"))
                p["_league"] = code
                all_picks.append(p)
        except Exception as e:
            logger.warning("value: error reading picks for %s: %s", code, e)

    value_picks = filter_value_picks(all_picks, min_edge=min_edge)
    return value_picks[:top_n]


def get_value_summary() -> dict:
    """
    Resumen del día: cuántos picks tienen valor, distribución por tier.
    """
    all_value = get_todays_value_picks(min_edge=0.01, top_n=200)
    high   = sum(1 for p in all_value if p["value"]["value_rating"] >= 70)
    mid    = sum(1 for p in all_value if 45 <= p["value"]["value_rating"] < 70)
    low    = sum(1 for p in all_value if 20 <= p["value"]["value_rating"] < 45)
    total  = len(all_value)

    best = all_value[0] if all_value else None

    return {
        "total_with_value": total,
        "high_value": high,
        "mid_value": mid,
        "low_value": low,
        "best_pick": {
            "home":         best.get("home", "") if best else "",
            "away":         best.get("away", "") if best else "",
            "market":       best.get("best_market", "") if best else "",
            "edge":         best["value"]["edge"] if best else 0,
            "value_rating": best["value"]["value_rating"] if best else 0,
            "league":       best.get("_league", "") if best else "",
        } if best else None,
    }
