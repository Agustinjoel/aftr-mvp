from __future__ import annotations

from datetime import datetime, timedelta, timezone

from data.providers.football_data import _get
from data.cache import read_json, write_json


def _cache_key(team_id: int, days_back: int) -> str:
    return f"team_form_{team_id}_{days_back}.json"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _is_fresh(fetched_at_iso: str | None, ttl_seconds: int) -> bool:
    if not fetched_at_iso:
        return False
    try:
        # soporta "Z" o "+00:00"
        if fetched_at_iso.endswith("Z"):
            fetched_at = datetime.fromisoformat(fetched_at_iso.replace("Z", "+00:00"))
        else:
            fetched_at = datetime.fromisoformat(fetched_at_iso)
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        return (_now_utc() - fetched_at).total_seconds() <= ttl_seconds
    except Exception:
        return False


def get_team_recent_matches(
    team_id: int,
    days_back: int = 30,
    limit: int = 10,
    ttl_seconds: int = 12 * 60 * 60,  # 12h
) -> list[dict]:
    """
    Devuelve últimos partidos (FINISHED) del equipo.
    Cachea por team_id + days_back, con TTL.
    Si la API falla, usa cache viejo si existe.
    """
    key = _cache_key(team_id, days_back)
    cached = read_json(key)

    # ✅ Compatibilidad hacia atrás:
    # - formato viejo: lista[dict]
    # - formato nuevo: {"meta": {...}, "matches": [...]}
    cached_matches: list[dict] | None = None
    cached_fetched_at: str | None = None

    if isinstance(cached, dict):
        cached_matches = cached.get("matches") if isinstance(cached.get("matches"), list) else None
        meta = cached.get("meta") if isinstance(cached.get("meta"), dict) else {}
        cached_fetched_at = meta.get("fetched_at")
        if cached_matches is not None and _is_fresh(cached_fetched_at, ttl_seconds):
            return cached_matches[:limit]

    elif isinstance(cached, list) and cached:
        # viejo: si hay lista y no está vacía, úsala (sin TTL)
        # pero igual vamos a refrescar si podemos (para que no se quede congelado para siempre)
        cached_matches = cached

    # ---- Fetch API ----
    end = _now_utc()
    start = end - timedelta(days=days_back)

    date_from = start.strftime("%Y-%m-%d")
    date_to = end.strftime("%Y-%m-%d")

    try:
        data = _get(
            f"/teams/{team_id}/matches",
            params={
                "status": "FINISHED",
                "dateFrom": date_from,
                "dateTo": date_to,
                "limit": limit,
            },
        )
    except Exception:
        # ✅ Si la API falla (429, etc.), devolvemos cache viejo si había
        if cached_matches:
            return cached_matches[:limit]
        return []

    matches = data.get("matches", []) or []
    out: list[dict] = []

    for m in matches:
        ft = ((m.get("score") or {}).get("fullTime")) or {}
        hg = ft.get("home")
        ag = ft.get("away")
        if hg is None or ag is None:
            continue

        out.append(
            {
                "utcDate": m.get("utcDate", ""),
                "home_id": (m.get("homeTeam") or {}).get("id"),
                "away_id": (m.get("awayTeam") or {}).get("id"),
                "home_goals": int(hg),
                "away_goals": int(ag),
            }
        )

    payload = {
        "meta": {
            "team_id": team_id,
            "days_back": days_back,
            "limit": limit,
            "fetched_at": end.isoformat(),
            "ttl_seconds": ttl_seconds,
        },
        "matches": out,
    }
    write_json(key, payload)

    return out[:limit]