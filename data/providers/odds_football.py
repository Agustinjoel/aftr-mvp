"""
Football odds provider. Fetches bookmaker odds (The Odds API), normalizes to AFTR market names,
matches to existing matches by home/away/date, and supports cache read/write.
NBA/other sports: use separate provider later; this module is football-only.
"""
from __future__ import annotations

import logging
import re as _re
from datetime import datetime, timezone
from typing import Any

import requests

from config.settings import ODDS_API_BASE, ODDS_API_KEY, ODDS_LEAGUE_SPORT_KEYS
from data.cache import read_json, write_json

logger = logging.getLogger(__name__)

ODDS_CACHE_FILE = "daily_odds_{league_code}.json"
ODDS_REGIONS = "uk,eu"
ODDS_MARKETS = "h2h,totals"
ODDS_FORMAT = "decimal"


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


def _normalize_event(ev: dict, sport_key: str) -> dict | None:
    """
    Turn one API event into AFTR-shaped odds: home, away, date_iso, odds by market.
    h2h outcomes: map home_team -> Home Win, away_team -> Away Win, Draw -> Draw.
    totals: keep Over/Under with point; we use point 2.5 as Over 2.5 / Under 2.5.
    """
    home_team = (ev.get("home_team") or "").strip()
    away_team = (ev.get("away_team") or "").strip()
    commence = ev.get("commence_time") or ""
    date_iso = _parse_commence_date(commence)
    if not home_team or not away_team or not date_iso:
        return None

    odds_by_market: dict[str, dict[str, float]] = {}
    bookmaker_title: str = ""
    for book in ev.get("bookmakers") or []:
        book_title = (book.get("title") or book.get("key") or "").strip()
        for mkt in book.get("markets") or []:
            key = (mkt.get("key") or "").strip().lower()
            if key == "h2h" and "h2h" not in odds_by_market:
                by_outcome: dict[str, float] = {}
                for out in mkt.get("outcomes") or []:
                    name = (out.get("name") or "").strip()
                    price = out.get("price")
                    if name and price is not None:
                        try:
                            dec = float(price)
                        except (TypeError, ValueError):
                            continue
                        if _normalize_team(name) == _normalize_team(home_team):
                            by_outcome["Home Win"] = dec
                        elif _normalize_team(name) == _normalize_team(away_team):
                            by_outcome["Away Win"] = dec
                        elif "draw" in name.lower():
                            by_outcome["Draw"] = dec
                if by_outcome:
                    odds_by_market["h2h"] = by_outcome
                    if not bookmaker_title and book_title:
                        bookmaker_title = book_title
            elif key == "totals" and "totals_25" not in odds_by_market:
                point = mkt.get("point")
                for out in mkt.get("outcomes") or []:
                    name = (out.get("name") or "").strip().lower()
                    price = out.get("price")
                    if price is None:
                        continue
                    try:
                        dec = float(price)
                    except (TypeError, ValueError):
                        continue
                    if point is not None:
                        try:
                            pt = float(point)
                            if abs(pt - 2.5) < 0.01:
                                if "over" in name:
                                    odds_by_market.setdefault("totals_25", {})["Over 2.5"] = dec
                                elif "under" in name:
                                    odds_by_market.setdefault("totals_25", {})["Under 2.5"] = dec
                                if not bookmaker_title and book_title:
                                    bookmaker_title = book_title
                        except (TypeError, ValueError):
                            pass
    if not odds_by_market:
        return None
    out: dict = {
        "home_team": home_team,
        "away_team": away_team,
        "date_iso": date_iso,
        "commence_time": commence,
        "odds_by_market": odds_by_market,
    }
    if bookmaker_title:
        out["bookmaker_title"] = bookmaker_title
    return out


def fetch_odds_for_league(league_code: str) -> list[dict]:
    """
    Fetch upcoming odds from The Odds API for the given football league.
    Returns list of normalized odds events { home_team, away_team, date_iso, odds_by_market }.
    Empty list on missing key, API error, or non-football league.
    """
    if not ODDS_API_KEY:
        logger.debug("ODDS_API_KEY not set; skipping odds fetch for %s", league_code)
        return []
    sport_key = ODDS_LEAGUE_SPORT_KEYS.get(league_code)
    if not sport_key:
        logger.debug("No odds sport key for league %s; skipping", league_code)
        return []
    url = f"{ODDS_API_BASE}/v4/sports/{sport_key}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": ODDS_REGIONS,
        "markets": ODDS_MARKETS,
        "oddsFormat": ODDS_FORMAT,
    }
    try:
        r = requests.get(url, params=params, timeout=25)
        if r.status_code == 401:
            logger.warning("Odds API 401 Unauthorized; check ODDS_API_KEY")
            return []
        if r.status_code == 429:
            logger.warning("Odds API 429 rate limit")
            return []
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning("Odds API request failed for %s: %s", league_code, e)
        return []

    out: list[dict] = []
    for ev in (data if isinstance(data, list) else []):
        if not isinstance(ev, dict):
            continue
        norm = _normalize_event(ev, sport_key)
        if norm:
            out.append(norm)
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
