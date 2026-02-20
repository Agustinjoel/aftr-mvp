import json
import os

# engine/
BASE_DIR = os.path.dirname(os.path.dirname(__file__))

# Nueva carpeta cache
CACHE_DIR = os.path.join(BASE_DIR, "data", "cache")

# Fallback (por si algún día usás daily)
DAILY_DIR = os.path.join(BASE_DIR, "daily")


def read_json(filename: str):
    """
    Lee primero desde data/cache.
    Si no existe, intenta desde daily.
    Si no encuentra nada, devuelve [].
    """

    # 1️⃣ Buscar en cache
    cache_path = os.path.join(CACHE_DIR, filename)
    if os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)

    # 2️⃣ Buscar en daily (fallback)
    daily_path = os.path.join(DAILY_DIR, filename)
    if os.path.exists(daily_path):
        with open(daily_path, "r", encoding="utf-8") as f:
            return json.load(f)

    # 3️⃣ Si no existe
    return []


def write_json(filename: str, data):
    """
    Guarda SIEMPRE en data/cache.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)

    path = os.path.join(CACHE_DIR, filename)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)