"""
Acceso a cache JSON: lectura/escritura usando config.settings CACHE_DIR (env AFTR_CACHE_DIR si está definido),
con fallback de lectura a daily/. Incluye fallback a .prev y metadata de última actualización.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

CACHE_META_FILENAME = "cache_meta.json"

# Import tardío para evitar ciclos; se resuelve en runtime
def _get_dirs():
    from config.settings import CACHE_DIR, DAILY_DIR
    return CACHE_DIR, DAILY_DIR


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
    Lee primero desde data/cache; si no existe, desde daily/.
    Si no encuentra nada, devuelve [].
    """
    cache_dir, daily_dir = _get_dirs()
    cache_path = cache_dir / filename
    if cache_path.exists():
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)
    daily_path = daily_dir / filename
    if daily_path.exists():
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
    cache_dir, _ = _get_dirs()
    cache_dir.mkdir(parents=True, exist_ok=True)
    path_prev = cache_dir / (filename + ".prev")
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
    cache_dir, _ = _get_dirs()
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / CACHE_META_FILENAME
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def write_json(filename: str, data: Any) -> None:
    """Guarda siempre en data/cache."""
    cache_dir, _ = _get_dirs()
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
