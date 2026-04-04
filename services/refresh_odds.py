"""
Enriquecimiento de picks con odds del mercado (implied prob, edge, bookmaker).
Consolida el helper _pick_debug_key que estaba duplicado 4 veces en refresh.py.
"""
from __future__ import annotations

import logging
import os

from config.settings import settings
from core.odds import edge as odds_edge, get_decimal_and_implied_for_market
from data.providers.odds_football import (
    ensure_odds_for_league,
    get_odds_for_match,
    match_odds_to_matches,
)
from services.refresh_utils import _safe_float

logger = logging.getLogger(__name__)


# -------------------------
# Debug helpers (antes duplicados 4 veces)
# -------------------------

def _pick_debug_key(p: dict) -> str:
    """Clave única de un pick para debug de odds. Antes estaba copy-pasted 4 veces."""
    mid = p.get("match_id") or p.get("id")
    bm = (p.get("best_market") or "").strip()
    utc = str(p.get("utcDate") or "").strip()
    home = str(p.get("home") or "").strip()
    away = str(p.get("away") or "").strip()
    return f"{mid}|{bm}|{utc}|{home}|{away}"


def _is_odds_debug_enabled() -> bool:
    return bool(getattr(settings, "debug", False)) or str(
        os.getenv("AFTR_ODDS_DEBUG", "0")
    ).lower() in ("1", "true", "yes")


def _build_debug_watch_keys(picks: list[dict], max_samples: int) -> set[str] | None:
    """Construye el set de keys a watchear para debug. Devuelve None si debug está apagado."""
    if not _is_odds_debug_enabled() or max_samples <= 0:
        return None
    watch: set[str] = set()
    for p in picks:
        if not isinstance(p, dict):
            continue
        k = _pick_debug_key(p)
        if not k:
            continue
        watch.add(k)
        if len(watch) >= max_samples:
            break
    return watch


def _log_odds_debug_saved(
    league_code: str,
    watch_keys: set[str] | None,
    saved_picks: list[dict],
) -> None:
    """Logea el estado final de odds para los picks observados después de escribir al cache."""
    if not watch_keys or not _is_odds_debug_enabled():
        return
    saved_map = {_pick_debug_key(p): p for p in saved_picks if isinstance(p, dict)}
    for k in list(watch_keys):
        sp = saved_map.get(k) or {}
        logger.info(
            "ODDS DEBUG saved | league=%s pick_key=%s home=%s away=%s best_market=%s"
            " | odds_decimal=%s implied_prob=%s edge=%s",
            league_code,
            k,
            sp.get("home"),
            sp.get("away"),
            sp.get("best_market"),
            sp.get("odds_decimal"),
            sp.get("implied_prob"),
            sp.get("edge"),
        )


# -------------------------
# Enriquecimiento principal
# -------------------------

