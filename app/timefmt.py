"""
Shared match kickoff / UTC parsing for AFTR UI.

Provider data uses ISO `utcDate` in UTC. Display uses America/Argentina/Buenos_Aires.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo

    AFTR_DISPLAY_TZ = ZoneInfo("America/Argentina/Buenos_Aires")
except Exception:  # pragma: no cover - Windows without tzdata, minimal envs
    # America/Argentina/Buenos_Aires is UTC−3 year-round (no DST since 2009).
    AFTR_DISPLAY_TZ = timezone(timedelta(hours=-3))


def parse_utc_instant(value: object) -> datetime | None:
    """
    Parse a provider instant to timezone-aware UTC.

    - ISO strings: Z or explicit offset handled; naive strings are treated as UTC.
    - datetime: naive → UTC; aware → normalized to UTC (single canonical instant).

    Returns None on failure. Does not apply display TZ (callers convert once for display).
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    try:
        ss = str(value).strip()
        if not ss:
            return None
        if ss.endswith("Z"):
            ss = ss.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ss)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def format_match_kickoff_ar(utc_value: object) -> str:
    """Format kickoff for UI in America/Argentina/Buenos_Aires (dd/mm HH:MM)."""
    dt_utc = parse_utc_instant(utc_value)
    if dt_utc is None:
        return "—"
    local = dt_utc.astimezone(AFTR_DISPLAY_TZ)
    return local.strftime("%d/%m %H:%M")
