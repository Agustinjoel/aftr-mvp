from datetime import datetime, timezone, timedelta
from typing import Any

from fastapi import APIRouter, Query

from config.settings import settings
from data.cache import read_json

router = APIRouter()

# Días de la semana para label (lunes=0, domingo=6)
_WEEKDAY_LABELS = ("Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo")


def _utc_to_local_date(utc_date_str: str) -> str | None:
    """Convierte utcDate (ISO) a fecha local YYYY-MM-DD. None si no se puede parsear."""
    if not utc_date_str or not isinstance(utc_date_str, str):
        return None
    s = utc_date_str.strip()
    try:
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local_dt = dt.astimezone()
        return local_dt.date().isoformat()
    except Exception:
        return None


def group_matches_by_day(
    matches: list[dict[str, Any]],
    days: int = 7,
) -> list[dict[str, Any]]:
    """
    Agrupa partidos por fecha local. Devuelve lista de { date, label, matches }.
    label: "Hoy" | "Mañana" | nombre del día (ej. "Lunes").
    Solo incluye fechas en [hoy, hoy + days - 1].
    """
    today = datetime.now().astimezone().date()
    end = today + timedelta(days=max(0, days - 1))
    by_date: dict[str, list[dict]] = {}

    for m in matches:
        local_date_str = _utc_to_local_date(m.get("utcDate") or "")
        if not local_date_str:
            continue
        try:
            d = datetime.strptime(local_date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if today <= d <= end:
            by_date.setdefault(local_date_str, []).append(m)

    out = []
    for date_str in sorted(by_date.keys()):
        day_matches = by_date[date_str]
        day_matches.sort(key=lambda x: (x.get("utcDate") or ""))
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        if d == today:
            label = "Hoy"
        elif d == today + timedelta(days=1):
            label = "Mañana"
        else:
            label = _WEEKDAY_LABELS[d.weekday()]
        out.append({
            "date": date_str,
            "label": label,
            "matches": day_matches,
        })
    return out


@router.get("/matches")
def get_matches(league: str = Query(settings.default_league)):
    league = league if settings.is_valid_league(league) else settings.default_league
    return read_json(f"daily_matches_{league}.json")


@router.get("/matches/by-day")
def get_matches_by_day(
    league: str = Query(settings.default_league),
    days: int = Query(7, ge=1, le=31),
):
    league = league if settings.is_valid_league(league) else settings.default_league
    matches = read_json(f"daily_matches_{league}.json")
    if not isinstance(matches, list):
        matches = []
    days_list = group_matches_by_day(matches, days=days)
    return {"days": days_list}