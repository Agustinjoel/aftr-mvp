"""
Pipeline de refresco — punto de entrada público.

Este archivo fue refactorizado desde un monolito de ~1700 líneas a módulos separados.
Mantiene todos los exports anteriores para compatibilidad con:
  - services/__init__.py
  - services/tiered_refresh.py
  - services/refresh_basketball.py
  - app/cli.py

Módulos:
  refresh_utils.py    — parsing de fechas, safe casts, normalize_match
  refresh_teams.py    — team names, team stats, league freshness state
  refresh_results.py  — resultados, merge, history, window diaria
  refresh_picks.py    — construcción de picks modelo A+B
  refresh_odds.py     — odds enrichment + debug consolidado
  refresh_combos.py   — combos/parlays globales
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from config.settings import settings
from data.cache import (
    CACHE_META_FILENAME,
    read_json,
    release_refresh_running_meta,
    write_cache_meta,
)
# ── Re-exports para backward compat ──────────────────────────────────────────
# (los archivos que ya importan de services.refresh siguen funcionando sin cambios)

from services.refresh_utils import (          # noqa: F401
    _parse_iso_utc,
    _parse_utcdate,
    _parse_utcdate_str,
    _safe_float,
    _safe_int,
    _best_prob,
    _read_json_list,
    _normalize_match,
)
from services.refresh_teams import (          # noqa: F401
    _load_team_names_cache,
    _save_team_names_cache,
    _crest_from_id,
    _update_team_names_from_matches,
    _league_is_fresh,
    _load_league_last_refresh,
    _save_league_last_refresh,
    _result_letter_from_goals,
    _calc_team_stats_from_recent,
    _build_recent_compact,
    TEAM_NAMES_FILE,
    LEAGUE_REFRESH_STATE_FILE,
)
from services.refresh_results import (        # noqa: F401
    _build_finished_lookup_by_id,
    _scores_lookup_from_match_list,
    _apply_results_by_match_id,
    _merge_by_match_id,
    _save_history,
    _window_daily,
    _write_league_cache,
)
from services.refresh_picks import (          # noqa: F401
    _top2_from_candidates,
    _confidence_score,
    _build_picks_from_matches,
)
from services.refresh_odds import (           # noqa: F401
    _pick_debug_key,
    _enrich_football_picks_with_odds,
)
from services.refresh_combos import (         # noqa: F401
    _combo_sig,
    _tier_from_name_or_prob,
    _fix_tiers,
    _dedupe_window,
    _prune_next3d_overlap,
    _build_and_save_combos,
)

# ─────────────────────────────────────────────────────────────────────────────

logger = logging.getLogger(__name__)

# Lock global de proceso (CLI + auto-scheduler comparten este lock)
_refresh_global_lock = threading.Lock()
_auto_rr_index = 0


@dataclass
class RefreshMetrics:
    """Métricas acumuladas durante refresh_all."""
    matches_updated: int = 0


@dataclass
class RefreshAllResult:
    ran: bool
    skipped_busy: bool = False
    light_mode: bool = False
    leagues_refreshed: int = 0
    leagues_skipped_fresh: int = 0
    matches_updated: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# API pública principal
# ─────────────────────────────────────────────────────────────────────────────

def refresh_all(
    *,
    non_blocking: bool = False,
    light: bool = False,
) -> RefreshAllResult:
    """
    Refresca picks/partidos para todas las ligas configuradas.

    - light=True (auto-refresh): menos ventana FINISHED, menos ligas por ciclo,
      sin odds por defecto, salta ligas recién actualizadas.
    - non_blocking=True: retorna inmediatamente si ya hay un refresco corriendo.
    """
    global _auto_rr_index

    if not _refresh_global_lock.acquire(blocking=not non_blocking):
        logger.info("refresh_all: skipped (already running)")
        return RefreshAllResult(ran=False, skipped_busy=True, light_mode=light)

    result = RefreshAllResult(ran=True, light_mode=light)
    auto_log = logging.getLogger("aftr.auto_refresh")

    try:
        mode_label = "ligero" if light else "completo"
        logger.info("Iniciando refresco (%s)", mode_label)

        now_iso = datetime.now(timezone.utc).isoformat()
        raw_meta = read_json(CACHE_META_FILENAME)
        meta_base = dict(raw_meta) if isinstance(raw_meta, dict) else {}
        meta_base["refresh_running"] = True
        meta_base["refresh_started_at"] = now_iso
        meta_base["last_updated"] = meta_base.get("last_updated") or now_iso
        write_cache_meta(meta_base)

        if light:
            auto_log.info(
                "AUTO REFRESH START | refresh_running=true | started_at=%s | stuck_ttl=%ss",
                now_iso,
                int(getattr(settings, "refresh_running_ttl_sec", 0) or 0),
            )
        else:
            logger.info("REFRESH START | refresh_running=true | started_at=%s | %s", now_iso, mode_label)

        m_metrics = RefreshMetrics()

        codes = list(settings.league_codes())
        skip_min = int(getattr(settings, "refresh_skip_if_fresh_min", 0) or 0) if light else 0
        batch_n = int(getattr(settings, "auto_refresh_leagues_per_cycle", 0) or 0)

        if light:
            finished_days = max(1, int(getattr(settings, "auto_refresh_finished_days", 3) or 3))
            fetch_odds = bool(getattr(settings, "auto_refresh_fetch_odds", False))
        else:
            finished_days = 7
            fetch_odds = True

        if light and batch_n > 0 and batch_n < len(codes):
            n = len(codes)
            batch = [codes[(_auto_rr_index + i) % n] for i in range(batch_n)]
            _auto_rr_index = (_auto_rr_index + batch_n) % n
            logger.info(
                "refresh (light): round-robin batch %s (%d de %d ligas por ciclo)",
                batch, batch_n, n,
            )
        else:
            batch = codes

        last_ok = _load_league_last_refresh()

        for code in batch:
            if light and skip_min > 0 and _league_is_fresh(code, last_ok, skip_min):
                result.leagues_skipped_fresh += 1
                logger.info(
                    "refresh: skipping league %s (updated within last %d min)",
                    code, skip_min,
                )
                continue
            try:
                sport = settings.league_sport.get(code, "football")
                if sport == "basketball":
                    from services.refresh_basketball import refresh_league_basketball
                    refresh_league_basketball(code, finished_days_back=finished_days, metrics=m_metrics)
                else:
                    from services.refresh_apifootball import apif_refresh_league
                    apif_refresh_league(
                        code,
                        days_upcoming=7,
                        days_finished=finished_days,
                        fetch_odds=fetch_odds,
                        metrics=m_metrics,
                    )
                result.leagues_refreshed += 1
                _save_league_last_refresh({code: datetime.now(timezone.utc).isoformat()})
            except Exception as e:
                logger.exception("Error refrescando liga %s: %s", code, e)

        result.matches_updated = m_metrics.matches_updated

        logger.info(
            "refresh summary: leagues_refreshed=%d skipped_fresh=%d matches_updated=%d",
            result.leagues_refreshed,
            result.leagues_skipped_fresh,
            result.matches_updated,
        )
        logger.info("✅ Refresco finalizado")

        if light:
            auto_log.info(
                "AUTO REFRESH SUCCESS | leagues_refreshed=%d matches_updated=%d | %s",
                result.leagues_refreshed,
                result.matches_updated,
                datetime.now(timezone.utc).isoformat(),
            )
        return result

    except Exception as e:
        ts = datetime.now(timezone.utc).isoformat()
        if light:
            auto_log.error("AUTO REFRESH ERROR: %s | %s", e, ts)
        logger.exception("REFRESH ERROR: %s | %s", e, ts)
        raise

    finally:
        try:
            release_refresh_running_meta()
        except Exception as fin_e:
            logger.critical(
                "CRITICAL: release_refresh_running_meta falló (lock de proceso se libera igual): %s",
                fin_e,
                exc_info=True,
            )
        _refresh_global_lock.release()
