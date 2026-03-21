"""
Auto-refresh por tres tiers (asyncio): LIVE, UPCOMING, RESULTS.
Cada uno corre en su propio loop con intervalo configurable; la lógica pesada va en threads.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from config.settings import settings
from services.tiered_refresh import (
    run_live_refresh_job,
    run_results_refresh_job,
    run_upcoming_refresh_job,
)

logger = logging.getLogger("aftr.auto_refresh")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _live_loop() -> None:
    sec = float(getattr(settings, "live_refresh_seconds", 60) or 60)
    logger.info(
        "AUTO REFRESH: LIVE loop started | interval=%.0fs | %s",
        sec,
        _utc_iso(),
    )
    while True:
        await asyncio.to_thread(run_live_refresh_job)
        await asyncio.sleep(sec)


async def _upcoming_loop() -> None:
    await asyncio.sleep(5.0)
    sec = float(getattr(settings, "upcoming_refresh_min", 15) or 15) * 60.0
    logger.info(
        "AUTO REFRESH: UPCOMING loop started | interval=%.0fs | %s",
        sec,
        _utc_iso(),
    )
    while True:
        await asyncio.to_thread(run_upcoming_refresh_job)
        await asyncio.sleep(sec)


async def _results_loop() -> None:
    await asyncio.sleep(12.0)
    sec = float(getattr(settings, "results_refresh_min", 10) or 10) * 60.0
    logger.info(
        "AUTO REFRESH: RESULTS loop started | interval=%.0fs | %s",
        sec,
        _utc_iso(),
    )
    while True:
        await asyncio.to_thread(run_results_refresh_job)
        await asyncio.sleep(sec)


def spawn_auto_refresh_tasks() -> list[asyncio.Task[None]]:
    """Tres tareas en paralelo; lifespan debe cancelarlas al apagar."""
    return [
        asyncio.create_task(_live_loop(), name="aftr-tier-live"),
        asyncio.create_task(_upcoming_loop(), name="aftr-tier-upcoming"),
        asyncio.create_task(_results_loop(), name="aftr-tier-results"),
    ]
