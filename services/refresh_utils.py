"""
Utilidades generales del pipeline de refresco.
Parsing de fechas, casteos seguros, normalización de matches.
Sin dependencias internas de services/.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from data.cache import read_json


# -------------------------
# Parsing de fechas
# -------------------------

def _parse_iso_utc(s: str) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    try:
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _parse_utcdate(m: dict) -> datetime:
    s = (m or {}).get("utcDate") or ""
    try:
        if isinstance(s, str) and s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        return datetime.fromisoformat(s)
    except Exception:
        return datetime.now(timezone.utc)


def _parse_utcdate_str(s: Any) -> datetime:
    try:
        if isinstance(s, str) and s:
            if s.endswith("Z"):
                return datetime.fromisoformat(s.replace("Z", "+00:00"))
            return datetime.fromisoformat(s)
    except Exception:
        pass
    return datetime.now(timezone.utc)


# -------------------------
# Casteos seguros
# -------------------------

def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return float(default)
        return float(x)
    except Exception:
        return float(default)


def _safe_int(x: Any, default: int | None = None) -> int | None:
    try:
        if x is None:
            return default
        return int(x)
    except Exception:
        return default


def _best_prob(p: dict) -> float:
    try:
        return float((p or {}).get("best_prob") or 0.0)
    except Exception:
        return 0.0


# -------------------------
# JSON + normalización de match
# -------------------------

def _read_json_list(filename: str) -> list[dict]:
    data = read_json(filename)
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]


def _normalize_match(m: dict) -> dict:
    """
    Deja el match con:
    - home_crest/away_crest
    - status
    - score: {home, away} (si hay goles disponibles, sino None/None)
    - sport: "football" | "basketball" (preserved or default football)
    """
    out = dict(m) if isinstance(m, dict) else {}

    out.setdefault("home_crest", None)
    out.setdefault("away_crest", None)
    st_raw = out.get("status")
    if st_raw is None or (isinstance(st_raw, str) and not st_raw.strip()):
        out["status"] = "TIMED"
    else:
        out["status"] = str(st_raw).strip().upper()
    out.setdefault("sport", "football")

    sc = out.get("score")
    if isinstance(sc, dict) and ("home" in sc or "away" in sc):
        out["score"] = {"home": sc.get("home"), "away": sc.get("away")}
        return out

    hg = out.get("home_goals", None)
    ag = out.get("away_goals", None)
    if hg is not None and ag is not None:
        try:
            out["score"] = {"home": int(hg), "away": int(ag)}
        except Exception:
            out["score"] = {"home": hg, "away": ag}
    else:
        out["score"] = {"home": None, "away": None}

    return out
