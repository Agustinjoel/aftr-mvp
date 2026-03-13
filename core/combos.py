from __future__ import annotations

from typing import Any, Tuple
from datetime import datetime, timezone, timedelta

TIERS = {
    "SAFE":   {"min_prob": 0.82, "legs": 3},
    "MEDIUM": {"min_prob": 0.76, "legs": 4},
    "SPICY":  {"min_prob": 0.70, "legs": 5},
}

# =========================================================
# Time helpers
# =========================================================
def _parse_dt(s: str) -> datetime:
    """
    Parse ISO string a datetime aware.
    Soporta Z.
    """
    try:
        s = str(s or "")
        if not s:
            return datetime.now(timezone.utc)
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        dt = datetime.fromisoformat(s)
        # si vino naive, lo asumimos UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return datetime.now(timezone.utc)


def _in_window(utcDate: str, start: datetime, end: datetime) -> bool:
    dt = _parse_dt(str(utcDate or ""))
    return start <= dt <= end


def _window_bounds(mode: str) -> tuple[datetime, datetime, str]:
    """
    mode:
      - "today": [00:00 .. 23:59:59] UTC
      - "3d": [now .. now+3d] UTC
    """
    now = datetime.now(timezone.utc)
    m = (mode or "today").lower().strip()

    if m == "3d":
        start = now
        end = now + timedelta(days=3)
        label = "72HS"
        return start, end, label

    # default: today
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    label = "HOY"
    return start, end, label


