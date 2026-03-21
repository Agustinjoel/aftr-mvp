"""
Acceso a cache JSON: todas las lecturas/escrituras usan config.settings CACHE_DIR (AFTR_CACHE_DIR).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from config.settings import CACHE_DIR, DAILY_DIR, settings

CACHE_META_FILENAME = "cache_meta.json"
_logger = logging.getLogger("aftr.cache")

# Debug: resolved CACHE_DIR at import (same as web/refresh process)
_logger.info("CACHE_DIR resolved: %s", str(CACHE_DIR))


def _is_valid_cache_data(data: Any, filename: str) -> bool:
    """True si los datos son válidos para mostrar (no vacíos para picks/matches)."""
    if data is None:
        return False
    if "daily_picks_" in filename or "daily_matches_" in filename:
        return isinstance(data, list) and len(data) > 0
    if isinstance(data, list):
        return True
    if isinstance(data, dict):
        return True
    return False


def read_json(filename: str) -> list[Any] | dict[str, Any]:
    """Lee desde CACHE_DIR; si no existe, desde DAILY_DIR (fallback solo lectura). Si no encuentra nada, []."""
    path = CACHE_DIR / filename
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        n = len(data) if isinstance(data, list) else (len(data) if isinstance(data, dict) else 0)
        _logger.info("read_json: %s -> %s items (from %s)", filename, n, str(path))
        return data
    daily_path = DAILY_DIR / filename
    if daily_path.exists():
        with open(daily_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        n = len(data) if isinstance(data, list) else (len(data) if isinstance(data, dict) else 0)
        _logger.info("read_json: %s -> %s items (from daily fallback %s)", filename, n, str(daily_path))
        return data
    _logger.info("read_json: %s -> not found (checked %s and %s)", filename, str(path), str(daily_path))
    return []


def read_json_with_fallback(filename: str) -> list[Any] | dict[str, Any]:
    """
    Si hay cache fresco (válido), lo usa; si no, intenta cache .prev; si no, vacío.
    Para daily_picks_*.json y daily_matches_*.json devuelve []; para otros {}.
    """
    data = read_json(filename)
    if _is_valid_cache_data(data, filename):
        n = len(data) if isinstance(data, list) else (len(data) if isinstance(data, dict) else 0)
        _logger.info("read_json_with_fallback: %s -> primary valid, %s items", filename, n)
        return data
    prev_name = filename + ".prev"
    data_prev = read_json(prev_name)
    if _is_valid_cache_data(data_prev, filename):
        n = len(data_prev) if isinstance(data_prev, list) else (len(data_prev) if isinstance(data_prev, dict) else 0)
        _logger.info("read_json_with_fallback: %s -> .prev valid, %s items", filename, n)
        return data_prev
    _logger.info("read_json_with_fallback: %s -> empty (primary and .prev invalid or missing)", filename)
    if "daily_picks_" in filename or "daily_matches_" in filename:
        return []
    return {}


def backup_current_to_prev(filename: str) -> None:
    """Copia el contenido actual del archivo a filename.prev si es válido (para fallback durante refresh)."""
    data = read_json(filename)
    if not _is_valid_cache_data(data, filename):
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path_prev = CACHE_DIR / (filename + ".prev")
    with open(path_prev, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _seconds_since_utc_iso(iso: str | None) -> float | None:
    """Segundos desde un instante ISO (UTC); None si no parseable."""
    if not iso or not isinstance(iso, str):
        return None
    try:
        s = iso.strip()
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return None


def read_cache_meta() -> dict[str, Any]:
    """
    Lee cache_meta.json: last_updated, refresh_running, refresh_started_at.
    Si refresh_running lleva más de REFRESH_RUNNING_TTL_SEC sin cerrarse (p. ej. proceso muerto),
    libera el flag y persiste meta corregida.
    """
    data = read_json(CACHE_META_FILENAME)
    if not isinstance(data, dict):
        return {"last_updated": None, "refresh_running": False, "refresh_started_at": None}

    ttl = int(getattr(settings, "refresh_running_ttl_sec", 600) or 0)
    running = bool(data.get("refresh_running"))

    if running and ttl > 0:
        started = data.get("refresh_started_at") or data.get("last_updated")
        age = _seconds_since_utc_iso(started if isinstance(started, str) else None)
        if age is None or age > float(ttl):
            now_iso = datetime.now(timezone.utc).isoformat()
            _logger.warning(
                "refresh_running en caché expirado o sin marca de tiempo válida "
                "(age=%s s, ttl=%s s) — liberando flag | %s",
                age,
                ttl,
                now_iso,
            )
            data = {
                **data,
                "refresh_running": False,
                "refresh_started_at": None,
                "last_updated": data.get("last_updated") or now_iso,
            }
            try:
                write_cache_meta(data)
            except Exception as e:
                _logger.error("No se pudo persistir meta tras liberar refresh expirado: %s", e)

    return {
        "last_updated": data.get("last_updated"),
        "refresh_running": bool(data.get("refresh_running")),
        "refresh_started_at": data.get("refresh_started_at"),
    }


def write_cache_meta(meta: dict[str, Any]) -> None:
    """Escribe cache_meta.json (last_updated, refresh_running, refresh_started_at, …)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / CACHE_META_FILENAME
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def release_refresh_running_meta() -> None:
    """
    Fuerza refresh_running=False en caché. Llamar siempre en finally del refresco.
    Nunca debe fallar en silencio: reintenta y loguea CRITICAL si hace falta.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        raw = read_json(CACHE_META_FILENAME)
        base = dict(raw) if isinstance(raw, dict) else {}
        base["refresh_running"] = False
        base["refresh_started_at"] = None
        base["last_updated"] = now_iso
        write_cache_meta(base)
        _logger.info(
            "AUTO REFRESH END (lock liberado en caché) | refresh_running=false | %s",
            now_iso,
        )
    except Exception as e:
        _logger.critical(
            "CRITICAL: no se pudo liberar refresh_running en caché: %s",
            e,
            exc_info=True,
        )
        try:
            write_cache_meta(
                {
                    "refresh_running": False,
                    "refresh_started_at": None,
                    "last_updated": now_iso,
                }
            )
        except Exception as e2:
            _logger.critical("CRITICAL: reintento fallido al liberar refresh_running: %s", e2)


def write_json(filename: str, data: Any) -> None:
    """Guarda en CACHE_DIR (AFTR_CACHE_DIR)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
