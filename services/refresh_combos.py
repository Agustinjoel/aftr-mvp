"""
Construcción, deduplicación y guardado de combinadas (parlays) globales.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from config.settings import settings
from core.combos import build_global_combos
from data.cache import read_json, write_json

logger = logging.getLogger(__name__)


# -------------------------
# Helpers de tier + signature
# -------------------------

def _combo_sig(c: dict) -> str:
    """Firma única de una combinada basada en sus legs (match_id + market)."""
    if not isinstance(c, dict):
        return ""
    legs = c.get("legs") or []
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


def _tier_from_name_or_prob(combo: dict) -> str:
    """Determina SAFE/MEDIUM/SPICY desde el nombre o la probabilidad del combo."""
    name = (combo.get("name") or "").upper()
    if "SAFE" in name:
        return "SAFE"
    if "MEDIUM" in name:
        return "MEDIUM"
    if "SPICY" in name:
        return "SPICY"

    pct = combo.get("combo_prob_pct")
    try:
        pct = float(pct)
    except Exception:
        pct = None

    if pct is None:
        return "SPICY"
    if pct >= 55:
        return "SAFE"
    if pct >= 45:
        return "MEDIUM"
    return "SPICY"


def _fix_tiers(win: dict) -> None:
    """Alinea el campo `tier` de cada combo con su nombre (SAFE/MEDIUM/SPICY)."""
    if not isinstance(win, dict):
        return
    free = win.get("free")
    if isinstance(free, dict):
        free["tier"] = _tier_from_name_or_prob(free)
    prem = win.get("premium")
    if isinstance(prem, list):
        for c in prem:
            if isinstance(c, dict):
                c["tier"] = _tier_from_name_or_prob(c)


def _dedupe_window(win: dict) -> None:
    """Elimina combos premium duplicados (vs free y entre sí)."""
    if not isinstance(win, dict):
        return
    free = win.get("free") if isinstance(win.get("free"), dict) else {}
    free_sig = _combo_sig(free) if free else ""
    prem = win.get("premium")
    if not isinstance(prem, list):
        return
    seen: set[str] = set()
    out = []
    for c in prem:
        if not isinstance(c, dict):
            continue
        sig = _combo_sig(c)
        if not sig or sig == free_sig or sig in seen:
            continue
        seen.add(sig)
        out.append(c)
    win["premium"] = out


def _prune_next3d_overlap(payload: dict) -> None:
    """
    Remueve legs del window next3d que sean de HOY (UTC),
    para que next3d muestre solo picks de mañana en adelante.
    """
    if not isinstance(payload, dict):
        return
    now_utc = datetime.now(timezone.utc)
    today_utc = now_utc.date()
    next3d = payload.get("next3d")
    if not isinstance(next3d, dict):
        return

    def prune_combo(combo: dict) -> None:
        if not isinstance(combo, dict):
            return
        legs = combo.get("legs") or []
        if not isinstance(legs, list):
            return
        kept = []
        for it in legs:
            if not isinstance(it, dict):
                continue
            dt = it.get("utcDate") or ""
            try:
                s = dt.replace("Z", "+00:00") if isinstance(dt, str) else ""
                d = datetime.fromisoformat(s).date()
            except Exception:
                kept.append(it)
                continue
            if d != today_utc:
                kept.append(it)
        combo["legs"] = kept

    if isinstance(next3d.get("free"), dict):
        prune_combo(next3d["free"])
    if isinstance(next3d.get("premium"), list):
        for c in next3d["premium"]:
            if isinstance(c, dict):
                prune_combo(c)


# -------------------------
# Builder + guardado
# -------------------------

def _build_and_save_combos() -> None:
    """
    Genera combinadas globales y las guarda en daily_combos.json:
    - today: solo partidos del día (UTC)
    - next3d: próximos 3 días (72hs, UTC, sin overlap de hoy)
    """
    picks_by_league: dict[str, list[dict]] = {}
    for c in settings.league_codes():
        p = read_json(f"daily_picks_{c}.json") or []
        picks_by_league[c] = [x for x in p if isinstance(x, dict)]

    today = build_global_combos(picks_by_league, mode="today")
    next3d = build_global_combos(picks_by_league, mode="3d")

    payload = {
        "today": today,
        "next3d": next3d,
        "meta": {
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "leagues": list(settings.league_codes()),
        },
    }

    _prune_next3d_overlap(payload)
    _dedupe_window(payload.get("today") or {})
    _dedupe_window(payload.get("next3d") or {})
    _fix_tiers(payload.get("today") or {})
    _fix_tiers(payload.get("next3d") or {})

    write_json("daily_combos.json", payload)

    logger.info(
        "Combos OK: today=%s uniq=%s | next3d=%s uniq=%s",
        ((today.get("meta") or {}).get("total_candidates") if isinstance(today, dict) else None),
        ((today.get("meta") or {}).get("total_unique_matches") if isinstance(today, dict) else None),
        ((next3d.get("meta") or {}).get("total_candidates") if isinstance(next3d, dict) else None),
        ((next3d.get("meta") or {}).get("total_unique_matches") if isinstance(next3d, dict) else None),
    )