# =========================================================
# Math helpers
# =========================================================
def _safe_float(x, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return float(default)


def _clamp01(p: float) -> float:
    return max(0.0, min(1.0, p))


def _pick_best_candidate(pick: dict) -> dict | None:
    """
    Toma el primer candidato válido (asumimos que candidates ya viene ordenado en tu pipeline).
    """
    cands = pick.get("candidates") or []
    if not isinstance(cands, list) or not cands:
        return None
    for c in cands:
        if isinstance(c, dict) and (c.get("market") or "").strip():
            return c
    return None


def _combo_prob(items: list[dict]) -> float:
    prob = 1.0
    for it in items:
        prob *= _clamp01(_safe_float(it.get("prob"), 0.0))
    return prob


def _combo_fair(items: list[dict]) -> float | None:
    # si viene fair por selección, lo multiplicamos (aprox)
    fair = 1.0
    ok = False
    for it in items:
        f = it.get("fair")
        if f is None:
            continue
        ok = True
        fair *= _safe_float(f, 1.0)
    return fair if ok else None


def _risk_bucket(p: float) -> str:
    # p en 0..1
    if p >= 0.72:
        return "SAFE"
    if p >= 0.60:
        return "MEDIUM"
    return "SPICY"


def _match_key(it: dict) -> Tuple[str, str]:
    """
    Key estable para no repetir el mismo partido.
    - Si hay match_id: usa league + match_id
    - Si no hay: usa league + (utcDate|home|away)
    """
    league = str(it.get("league") or "")
    mid = it.get("match_id")
    if mid is not None and str(mid).strip():
        return (league, f"id:{str(mid).strip()}")
    utc = str(it.get("utcDate") or "")
    home = str(it.get("home") or "")
    away = str(it.get("away") or "")
    return (league, f"f:{utc}|{home}|{away}")


# =========================================================
# Public builder
# =========================================================
def build_global_combos(
    picks_by_league: dict[str, list[dict]],
    mode: str = "today",
) -> dict[str, Any]:
    """
    Construye combos globales filtrando por ventana temporal.
    mode:
      - "today": solo partidos del día (UTC)
      - "3d": próximos 3 días desde ahora (UTC)
    """
    start, end, label = _window_bounds(mode)

    pool: list[dict] = []

    for league, picks in (picks_by_league or {}).items():
        if not isinstance(picks, list):
            continue

        for p in picks:
            if not isinstance(p, dict):
                continue

            # filtrar por ventana
            if not _in_window(p.get("utcDate"), start, end):
                continue

            best = _pick_best_candidate(p)
            if not best:
                continue

            prob = _safe_float(best.get("prob"), 0.0)
            # si viene 0..100 lo normalizamos
            if prob > 1.0:
                prob = prob / 100.0

            # match_id compat
            mid = p.get("match_id")
            if mid is None:
                mid = p.get("id")

            item = {
                "league": league,
                "match_id": mid,
                "utcDate": p.get("utcDate"),
                "home": p.get("home"),
                "away": p.get("away"),
                "market": best.get("market"),
                "prob": _clamp01(prob),
                "fair": best.get("fair"),
                "home_crest": p.get("home_crest"),
                "away_crest": p.get("away_crest"),
            }
            pool.append(item)

    # orden global: primero los más probables
    pool.sort(key=lambda x: x["prob"], reverse=True)

    # dedupe por partido
    used = set()
    uniq: list[dict] = []
    for it in pool:
        k = _match_key(it)
        if k in used:
            continue
        used.add(k)
        uniq.append(it)

    def pick_n(min_prob: float, n: int) -> list[dict]:
        out = []
        for it in uniq:
            if it["prob"] >= min_prob:
                out.append(it)
                if len(out) >= n:
                    break
        return out

    # tamaños auto (pero con “personalidad” según ventana)
    # HOY: combos más cortos
    # 72HS: combos un poquito más largos
    is_3d = (mode or "").lower().strip() == "3d"

    safe_n = 1
    if len(uniq) >= 2:
        safe_n = 2
    if len(uniq) >= 4:
        safe_n = 3
    if len(uniq) >= 7:
        safe_n = 4

    medium_n = 2
    if len(uniq) >= 5:
        medium_n = 3
    if len(uniq) >= 9:
        medium_n = 4

    spicy_n = 3
    if len(uniq) >= 8:
        spicy_n = 4
    if len(uniq) >= 12:
        spicy_n = 5

    # ajuste por ventana
    if not is_3d:
        # HOY: un toque más conservador en cantidad
        safe_n = min(safe_n, 3)
        medium_n = min(medium_n, 3)
        spicy_n = min(spicy_n, 4)
    else:
        # 72HS: permitimos un leg más si hay pool
        if len(uniq) >= 10:
            safe_n = min(5, safe_n + 1)
        if len(uniq) >= 12:
            medium_n = min(5, medium_n + 1)
        if len(uniq) >= 15:
            spicy_n = min(6, spicy_n + 1)

    safe_items = pick_n(0.72, safe_n)
    medium_items = pick_n(0.60, medium_n)
    spicy_items = pick_n(0.48, spicy_n)

    # fallbacks si el filtro dejó pool corto
    if not safe_items:
        safe_items = uniq[: max(1, min(2, len(uniq)))]
    if not medium_items:
        medium_items = uniq[: max(2, min(3, len(uniq)))]
    if not spicy_items:
        spicy_items = uniq[: max(3, min(4, len(uniq)))]

    def pack(name: str, items: list[dict]) -> dict:
        cp = _combo_prob(items)
        fair_val = _combo_fair(items)
        return {
            "name": name,
            "tier": _risk_bucket(cp),
            "legs": items,
            "combo_prob": round(cp, 4),
            "combo_prob_pct": round(cp * 100, 1),
            "fair": (round(fair_val, 2) if fair_val is not None else None),
        }

    # Nombres según ventana
    suffix = f"• {label}"

    return {
        "window": {
            "mode": mode,
            "label": label,
            "start_utc": start.isoformat(),
            "end_utc": end.isoformat(),
        },
        "free": pack(f"Free • SAFE {suffix}", safe_items),
        "premium": [
            pack(f"Premium • SAFE {suffix}", safe_items),
            pack(f"Premium • MEDIUM {suffix}", medium_items),
            pack(f"Premium • SPICY {suffix}", spicy_items),
        ],
        "meta": {
            "total_candidates": len(pool),
            "total_unique_matches": len(uniq),
        },
    }