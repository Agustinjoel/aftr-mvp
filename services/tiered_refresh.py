"""
Auto-refresh por capas: LIVE (en juego), UPCOMING (programados + odds), RESULTS (finished + settlement).
Cada job tiene lock propio; jobs pesados comparten refresh_running en caché (UI) con try_begin_global_refresh_busy.

Nota: ligas basketball (p. ej. NBA) no entran en estos jobs; usar `python -m app.cli refresh` para refresco completo.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from config.settings import settings
from data.cache import (
    read_json,
    release_refresh_running_meta,
    try_begin_global_refresh_busy,
    write_json,
)
from data.providers.football_data import (
    football_data_refresh_cycle,
    get_football_data_cycle_stats_snapshot,
)
from services.refresh import (
    RefreshMetrics,
    _league_is_fresh,
    _load_league_last_refresh,
    _save_league_last_refresh,
    refresh_league,
)
from services.refresh_rate_guard import (
    apply_backoff_seconds,
    register_rate_pressure_from_stats,
    seconds_to_wait_for_backoff,
)

logger = logging.getLogger("aftr.tiered_refresh")

STATE_FILE = "auto_refresh_tiered_state.json"

_live_lock = threading.Lock()
_upcoming_lock = threading.Lock()
_results_lock = threading.Lock()

_rr_lock = threading.Lock()
_rr_upcoming_idx = 0
_rr_results_idx = 0


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _football_league_codes() -> list[str]:
    return [
        c
        for c in settings.league_codes()
        if getattr(settings, "league_sport", {}).get(c, "football") != "basketball"
    ]


def _load_state() -> dict:
    raw = read_json(STATE_FILE)
    return raw if isinstance(raw, dict) else {}


def _save_state_patch(patch: dict) -> None:
    st = _load_state()
    st.update(patch)
    write_json(STATE_FILE, st)


def _seconds_since(ts_key: str, state: dict) -> float:
    try:
        t = float(state.get(ts_key) or 0)
    except (TypeError, ValueError):
        return 1e9
    return max(0.0, time.time() - t)


@dataclass
class JobOutcome:
    job: str
    skipped: bool = False
    skip_reason: str = ""
    err: str | None = None
    leagues_touched: int = 0
    matches_updated: int = 0
    http_requests: int = 0
    rate_limit_sleep_sec: int = 0


def run_live_refresh_job() -> JobOutcome:
    out = JobOutcome(job="live")
    if not _live_lock.acquire(blocking=False):
        out.skipped = True
        out.skip_reason = "lock"
        logger.info("AUTO REFRESH LIVE SKIPPED (already running) | %s", _utc_iso())
        return out
    try:
        w = seconds_to_wait_for_backoff()
        if w > 0:
            logger.info("AUTO REFRESH LIVE waiting backoff %.0fs | %s", w, _utc_iso())
            time.sleep(w)

        st = _load_state()
        min_gap = max(10, int(getattr(settings, "live_refresh_min_interval_sec", 30) or 30))
        if _seconds_since("last_live_ts", st) < min_gap:
            out.skipped = True
            out.skip_reason = "fresh"
            logger.info(
                "AUTO REFRESH LIVE SKIPPED (fresh, interval<%ss) | %s",
                min_gap,
                _utc_iso(),
            )
            return out

        logger.info("AUTO REFRESH LIVE START | %s", _utc_iso())
        codes = _football_league_codes()
        metrics = RefreshMetrics()
        any_live = False
        with football_data_refresh_cycle():
            for code in codes:
                try:
                    nu, _ = refresh_league(
                        code,
                        mode="live",
                        fetch_odds=False,
                        metrics=metrics,
                    )
                    if nu > 0:
                        any_live = True
                except Exception as e:
                    logger.warning("AUTO REFRESH LIVE league %s: %s", code, e)

        stats = get_football_data_cycle_stats_snapshot()
        out.http_requests = int(stats.get("http_requests", 0))
        out.rate_limit_sleep_sec = int(stats.get("rate_limit_sleep_sec", 0))
        out.matches_updated = metrics.matches_updated
        out.leagues_touched = len(codes)

        cap = float(getattr(settings, "refresh_backoff_seconds", 120) or 120)
        apply_backoff_seconds(register_rate_pressure_from_stats(stats), cap)

        _save_state_patch({"last_live_ts": time.time()})

        if not any_live:
            out.skipped = True
            out.skip_reason = "no_live_matches"
            logger.info("AUTO REFRESH LIVE SKIPPED (no live matches) | %s", _utc_iso())
        else:
            logger.info(
                "AUTO REFRESH LIVE SUCCESS | http=%d rate_sleep_s=%d matches_updated=%d | %s",
                out.http_requests,
                out.rate_limit_sleep_sec,
                out.matches_updated,
                _utc_iso(),
            )
        return out
    except Exception as e:
        out.err = str(e)
        logger.exception("AUTO REFRESH LIVE ERROR: %s | %s", e, _utc_iso())
        return out
    finally:
        _live_lock.release()
        logger.info("AUTO REFRESH END | job=live | %s", _utc_iso())


def _round_robin_batch(codes: list[str], cursor: int, n: int) -> tuple[list[str], int]:
    if not codes or n <= 0 or n >= len(codes):
        return list(codes), 0
    out = [codes[(cursor + i) % len(codes)] for i in range(n)]
    nxt = (cursor + n) % len(codes)
    return out, nxt


def run_upcoming_refresh_job() -> JobOutcome:
    out = JobOutcome(job="upcoming")
    if not _upcoming_lock.acquire(blocking=False):
        out.skipped = True
        out.skip_reason = "lock"
        logger.info("AUTO REFRESH UPCOMING SKIPPED (already running) | %s", _utc_iso())
        return out
    heavy = False
    try:
        w = seconds_to_wait_for_backoff()
        if w > 0:
            logger.info("AUTO REFRESH UPCOMING waiting backoff %.0fs | %s", w, _utc_iso())
            time.sleep(w)

        st = _load_state()
        min_m = max(1, int(getattr(settings, "upcoming_refresh_min", 15) or 15))
        if _seconds_since("last_upcoming_ts", st) < min_m * 60:
            out.skipped = True
            out.skip_reason = "fresh"
            logger.info(
                "AUTO REFRESH UPCOMING SKIPPED (fresh, <%dm) | %s",
                min_m,
                _utc_iso(),
            )
            return out

        if not try_begin_global_refresh_busy():
            out.skipped = True
            out.skip_reason = "global_refresh_busy"
            logger.info(
                "AUTO REFRESH UPCOMING SKIPPED (global refresh_running) | %s",
                _utc_iso(),
            )
            return out
        heavy = True

        logger.info("AUTO REFRESH UPCOMING START | %s", _utc_iso())
        fb = _football_league_codes()
        batch_n = int(getattr(settings, "auto_refresh_leagues_per_cycle", 4) or 4)
        global _rr_upcoming_idx
        with _rr_lock:
            batch, _rr_upcoming_idx = _round_robin_batch(fb, _rr_upcoming_idx, batch_n)
        fetch_odds = bool(getattr(settings, "auto_refresh_fetch_odds", False))
        skip_fresh_min = int(getattr(settings, "refresh_skip_if_fresh_min", 0) or 0)
        last_ok = _load_league_last_refresh() if skip_fresh_min > 0 else {}

        metrics = RefreshMetrics()
        touched = 0
        with football_data_refresh_cycle():
            for code in batch:
                if skip_fresh_min > 0 and _league_is_fresh(code, last_ok, skip_fresh_min):
                    logger.info("AUTO REFRESH UPCOMING skip league %s (fresh)", code)
                    continue
                try:
                    refresh_league(
                        code,
                        mode="upcoming",
                        fetch_odds=fetch_odds,
                        metrics=metrics,
                    )
                    touched += 1
                    _save_league_last_refresh({code: datetime.now(timezone.utc).isoformat()})
                except Exception as e:
                    logger.warning("AUTO REFRESH UPCOMING league %s: %s", code, e)

        stats = get_football_data_cycle_stats_snapshot()
        out.http_requests = int(stats.get("http_requests", 0))
        out.rate_limit_sleep_sec = int(stats.get("rate_limit_sleep_sec", 0))
        out.matches_updated = metrics.matches_updated
        out.leagues_touched = touched

        cap = float(getattr(settings, "refresh_backoff_seconds", 120) or 120)
        apply_backoff_seconds(register_rate_pressure_from_stats(stats), cap)

        _save_state_patch({"last_upcoming_ts": time.time()})
        logger.info(
            "AUTO REFRESH UPCOMING SUCCESS | leagues=%d http=%d matches_updated=%d | %s",
            touched,
            out.http_requests,
            out.matches_updated,
            _utc_iso(),
        )
        return out
    except Exception as e:
        out.err = str(e)
        logger.exception("AUTO REFRESH UPCOMING ERROR: %s | %s", e, _utc_iso())
        return out
    finally:
        if heavy:
            release_refresh_running_meta()
        _upcoming_lock.release()
        logger.info("AUTO REFRESH END | job=upcoming | %s", _utc_iso())


def run_results_refresh_job() -> JobOutcome:
    out = JobOutcome(job="results")
    if not _results_lock.acquire(blocking=False):
        out.skipped = True
        out.skip_reason = "lock"
        logger.info("AUTO REFRESH RESULTS SKIPPED (already running) | %s", _utc_iso())
        return out
    heavy = False
    try:
        w = seconds_to_wait_for_backoff()
        if w > 0:
            logger.info("AUTO REFRESH RESULTS waiting backoff %.0fs | %s", w, _utc_iso())
            time.sleep(w)

        st = _load_state()
        min_m = max(1, int(getattr(settings, "results_refresh_min", 10) or 10))
        if _seconds_since("last_results_ts", st) < min_m * 60:
            out.skipped = True
            out.skip_reason = "fresh"
            logger.info(
                "AUTO REFRESH RESULTS SKIPPED (fresh, <%dm) | %s",
                min_m,
                _utc_iso(),
            )
            return out

        if not try_begin_global_refresh_busy():
            out.skipped = True
            out.skip_reason = "global_refresh_busy"
            logger.info(
                "AUTO REFRESH RESULTS SKIPPED (global refresh_running) | %s",
                _utc_iso(),
            )
            return out
        heavy = True

        logger.info("AUTO REFRESH RESULTS START | %s", _utc_iso())
        fb = _football_league_codes()
        batch_n = int(getattr(settings, "auto_refresh_leagues_per_cycle", 4) or 4)
        finished_days = max(1, int(getattr(settings, "auto_refresh_finished_days", 3) or 3))
        global _rr_results_idx
        with _rr_lock:
            batch, _rr_results_idx = _round_robin_batch(fb, _rr_results_idx, batch_n)

        metrics = RefreshMetrics()
        touched = 0
        with football_data_refresh_cycle():
            for code in batch:
                try:
                    refresh_league(
                        code,
                        mode="results",
                        finished_days_back=finished_days,
                        fetch_odds=False,
                        metrics=metrics,
                    )
                    touched += 1
                except Exception as e:
                    logger.warning("AUTO REFRESH RESULTS league %s: %s", code, e)

        stats = get_football_data_cycle_stats_snapshot()
        out.http_requests = int(stats.get("http_requests", 0))
        out.rate_limit_sleep_sec = int(stats.get("rate_limit_sleep_sec", 0))
        out.matches_updated = metrics.matches_updated
        out.leagues_touched = touched

        cap = float(getattr(settings, "refresh_backoff_seconds", 120) or 120)
        apply_backoff_seconds(register_rate_pressure_from_stats(stats), cap)

        _save_state_patch({"last_results_ts": time.time()})
        logger.info(
            "AUTO REFRESH RESULTS SUCCESS | leagues=%d http=%d matches_updated=%d | %s",
            touched,
            out.http_requests,
            out.matches_updated,
            _utc_iso(),
        )
        return out
    except Exception as e:
        out.err = str(e)
        logger.exception("AUTO REFRESH RESULTS ERROR: %s | %s", e, _utc_iso())
        return out
    finally:
        if heavy:
            release_refresh_running_meta()
        _results_lock.release()
        logger.info("AUTO REFRESH END | job=results | %s", _utc_iso())
