"""
Acceso a cache JSON: todas las lecturas/escrituras usan config.settings CACHE_DIR (AFTR_CACHE_DIR).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from config.settings import CACHE_DIR, DAILY_DIR

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


def read_cache_meta() -> dict[str, Any]:
    """Lee cache_meta.json: last_updated (ISO str), refresh_running (bool)."""
    data = read_json(CACHE_META_FILENAME)
    if not isinstance(data, dict):
        return {"last_updated": None, "refresh_running": False}
    return {
        "last_updated": data.get("last_updated"),
        "refresh_running": bool(data.get("refresh_running")),
    }


def write_cache_meta(meta: dict[str, Any]) -> None:
    """Escribe cache_meta.json (last_updated, refresh_running)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / CACHE_META_FILENAME
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def write_json(filename: str, data: Any) -> None:
    """Guarda en CACHE_DIR (AFTR_CACHE_DIR)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
