"""
Auto-refresh por tres tiers (asyncio): LIVE, RESULTS, ODDS/PRE-MATCH.
Los intervalos vienen de settings; la lógica vive en services.tiered_refresh.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from config.settings import settings
from services.tiered_refresh import (
    run_live_refresh_job,
    run_odds_refresh_job,
    run_results_refresh_job,
)

logger = logging.getLogger("aftr.auto_refresh")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _live_loop() -> None:
    sec = float(getattr(settings, "live_refresh_seconds", 30) or 30)
    logger.info(
        "AUTO REFRESH: LIVE loop | interval=%.0fs | %s",
        sec,
        _utc_iso(),
    )
    # Defer first job so uvicorn can finish binding and health checks before heavy work.
    await asyncio.sleep(5.0)
    while True:
        try:
            await asyncio.to_thread(run_live_refresh_job)
        except Exception as e:
            logger.exception("AUTO REFRESH LIVE loop unhandled error (continuing): %s", e)
        await asyncio.sleep(sec)


async def _odds_loop() -> None:
    await asyncio.sleep(5.0)
    sec = float(getattr(settings, "upcoming_refresh_min", 15) or 15) * 60.0
    logger.info(
        "AUTO REFRESH: ODDS/PRE-MATCH loop | interval=%.0fs | %s",
        sec,
        _utc_iso(),
    )
    while True:
        try:
            await asyncio.to_thread(run_odds_refresh_job)
        except Exception as e:
            logger.exception("AUTO REFRESH ODDS loop unhandled error (continuing): %s", e)
        await asyncio.sleep(sec)


async def _results_loop() -> None:
    await asyncio.sleep(12.0)
    sec = float(getattr(settings, "results_refresh_min", 10) or 10) * 60.0
    logger.info(
        "AUTO REFRESH: RESULTS loop | interval=%.0fs | %s",
        sec,
        _utc_iso(),
    )
    while True:
        try:
            await asyncio.to_thread(run_results_refresh_job)
        except Exception as e:
            logger.exception("AUTO REFRESH RESULTS loop unhandled error (continuing): %s", e)
        await asyncio.sleep(sec)


def spawn_auto_refresh_tasks() -> list[asyncio.Task[None]]:
    """Tres tareas en paralelo; lifespan debe cancelarlas al apagar."""
    return [
        asyncio.create_task(_live_loop(), name="aftr-tier-live"),
        asyncio.create_task(_odds_loop(), name="aftr-tier-odds"),
        asyncio.create_task(_results_loop(), name="aftr-tier-results"),
    ]