def _enrich_football_picks_with_odds(
    league_code: str,
    matches: list[dict],
    picks: list[dict],
    *,
    debug_watch_keys: set[str] | None = None,
) -> list[dict]:
    """
    Adjunta odds_decimal, implied_prob y edge a cada pick cuando hay odds disponibles.
    Limpia odds stale si el pick ya no matchea eventos del proveedor.
    """
    if not picks:
        return picks

    try:
        odds_events = ensure_odds_for_league(league_code, matches, use_cache_first=False)
        if not odds_events:
            return picks
        odds_lookup = match_odds_to_matches(odds_events, matches)
    except Exception as e:
        logger.debug("Odds enrichment skipped for %s: %s", league_code, e)
        return picks

    for p in picks:
        if not isinstance(p, dict):
            continue

        watch = (debug_watch_keys is not None) and (_pick_debug_key(p) in debug_watch_keys)
        key_pick = _pick_debug_key(p) if watch else None

        old_odds_decimal = p.get("odds_decimal")
        old_implied_prob = p.get("implied_prob")
        old_edge = p.get("edge")

        match_placeholder = {
            "home": p.get("home"),
            "away": p.get("away"),
            "utcDate": p.get("utcDate"),
        }
        odds_row = get_odds_for_match(match_placeholder, odds_lookup)
        best_market = (p.get("best_market") or "").strip()

        if not odds_row or not best_market:
            # Limpiar odds stale de cargas anteriores
            p.pop("odds_decimal", None)
            p.pop("implied_prob", None)
            p.pop("bookmaker_title", None)
            p.pop("edge", None)
            if watch:
                logger.info(
                    "ODDS DEBUG clear | league=%s pick=%s home=%s away=%s utcDate=%s"
                    " best_market=%s | old_odds_decimal=%s old_implied_prob=%s old_edge=%s"
                    " | provider_match=NONE",
                    league_code, key_pick, p.get("home"), p.get("away"),
                    p.get("utcDate"), best_market,
                    old_odds_decimal, old_implied_prob, old_edge,
                )
            continue

        # --- Market re-selection: pick the candidate with the highest edge ---
        candidates = p.get("candidates") or []
        best_edge_market = None
        best_edge_val = None
        best_edge_decimal = None
        best_edge_implied = None
        best_edge_prob = None

        for cand in candidates:
            cand_market = (cand.get("market") or "").strip()
            cand_prob = _safe_float(cand.get("prob"))
            if not cand_market or cand_prob is None:
                continue
            c_dec, c_impl = get_decimal_and_implied_for_market(odds_row, cand_market)
            if c_dec is None or c_impl is None:
                continue
            c_edge = odds_edge(cand_prob, c_impl)
            if c_edge is None:
                continue
            if best_edge_val is None or c_edge > best_edge_val:
                best_edge_val = c_edge
                best_edge_market = cand_market
                best_edge_decimal = c_dec
                best_edge_implied = c_impl
                best_edge_prob = cand_prob

        # Use best-edge market if it has a meaningful advantage (>= 1% edge)
        if best_edge_market and best_edge_val is not None and best_edge_val >= 0.01:
            use_market = best_edge_market
            use_decimal = best_edge_decimal
            use_implied = best_edge_implied
            use_prob = best_edge_prob
        else:
            # Fall back to the already-selected best_market
            use_market = best_market
            use_decimal, use_implied = get_decimal_and_implied_for_market(odds_row, best_market)
            use_prob = _safe_float(p.get("best_prob"))

        decimal_odds, implied_prob = use_decimal, use_implied
        if decimal_odds is not None and implied_prob is not None:
            new_odds_decimal = round(float(decimal_odds), 2)
            # Update pick to use the selected market
            if use_market != best_market:
                logger.debug(
                    "Market re-selection %s vs %s: switching %s → %s (edge %.3f)",
                    p.get("home"), p.get("away"), best_market, use_market, best_edge_val or 0,
                )
                p["best_market"] = use_market
                if use_prob is not None:
                    p["best_prob"] = round(float(use_prob), 4)
                    p["best_fair"] = round(1.0 / float(use_prob), 2) if use_prob > 0 else None
            if watch:
                logger.info(
                    "ODDS DEBUG update | league=%s pick=%s home=%s away=%s utcDate=%s"
                    " best_market=%s | old_odds_decimal=%s old_implied_prob=%s old_edge=%s"
                    " | provider_decimal=%s provider_implied_prob=%s bookmaker=%s",
                    league_code, key_pick, p.get("home"), p.get("away"),
                    p.get("utcDate"), p.get("best_market"),
                    old_odds_decimal, old_implied_prob, old_edge,
                    decimal_odds, implied_prob,
                    (odds_row.get("bookmaker_title") or "").strip(),
                )
            p["odds_decimal"] = new_odds_decimal
            p["implied_prob"] = implied_prob
            aftr_prob = _safe_float(p.get("best_prob"))
            if aftr_prob is not None:
                e = odds_edge(aftr_prob, implied_prob)
                if e is not None:
                    p["edge"] = e
            bookmaker_title = (odds_row.get("bookmaker_title") or "").strip()
            if bookmaker_title:
                p["bookmaker_title"] = bookmaker_title
        else:
            # Match existe pero no se pudo mapear el mercado => limpiar stale
            p.pop("odds_decimal", None)
            p.pop("implied_prob", None)
            p.pop("bookmaker_title", None)
            p.pop("edge", None)
            if watch:
                logger.info(
                    "ODDS DEBUG clear-mapping | league=%s pick=%s home=%s away=%s utcDate=%s"
                    " best_market=%s | old_odds_decimal=%s old_implied_prob=%s old_edge=%s"
                    " | provider_decimal=%s provider_implied_prob=%s",
                    league_code, key_pick, p.get("home"), p.get("away"),
                    p.get("utcDate"), best_market,
                    old_odds_decimal, old_implied_prob, old_edge,
                    decimal_odds, implied_prob,
                )

    return picks
