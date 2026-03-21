"""
Background periodic refresh: light `refresh_all(light=True)` — fewer leagues per cycle,
shorter finished window, optional odds skip, Football-Data in-cycle cache + stats.

Uses `refresh_all(non_blocking=True)` so CLI/manual full refresh is not blocked waiting;
if a refresh is already in progress, the tick is skipped.
"""
from __future__ import annotations

import asyncio
import logging

from config.settings import settings
from data.cache import read_cache_meta
from services.refresh import refresh_all

logger = logging.getLogger("aftr.auto_refresh")


async def _auto_refresh_loop(interval_sec: float) -> None:
    """
    Run a light refresh every `interval_sec` seconds after each cycle completes.
    Adds extra delay after a cycle that hit Football-Data rate-limit sleeps (capped).
    """
    while True:
        meta = read_cache_meta()
        if meta.get("refresh_running"):
            logger.info("auto-refresh: skipped because already running")
            await asyncio.sleep(interval_sec)
            continue

        try:
            res = await asyncio.to_thread(
                lambda: refresh_all(non_blocking=True, light=True)
            )
            if res.skipped_busy:
                logger.info("auto-refresh: skipped because already running")
                extra = 0
            else:
                extra = 0
                cap = int(getattr(settings, "rate_limit_cooldown_cap_sec", 600) or 600)
                if res.rate_limit_sleep_sec and cap > 0:
                    extra = min(cap, int(res.rate_limit_sleep_sec))
                    if extra:
                        logger.info(
                            "auto-refresh: rate-limit cooldown +%ds before next interval",
                            extra,
                        )
        except Exception:
            logger.exception("auto-refresh: failed with error")
            extra = 0

        await asyncio.sleep(interval_sec + extra)


def spawn_auto_refresh_task(interval_sec: float) -> asyncio.Task[None]:
    """Start the background loop; caller owns cancellation on shutdown."""
    return asyncio.create_task(
        _auto_refresh_loop(interval_sec),
        name="aftr-auto-refresh",
    )
