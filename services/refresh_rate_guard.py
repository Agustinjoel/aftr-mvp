"""
Backoff compartido entre jobs de auto-refresh cuando Football-Data reporta rate limit bajo / sleeps.
Usa time.monotonic() para no depender del reloj de pared.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger("aftr.refresh_rate_guard")

_lock = threading.Lock()
_backoff_until_mono: float = 0.0


def register_rate_pressure_from_stats(stats: dict[str, Any] | None) -> float:
    """
    Tras un job con football_data_refresh_cycle, si hubo sleeps por rate limit,
    programa backoff. Devuelve segundos extra sugeridos para el próximo sleep del job.
    """
    global _backoff_until_mono
    if not stats:
        return 0.0
    try:
        slept = int(stats.get("rate_limit_sleep_sec") or 0)
    except (TypeError, ValueError):
        slept = 0
    if slept <= 0:
        return 0.0
    return float(slept)


def apply_backoff_seconds(seconds: float, cap: float) -> None:
    """Extiende el instante hasta el cual los jobs deben esperar."""
    global _backoff_until_mono
    try:
        sec = max(0.0, float(seconds))
        cap_f = max(0.0, float(cap))
    except (TypeError, ValueError):
        return
    extra = min(sec, cap_f) if cap_f > 0 else sec
    until = time.monotonic() + extra
    with _lock:
        if until > _backoff_until_mono:
            _backoff_until_mono = until
            logger.warning(
                "RATE LIMIT ALCANZADO - BACKOFF ACTIVADO | extra_s=%.0f | "
                "AUTO REFRESH RATE LIMIT LOW, backing off until_mono=%.0f",
                extra,
                _backoff_until_mono,
            )


def seconds_to_wait_for_backoff() -> float:
    """Segundos a dormir antes de ejecutar un job (0 si ya pasó el backoff)."""
    with _lock:
        now = time.monotonic()
        if now >= _backoff_until_mono:
            return 0.0
        return max(0.0, _backoff_until_mono - now)


def clear_backoff_for_tests() -> None:
    global _backoff_until_mono
    with _lock:
        _backoff_until_mono = 0.0
