"""
Acceso a cache JSON: lectura/escritura en data/cache con fallback a daily/.
Usa config.settings para rutas.
"""
from __future__ import annotations

import json
from typing import Any

# Import tardío para evitar ciclos; se resuelve en runtime
def _get_dirs():
    from config.settings import CACHE_DIR, DAILY_DIR
    return CACHE_DIR, DAILY_DIR


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


def write_json(filename: str, data: Any) -> None:
    """Guarda siempre en data/cache."""
    cache_dir, _ = _get_dirs()
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
