"""
Acceso a cache JSON: una sola carpeta de cache desde config.settings CACHE_DIR (env AFTR_CACHE_DIR).
Todas las lecturas y escrituras usan get_cache_dir(). Fallback de lectura a daily/ solo si no existe en cache.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

CACHE_META_FILENAME = "cache_meta.json"
_logger = logging.getLogger("aftr.cache")

_cache_dir_value: Path | None = None


def get_cache_dir() -> Path:
    """Única resolución del directorio de cache (AFTR_CACHE_DIR vía settings). Crea el dir si no existe."""
    global _cache_dir_value
    if _cache_dir_value is None:
        from config.settings import CACHE_DIR
        _cache_dir_value = CACHE_DIR.resolve() if hasattr(CACHE_DIR, "resolve") else Path(CACHE_DIR)
        os.makedirs(str(_cache_dir_value), exist_ok=True)
        _logger.info("cache_dir resolved: %s", str(_cache_dir_value))
    return _cache_dir_value


def _get_dirs():
    """Cache dir (único) y daily dir (solo lectura fallback)."""
    from config.settings import DAILY_DIR
    return get_cache_dir(), DAILY_DIR


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
    """
    Lee primero desde cache_dir (AFTR_CACHE_DIR); si no existe, desde daily/ (fallback solo lectura).
    Si no encuentra nada, devuelve [].
    """
    cache_dir, daily_dir = _get_dirs()
    cache_path = cache_dir / filename
    if cache_path.exists():
        _logger.info("cache read: %s", str(cache_path))
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)
    daily_path = daily_dir / filename
    if daily_path.exists():
        _logger.info("cache read (daily fallback): %s", str(daily_path))
        with open(daily_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def read_json_with_fallback(filename: str) -> list[Any] | dict[str, Any]:
    """
    Si hay cache fresco (válido), lo usa; si no, intenta cache .prev; si no, vacío.
    Para daily_picks_*.json y daily_matches_*.json devuelve []; para otros {}.
    """
    data = read_json(filename)
    if _is_valid_cache_data(data, filename):
        return data
    prev_name = filename + ".prev"
    data_prev = read_json(prev_name)
    if _is_valid_cache_data(data_prev, filename):
        return data_prev
    if "daily_picks_" in filename or "daily_matches_" in filename:
        return []
    return {}


def backup_current_to_prev(filename: str) -> None:
    """Copia el contenido actual del archivo a filename.prev si es válido (para fallback durante refresh)."""
    data = read_json(filename)
    if not _is_valid_cache_data(data, filename):
        return
    cache_dir = get_cache_dir()
    os.makedirs(str(cache_dir), exist_ok=True)
    path_prev = cache_dir / (filename + ".prev")
    _logger.info("cache write: %s", str(path_prev))
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
    cache_dir = get_cache_dir()
    os.makedirs(str(cache_dir), exist_ok=True)
    path = cache_dir / CACHE_META_FILENAME
    _logger.info("cache write: %s", str(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def write_json(filename: str, data: Any) -> None:
    """Guarda siempre en cache_dir (AFTR_CACHE_DIR)."""
    cache_dir = get_cache_dir()
    os.makedirs(str(cache_dir), exist_ok=True)
    path = cache_dir / filename
    _logger.info("cache write: %s", str(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
