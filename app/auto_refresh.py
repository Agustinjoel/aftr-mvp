"""
Background periodic refresh: same pipeline as `python -m app.cli refresh` (services.refresh.refresh_all).
Does not block the event loop; uses asyncio.to_thread + a lock to avoid overlapping runs.
"""
from __future__ import annotations

import asyncio
import logging
import threading

from data.cache import read_cache_meta
from services.refresh import refresh_all

logger = logging.getLogger("aftr.auto_refresh")

# Serialize auto-refresh ticks (non-blocking try-acquire).
_refresh_lock = threading.Lock()


async def _auto_refresh_loop(interval_sec: float) -> None:
    """
    Run refresh_all every `interval_sec` seconds after each cycle completes.
    First tick runs shortly after app startup (no initial sleep).
    """
    while True:
        if not _refresh_lock.acquire(blocking=False):
            logger.info("auto-refresh: skipped because already running")
            await asyncio.sleep(interval_sec)
            continue
        try:
            meta = read_cache_meta()
            if meta.get("refresh_running"):
                logger.info("auto-refresh: skipped because already running")
            else:
                logger.info("auto-refresh: started")
                try:
                    await asyncio.to_thread(refresh_all)
                    logger.info("auto-refresh: finished")
                except Exception:
                    logger.exception("auto-refresh: failed with error")
        finally:
            _refresh_lock.release()
        await asyncio.sleep(interval_sec)


def spawn_auto_refresh_task(interval_sec: float) -> asyncio.Task[None]:
    """Start the background loop; caller owns cancellation on shutdown."""
    return asyncio.create_task(
        _auto_refresh_loop(interval_sec),
        name="aftr-auto-refresh",
    )
