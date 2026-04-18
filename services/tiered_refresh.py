"""
Auto-refresh por capas: LIVE, RESULTS, ODDS/PRE-MATCH.
- LIVE: ligas con IN_PLAY/PAUSED en caché, o con kickoff ya pasado y estado no final (evita gallina-huevo
  cuando la caché solo tenía TIMED hasta el primer poll).
- RESULTS: ventana corta (RESULTS_FINISHED_HOURS).
- ODDS: solo ligas con partido en ventana ODDS_PREMATCH_HOURS; respeta frescura de daily_odds_*.json.

Jobs pesados (results/odds) usan try_begin_global_refresh_busy + release en finally.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from config.settings import settings
from data.cache import (
    read_cache_meta,
    read_json,
    release_refresh_running_meta,
    try_begin_global_refresh_busy,
    write_json,
)
from services.refresh import (
    RefreshMetrics,
    _league_is_fresh,
    _load_league_last_refresh,
    _parse_utcdate_str,
    _save_league_last_refresh,
)
from services.refresh_rate_guard import seconds_to_wait_for_backoff

logger = logging.getLogger("aftr.tiered_refresh")

STATE_FILE = "auto_refresh_tiered_state.json"

LIVE_HINT_STATUSES = frozenset({"IN_PLAY", "PAUSED", "LIVE"})

_live_lock = threading.Lock()
_live_lock_ts: float = 0.0          # timestamp de adquisición del live lock
_live_lock_mu = threading.Lock()    # protege _live_lock_ts
_LIVE_LOCK_MAX_SEC = 300            # 5 minutos → fuerza liberación
_odds_lock = threading.Lock()
_results_lock = threading.Lock()

_rr_lock = threading.Lock()
_rr_odds_idx = 0
_rr_results_idx = 0
_state_lock = threading.Lock()


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def reset_live_lock() -> None:
    """Libera el live lock en memoria (llamar al arrancar la app para limpiar locks colgados)."""
    global _live_lock_ts
    with _live_lock_mu:
        _live_lock_ts = 0.0


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
    with _state_lock:
        st = _load_state()
        st.update(patch)
        write_json(STATE_FILE, st)


def _seconds_since(ts_key: str, state: dict) -> float:
    try:
        t = float(state.get(ts_key) or 0)
    except (TypeError, ValueError):
        return 1e9
    return max(0.0, time.time() - t)


def _last_odds_ts(state: dict) -> float:
    try:
        return float(state.get("last_odds_ts") or state.get("last_upcoming_ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def _leagues_needing_live_poll(max_hours_after_kickoff: float = 5.0) -> list[str]:
    """
    Unión de ligas con pista IN_PLAY y con kickoff pasado-no-final.
    Loop único por liga: parsea cada JSON una sola vez para detectar ambas condiciones.
    """
    now = datetime.now(timezone.utc)
    fin_like = frozenset(
        {"FINISHED", "FT", "FINAL", "AWARDED", "CANCELLED", "POSTPONED", "SETTLED", "FINALIZADO"},
    )
    max_sec = max_hours_after_kickoff * 3600
    hinted: set[str] = set()

    for code in _football_league_codes():
        data = read_json(f"daily_matches_{code}.json")
        if not isinstance(data, list):
            continue
        for m in data:
            if not isinstance(m, dict):
                continue
            st = (m.get("status") or "").upper()
            # Condición 1: live hint explícito
            if st in LIVE_HINT_STATUSES:
                hinted.add(code)
                break
            # Condición 2: kickoff pasado, estado no final, dentro de ventana
            if st in fin_like:
                continue
            dt = _parse_utcdate_str(m.get("utcDate"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt > now:
                continue
            if (now - dt).total_seconds() > max_sec:
                continue
            hinted.add(code)
            break

    return sorted(hinted)


def _league_in_prematch_window(league_code: str, hours: int, data: list | None = None) -> bool:
    """True si hay partido en las próximas `hours` h (o empezó hace <2h), excl. FINISHED.
    Acepta `data` pre-cargado para evitar doble parseo cuando el caller ya leyó el archivo."""
    if data is None:
        data = read_json(f"daily_matches_{league_code}.json")
    if not isinstance(data, list):
        return False
    now = datetime.now(timezone.utc)
    for m in data:
        if not isinstance(m, dict):
            continue
        st = (m.get("status") or "").upper()
        if st in ("FINISHED", "FT", "AWARDED"):
            continue
        dt = _parse_utcdate_str(m.get("utcDate"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = (dt - now).total_seconds()
        if -7200 <= delta <= hours * 3600:
            return True
    return False


def _odds_file_is_fresh(league_code: str, min_minutes: int) -> bool:
    if min_minutes <= 0:
        return False
    p = settings.cache_dir / f"daily_odds_{league_code}.json"
    try:
        if not p.exists():
            return False
        age = time.time() - p.stat().st_mtime
        return age < min_minutes * 60
    except OSError:
        return False


def _finished_days_from_results_hours(hours: int) -> int:
    """Hasta 14 días de historial — un solo call a la API con rango de fechas extendido."""
    return max(1, min(14, (int(hours) + 23) // 24))


def _global_refresh_blocks() -> bool:
    return bool(read_cache_meta().get("refresh_running"))


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


def _refresh_live_from_api() -> int:
    """
    Llama a fetch_live_fixtures() (endpoint /fixtures?live=all, sin filtro de liga/season),
    normaliza los resultados y parchea daily_matches_*.json con scores y status en vivo.
    Devuelve el número de partidos actualizados.
    Funciona aunque daily_matches_*.json esté vacío (filesystem efímero).
    """
    try:
        from data.providers.api_football import fetch_live_fixtures, normalize_apif_fixture, _api_key
        from services.refresh_utils import _normalize_match
        from config.settings import settings as _settings
    except Exception as _import_err:
        logger.warning("_refresh_live_from_api: import error: %s", _import_err)
        return 0

    if not _api_key():
        return 0

    try:
        live_raw = fetch_live_fixtures()
    except Exception as _e:
        logger.warning("_refresh_live_from_api: fetch_live_fixtures error: %s", _e)
        return 0

    if not live_raw:
        return 0

    # Build reverse index: apif fixture_id → league_code
    apif_id_map: dict[int, str] = {}
    for code in _football_league_codes():
        lid = _settings.get_apif_league_id(code)
        if lid:
            apif_id_map[int(lid)] = code

    # Group live fixtures by league_code
    by_league: dict[str, list[dict]] = {}
    for fx in live_raw:
        if not isinstance(fx, dict):
            continue
        league_info = (fx.get("league") or {})
        lid = league_info.get("id")
        if lid is None:
            continue
        code = apif_id_map.get(int(lid))
        if not code:
            continue
        normalized = normalize_apif_fixture(fx, code)
        if normalized:
            by_league.setdefault(code, []).append(normalized)

    updated = 0
    for code, live_matches in by_league.items():
        existing = read_json(f"daily_matches_{code}.json")
        existing_list: list[dict] = existing if isinstance(existing, list) else []

        # Build lookup of existing matches by match_id
        ex_by_id: dict[int, dict] = {}
        for m in existing_list:
            if not isinstance(m, dict):
                continue
            mid_raw = m.get("match_id") or m.get("id")
            if mid_raw is not None:
                try:
                    ex_by_id[int(mid_raw)] = m
                except (TypeError, ValueError):
                    pass

        for lm in live_matches:
            mid_raw = lm.get("match_id") or lm.get("id")
            if mid_raw is None:
                continue
            try:
                mid = int(mid_raw)
            except (TypeError, ValueError):
                continue
            if mid in ex_by_id:
                # Patch in-place: update score + status fields
                ex_by_id[mid].update({
                    k: lm[k] for k in (
                        "status", "home_goals", "away_goals", "minute",
                        "score_home", "score_away", "home_score", "away_score",
                    )
                    if k in lm
                })
                updated += 1
            else:
                existing_list.append(lm)
                ex_by_id[mid] = lm
                updated += 1

        write_json(f"daily_matches_{code}.json", existing_list)
        logger.info("_refresh_live_from_api: %s — %d live matches patched", code, len(live_matches))

    logger.info("_refresh_live_from_api: total updated=%d leagues=%s", updated, list(by_league.keys()))
    return updated


def run_live_refresh_job() -> JobOutcome:
    out = JobOutcome(job="live")
    # Time-based live lock: expires after _LIVE_LOCK_MAX_SEC (5 min) to avoid eternal block
    with _live_lock_mu:
        global _live_lock_ts
        now_ts = time.time()
        if _live_lock_ts > 0 and now_ts - _live_lock_ts < _LIVE_LOCK_MAX_SEC:
            out.skipped = True
            out.skip_reason = "lock"
            logger.info("AUTO REFRESH LIVE SKIPPED (lock, %.0fs remaining) | %s",
                        _LIVE_LOCK_MAX_SEC - (now_ts - _live_lock_ts), _utc_iso())
            return out
        _live_lock_ts = now_ts
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

        # Always attempt direct live poll first (works even with empty daily_matches_*.json)
        n_live_direct = _refresh_live_from_api()
        logger.info("AUTO REFRESH LIVE direct_api_updated=%d | %s", n_live_direct, _utc_iso())

        hints = _leagues_needing_live_poll()
        if not hints:
            out.skipped = True
            out.skip_reason = "no_live_matches"
            logger.info("AUTO REFRESH LIVE SKIPPED (no live matches via hints) | %s", _utc_iso())
            _save_state_patch({"last_live_ts": time.time()})
            return out

        logger.info("AUTO REFRESH START | job=live | %s", _utc_iso())
        logger.info("LIVE REFRESH RUNNING | leagues=%s | %s", hints, _utc_iso())

        metrics = RefreshMetrics()
        try:
            from services.refresh_apifootball import apif_refresh_league
            for code in hints:
                try:
                    apif_refresh_league(
                        code,
                        days_upcoming=1,
                        days_finished=1,
                        fetch_odds=False,
                        metrics=metrics,
                    )
                except Exception as e:
                    logger.warning("AUTO REFRESH LIVE league %s: %s", code, e)
        except Exception as _apif_err:
            logger.warning("apif_refresh import error (live): %s", _apif_err)

        out.matches_updated = metrics.matches_updated
        out.leagues_touched = len(hints)

        _save_state_patch({"last_live_ts": time.time()})

        # Live events — API-Football (RapidAPI, pago): más rápido + minuto exacto en goles.
        # Se ejecuta primero y deja sus flags en el state file.
        try:
            from services.live_events import process_live_events
            from data.providers.api_football import _api_key
            if _api_key():
                n_live = process_live_events()
                if n_live:
                    logger.info("apifootball_live_events sent %d push notifications", n_live)
        except Exception as _live_err:
            logger.warning("live_events error (non-fatal): %s", _live_err)

        # Live events — motor de caché (diff de caché): corre SIEMPRE como cobertura
        # complementaria. La deduplicación se hace por nombre de equipos en el state file
        # compartido: hereda los flags del motor API-Football y no vuelve a notificar.
        try:
            from services.live_events import process_cache_live_events
            n_cache = process_cache_live_events(hints)
            if n_cache:
                logger.info("cache_live_events sent %d push notifications", n_cache)
        except Exception as _cache_err:
            logger.warning("cache_live_events error (non-fatal): %s", _cache_err)

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
        global _live_lock_ts
        with _live_lock_mu:
            _live_lock_ts = 0.0
        logger.info("AUTO REFRESH END | job=live | %s", _utc_iso())


def _round_robin_batch(codes: list[str], cursor: int, n: int) -> tuple[list[str], int]:
    if not codes or n <= 0 or n >= len(codes):
        return list(codes), 0
    out = [codes[(cursor + i) % len(codes)] for i in range(n)]
    nxt = (cursor + n) % len(codes)
    return out, nxt


def run_results_refresh_job() -> JobOutcome:
    out = JobOutcome(job="results")
    if not _results_lock.acquire(blocking=False):
        out.skipped = True
        out.skip_reason = "lock"
        logger.info("AUTO REFRESH RESULTS SKIPPED (already running) | %s", _utc_iso())
        return out
    heavy = False
    try:
        if _global_refresh_blocks():
            out.skipped = True
            out.skip_reason = "global_refresh_running"
            logger.info("REFRESH SKIPPED (already running) | job=results | %s", _utc_iso())
            return out

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

        hours = int(getattr(settings, "results_finished_hours", 48) or 48)
        finished_days = _finished_days_from_results_hours(hours)

        logger.info("AUTO REFRESH START | job=results | window_h=%d days_api=%d | %s", hours, finished_days, _utc_iso())
        logger.info("RESULTS REFRESH RUNNING | %s", _utc_iso())

        fb = _football_league_codes()
        batch_n = int(getattr(settings, "auto_refresh_leagues_per_cycle", 4) or 4)
        global _rr_results_idx
        with _rr_lock:
            batch, _rr_results_idx = _round_robin_batch(fb, _rr_results_idx, batch_n)

        metrics = RefreshMetrics()
        touched = 0

        # ── API-Football leagues ──────────────────────────────────────────────
        try:
            from services.refresh_apifootball import apif_refresh_league
            for code in batch:
                try:
                    apif_refresh_league(
                        code,
                        days_upcoming=7,
                        days_finished=finished_days,
                        fetch_odds=settings.auto_refresh_fetch_odds,
                        metrics=metrics,
                    )
                    touched += 1
                except Exception as e:
                    logger.warning("AUTO REFRESH RESULTS (apif) league %s: %s", code, e)
        except Exception as _apif_err:
            logger.warning("apif_refresh import error: %s", _apif_err)

        out.http_requests = 0
        out.rate_limit_sleep_sec = 0
        out.matches_updated = metrics.matches_updated
        out.leagues_touched = touched

        _save_state_patch({"last_results_ts": time.time()})

        # Auto-settle tracker bet legs whose matches are now finished
        try:
            from services.auto_settle import auto_settle_tracker_legs
            n_settled = auto_settle_tracker_legs()
            if n_settled:
                logger.info("auto_settle resolved %d tracker legs", n_settled)
        except Exception as _settle_err:
            logger.warning("auto_settle error (non-fatal): %s", _settle_err)

        # Push notifications: se llaman aquí (results job, corre siempre cada ~10min)
        # y NO en el live job que se saltea cuando no hay partidos en vivo.
        try:
            from services.push_notifications import notify_tracker_bets, notify_trial_expiring, notify_upcoming_picks, load_user_follows_index
            from data.cache import read_json_with_fallback
            notify_tracker_bets()
            notify_trial_expiring()
            # notify_upcoming_picks: picks que empiezan en <=60min (corre aquí y no en live
            # job para que funcione aunque no haya partidos en vivo todavía)
            follows_index = load_user_follows_index()
            if follows_index:
                all_picks: list[dict] = []
                for _code in settings.league_codes():
                    _picks = read_json_with_fallback(f"daily_picks_{_code}.json")
                    if isinstance(_picks, list):
                        all_picks.extend(_picks)
                notify_upcoming_picks(all_picks, follows_index)
        except Exception as _push_err:
            logger.warning("push_notifications error (non-fatal): %s", _push_err)

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


def run_odds_refresh_job() -> JobOutcome:
    """Pre-match / programados + odds (solo ligas con partido próximo y odds no demasiado frescas)."""
    out = JobOutcome(job="odds")
    if not _odds_lock.acquire(blocking=False):
        out.skipped = True
        out.skip_reason = "lock"
        logger.info("AUTO REFRESH ODDS SKIPPED (already running) | %s", _utc_iso())
        return out
    heavy = False
    try:
        if _global_refresh_blocks():
            out.skipped = True
            out.skip_reason = "global_refresh_running"
            logger.info("REFRESH SKIPPED (already running) | job=odds | %s", _utc_iso())
            return out

        w = seconds_to_wait_for_backoff()
        if w > 0:
            logger.info("AUTO REFRESH ODDS waiting backoff %.0fs | %s", w, _utc_iso())
            time.sleep(w)

        st = _load_state()
        min_m = max(1, int(getattr(settings, "upcoming_refresh_min", 15) or 15))
        last_o = _last_odds_ts(st)
        if last_o and (time.time() - last_o) < min_m * 60:
            out.skipped = True
            out.skip_reason = "fresh"
            logger.info(
                "AUTO REFRESH ODDS SKIPPED (fresh, <%dm) | %s",
                min_m,
                _utc_iso(),
            )
            return out

        if not try_begin_global_refresh_busy():
            out.skipped = True
            out.skip_reason = "global_refresh_busy"
            logger.info(
                "AUTO REFRESH ODDS SKIPPED (global refresh_running) | %s",
                _utc_iso(),
            )
            return out
        heavy = True

        prematch_h = int(getattr(settings, "odds_prematch_hours", 24) or 24)
        odds_min = int(getattr(settings, "odds_min_refresh_minutes", 20) or 20)
        want_odds = bool(getattr(settings, "auto_refresh_fetch_odds", False))

        logger.info(
            "AUTO REFRESH START | job=odds | prematch_h=%d odds_min=%d fetch_odds=%s | %s",
            prematch_h,
            odds_min,
            want_odds,
            _utc_iso(),
        )
        logger.info("ODDS REFRESH RUNNING | %s", _utc_iso())

        fb = _football_league_codes()
        batch_n = int(getattr(settings, "auto_refresh_leagues_per_cycle", 4) or 4)
        global _rr_odds_idx
        with _rr_lock:
            batch, _rr_odds_idx = _round_robin_batch(fb, _rr_odds_idx, batch_n)

        skip_fresh_min = int(getattr(settings, "refresh_skip_if_fresh_min", 0) or 0)
        last_ok = _load_league_last_refresh() if skip_fresh_min > 0 else {}

        metrics = RefreshMetrics()
        touched = 0

        # ── API-Football: upcoming + picks ────────────────────────────────────
        try:
            from services.refresh_apifootball import apif_refresh_league
            for code in batch:
                league_matches = read_json(f"daily_matches_{code}.json")
                league_has_cache = isinstance(league_matches, list) and len(league_matches) > 0
                if league_has_cache and not _league_in_prematch_window(code, prematch_h, data=league_matches):
                    logger.info("AUTO REFRESH ODDS skip %s (no match in prematch window)", code)
                    continue
                if skip_fresh_min > 0 and _league_is_fresh(code, last_ok, skip_fresh_min):
                    logger.info("AUTO REFRESH ODDS skip %s (league fresh)", code)
                    continue
                try:
                    apif_refresh_league(
                        code,
                        days_upcoming=7,
                        days_finished=3,
                        fetch_odds=want_odds and not _odds_file_is_fresh(code, odds_min),
                        metrics=metrics,
                    )
                    touched += 1
                    _save_league_last_refresh({code: datetime.now(timezone.utc).isoformat()})
                except Exception as e:
                    logger.warning("AUTO REFRESH ODDS league %s: %s", code, e)
        except Exception as _apif_err:
            logger.warning("apif_refresh import error: %s", _apif_err)

        out.http_requests = 0
        out.rate_limit_sleep_sec = 0
        out.matches_updated = metrics.matches_updated
        out.leagues_touched = touched

        now_ts = time.time()
        _save_state_patch({"last_odds_ts": now_ts, "last_upcoming_ts": now_ts})
        logger.info(
            "AUTO REFRESH ODDS SUCCESS | leagues=%d http=%d matches_updated=%d | %s",
            touched,
            out.http_requests,
            out.matches_updated,
            _utc_iso(),
        )
        return out
    except Exception as e:
        out.err = str(e)
        logger.exception("AUTO REFRESH ODDS ERROR: %s | %s", e, _utc_iso())
        return out
    finally:
        if heavy:
            release_refresh_running_meta()
        _odds_lock.release()
        logger.info("AUTO REFRESH END | job=odds | %s", _utc_iso())


# Compatibilidad con código que aún importa el nombre anterior
run_upcoming_refresh_job = run_odds_refresh_job


def run_tiered_refresh() -> dict[str, JobOutcome]:
    """
    Ejecuta LIVE → RESULTS → ODDS en secuencia (útil para cron o debug).
    Los loops asyncio siguen usando los jobs por separado.
    """
    logger.info("AUTO REFRESH START | tiered_sequential | %s", _utc_iso())
    live = run_live_refresh_job()
    results = run_results_refresh_job()
    odds = run_odds_refresh_job()
    logger.info("AUTO REFRESH END | tiered_sequential | %s", _utc_iso())
    return {"live": live, "results": results, "odds": odds}
