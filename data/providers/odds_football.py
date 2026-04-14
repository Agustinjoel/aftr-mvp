"""
Football odds provider. Fetches odds via API-Football, normalizes to AFTR market names,
matches to existing matches by home/away/date, and supports cache read/write.
NBA/other sports: use separate provider later; this module is football-only.
"""
from __future__ import annotations

import logging
import re as _re
from datetime import datetime, timezone
from typing import Any

from data.cache import read_json, write_json

logger = logging.getLogger(__name__)

ODDS_CACHE_FILE = "daily_odds_{league_code}.json"


# Club-type tokens to remove from the start or end of the name
_CLUB_TOKENS = {"fc", "afc", "sc", "cf", "ac", "bfc", "sfc", "utd"}
_PUNC_RE = _re.compile(r'[^a-z0-9 ]')


def _normalize_team(s: str) -> str:
    """
    Normalize team name for fuzzy matching.
    Lowercases, removes punctuation (&, . etc.), strips common club suffixes/prefixes (FC, AFC…).
    'Arsenal FC' == 'Arsenal', 'AFC Bournemouth' == 'Bournemouth'.
    """
    t = (s or "").strip().lower()
    # & → and, remove remaining punctuation
    t = t.replace("&", "and")
    t = _PUNC_RE.sub('', t)
    # Split and drop club-type tokens at the boundaries
    tokens = t.split()
    while tokens and tokens[0] in _CLUB_TOKENS:
        tokens = tokens[1:]
    while tokens and tokens[-1] in _CLUB_TOKENS:
        tokens = tokens[:-1]
    return ' '.join(tokens)


def _parse_commence_date(commence_time: str) -> str | None:
    """Return YYYY-MM-DD from ISO commence_time."""
    if not commence_time or not isinstance(commence_time, str):
        return None
    s = commence_time.strip()
    try:
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.date().isoformat()
    except Exception:
        return None



def fetch_odds_for_league(league_code: str) -> list[dict]:
    """
    Fetch upcoming odds for the given football league via API-Football.
    Returns list of normalized odds events { home_team, away_team, date_iso, odds_by_market }.
    """
    return _fetch_odds_apif(league_code)


def _fetch_odds_apif(league_code: str) -> list[dict]:
    """
    Fetch odds from API-Football for the next 7 days.
    Uses the same season logic as refresh_apifootball: tries current year,
    falls back to previous year if no events found.
    Returns normalized list (same format as fetch_odds_for_league output) or [].
    """
    from config.settings import settings
    from data.providers.api_football import fetch_odds_apif
    from datetime import datetime, timezone, timedelta

    league_id = settings.get_apif_league_id(league_code)
    if not league_id:
        return []

    now = datetime.now(timezone.utc)
    today = now
    out: list[dict] = []
    seen_fixture_ids: set = set()

    def _fetch_season(season: int) -> list[dict]:
        results: list[dict] = []
        seen: set = set()
        for day_offset in range(8):
            date_str = (today + timedelta(days=day_offset)).strftime("%Y-%m-%d")
            try:
                events = fetch_odds_apif(league_id, season, date_str)
            except Exception as e:
                logger.debug("APIF odds fetch error league=%s date=%s: %s", league_code, date_str, e)
                continue
            for ev in events:
                fid = ev.get("fixture_id")
                if fid and fid in seen:
                    continue
                if fid:
                    seen.add(fid)
                results.append(ev)
        return results

    season = settings.get_apif_season(league_code)
    out = _fetch_season(season)
    if not out:
        prev = season - 1
        out = _fetch_season(prev)
        if out:
            logger.info("APIF odds for %s: using prev season %s (%d events)", league_code, prev, len(out))

    logger.info("APIF odds for %s: %d events over 8 days", league_code, len(out))
    return out


def _match_key(m: dict) -> tuple[str, str, str]:
    """Key for matching: (normalized_home, normalized_away, date_iso)."""
    home = _normalize_team(m.get("home") or m.get("home_team") or "")
    away = _normalize_team(m.get("away") or m.get("away_team") or "")
    utc = m.get("utcDate") or m.get("commence_time") or ""
    date_iso = _parse_commence_date(utc) if utc else ""
    return (home, away, date_iso or "")


def match_odds_to_matches(
    odds_events: list[dict],
    matches: list[dict],
) -> dict[tuple[str, str, str], dict]:
    """
    Build lookup (match_key -> odds_row) so we can find odds for each AFTR match.
    match_key = (normalized_home, normalized_away, date_iso).
    """
    by_key: dict[tuple[str, str, str], dict] = {}
    for o in odds_events:
        home = _normalize_team(o.get("home_team") or "")
        away = _normalize_team(o.get("away_team") or "")
        date_iso = (o.get("date_iso") or "").strip()
        if not date_iso:
            continue
        by_key[(home, away, date_iso)] = o

    return by_key


def get_odds_for_match(
    match: dict,
    odds_lookup: dict[tuple[str, str, str], dict],
) -> dict | None:
    """Return normalized odds row for this match, or None."""
    key = _match_key(match)
    return odds_lookup.get(key)


def load_odds_from_cache(league_code: str) -> list[dict]:
    """Load cached odds for league. Returns list of normalized odds events."""
    filename = ODDS_CACHE_FILE.format(league_code=league_code)
    data = read_json(filename)
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


def save_odds_to_cache(league_code: str, odds_events: list[dict]) -> None:
    """Persist odds for league."""
    filename = ODDS_CACHE_FILE.format(league_code=league_code)
    write_json(filename, odds_events)
    logger.info("Saved %d odds events to %s", len(odds_events), filename)


def ensure_odds_for_league(league_code: str, matches: list[dict], use_cache_first: bool = True) -> list[dict]:
    """
    Return list of normalized odds events for the league: from cache if use_cache_first and cache exists and has data,
    else fetch from API and write cache. Then filter to only events that match one of the given matches (by key).
    """
    if use_cache_first:
        cached = load_odds_from_cache(league_code)
        if cached:
            return cached
    raw = fetch_odds_for_league(league_code)
    if raw:
        save_odds_to_cache(league_code, raw)
    return raw
