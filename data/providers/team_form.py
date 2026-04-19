from __future__ import annotations

from datetime import datetime, timedelta, timezone

from data.cache import read_json, write_json


def _cache_key(team_id: int, days_back: int) -> str:
    return f"team_form_{team_id}_{days_back}.json"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _is_fresh(fetched_at_iso: str | None, ttl_seconds: int) -> bool:
    if not fetched_at_iso:
        return False
    try:
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
    Devuelve últimos partidos (FINISHED) del equipo via API-Football.
    Cachea por team_id + days_back, con TTL.
    Si la API falla, usa cache viejo si existe.
    """
    key = _cache_key(team_id, days_back)
    cached = read_json(key)

    cached_matches: list[dict] | None = None
    cached_fetched_at: str | None = None

    if isinstance(cached, dict):
        cached_matches = cached.get("matches") if isinstance(cached.get("matches"), list) else None
        meta = cached.get("meta") if isinstance(cached.get("meta"), dict) else {}
        cached_fetched_at = meta.get("fetched_at")
        if cached_matches is not None and _is_fresh(cached_fetched_at, ttl_seconds):
            return cached_matches[:limit]
    elif isinstance(cached, list) and cached:
        cached_matches = cached

    # ---- Fetch via API-Football ----
    end = _now_utc()
    start = end - timedelta(days=days_back)

    date_from = start.strftime("%Y-%m-%d")
    date_to = end.strftime("%Y-%m-%d")

    try:
        from data.providers.api_football import _get
        # API-Football v3 requires 'season'. Use y-1 for European style (active in April);
        # fallback to y if empty (American leagues).
        now_dt = _now_utc()
        _y = now_dt.year
        _season_primary = _y - 1 if now_dt.month < 7 else _y
        _season_fallback = _y if _season_primary == _y - 1 else _y - 1

        items = _get("/fixtures", {
            "team": team_id,
            "season": _season_primary,
            "from": date_from,
            "to": date_to,
            "status": "FT-AET-PEN",
        })
        if not items:
            items = _get("/fixtures", {
                "team": team_id,
                "season": _season_fallback,
                "from": date_from,
                "to": date_to,
                "status": "FT-AET-PEN",
            })
    except Exception:
        if cached_matches:
            return cached_matches[:limit]
        return []

    out: list[dict] = []
    for fx in (items or []):
        if not isinstance(fx, dict):
            continue
        fix = fx.get("fixture") or {}
        teams = fx.get("teams") or {}
        goals = fx.get("goals") or {}
        score = fx.get("score") or {}

        hg = goals.get("home")
        ag = goals.get("away")
        if hg is None or ag is None:
            ft = score.get("fulltime") or score.get("fullTime") or {}
            hg = ft.get("home")
            ag = ft.get("away")
        if hg is None or ag is None:
            continue

        home_team = (teams.get("home") or {})
        away_team = (teams.get("away") or {})

        out.append({
            "utcDate": fix.get("date", ""),
            "home_id": home_team.get("id"),
            "away_id": away_team.get("id"),
            "home_goals": int(hg),
            "away_goals": int(ag),
        })

    if not out and cached_matches:
        return cached_matches[:limit]

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
