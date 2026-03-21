"""
Background periodic refresh: light `refresh_all(light=True)` on a repeating asyncio loop.

Logs every cycle with AUTO REFRESH STARTED / FINISHED and explicit UTC timestamps.
Started from FastAPI lifespan when AUTO_REFRESH=true.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from config.settings import settings
from data.cache import read_cache_meta
from services.refresh import refresh_all

logger = logging.getLogger("aftr.auto_refresh")


def _utc_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _auto_refresh_loop(interval_sec: float) -> None:
    """
    Infinite loop: one tick per iteration, then sleep `interval_sec` (+ optional rate-limit extra).
    Each iteration logs AUTO REFRESH STARTED / FINISHED with timestamps.
    """
    logger.info(
        "AUTO REFRESH: scheduler loop active | interval=%.0fs | %s",
        interval_sec,
        _utc_ts(),
    )
    while True:
        logger.info("AUTO REFRESH STARTED | %s", _utc_ts())
        extra = 0
        meta = read_cache_meta()
        if meta.get("refresh_running"):
            logger.info(
                "AUTO REFRESH: tick skipped (refresh_running in cache) | %s",
                _utc_ts(),
            )
        else:
            try:
                res = await asyncio.to_thread(
                    lambda: refresh_all(non_blocking=True, light=True)
                )
                if res.skipped_busy:
                    logger.info(
                        "AUTO REFRESH: tick skipped (refresh lock busy) | %s",
                        _utc_ts(),
                    )
                else:
                    logger.info(
                        "AUTO REFRESH: tick OK | leagues_refreshed=%d skipped_fresh=%d "
                        "football_http=%d matches_updated=%d rate_limit_sleep_s=%d | %s",
                        res.leagues_refreshed,
                        res.leagues_skipped_fresh,
                        res.football_http_requests,
                        res.matches_updated,
                        res.rate_limit_sleep_sec,
                        _utc_ts(),
                    )
                    cap = int(getattr(settings, "rate_limit_cooldown_cap_sec", 600) or 600)
                    if res.rate_limit_sleep_sec and cap > 0:
                        extra = min(cap, int(res.rate_limit_sleep_sec))
                        if extra:
                            logger.info(
                                "AUTO REFRESH: rate-limit cooldown +%ds | %s",
                                extra,
                                _utc_ts(),
                            )
            except Exception:
                logger.exception("AUTO REFRESH: tick failed | %s", _utc_ts())

        sleep_next = interval_sec + extra
        logger.info(
            "AUTO REFRESH FINISHED | next_sleep_s=%.0f | %s",
            sleep_next,
            _utc_ts(),
        )
        await asyncio.sleep(sleep_next)


def spawn_auto_refresh_task(interval_sec: float) -> asyncio.Task[None]:
    """Start the background loop; caller owns cancellation on shutdown."""
    return asyncio.create_task(
        _auto_refresh_loop(interval_sec),
        name="aftr-auto-refresh",
    )
