"""
Dependencias de la aplicación FastAPI (inyección de config y servicios).
"""
from __future__ import annotations

from config.settings import settings
from data.cache import read_json, write_json


def get_settings():
    return settings


def get_cache_read():
    return read_json


def get_cache_write():
    return write_json
