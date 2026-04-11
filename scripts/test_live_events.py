"""
Script de prueba para process_cache_live_events.

Toma el primer partido TIMED de cualquier liga, lo guarda en .prev como TIMED,
actualiza el .json principal a IN_PLAY, llama a la función, y restaura todo.

Uso:
  python scripts/test_live_events.py

Requiere que estés logueado en la DB (usa DATABASE_URL del .env).
"""
import json
import pathlib
import shutil
import sys
import os

# Asegurar que el root del proyecto esté en el path
ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Cargar .env si existe
env_file = ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

CACHE_DIR = ROOT / "data" / "cache"


def find_timed_match() -> tuple[str, dict] | None:
    """Busca el primer partido TIMED en cualquier liga."""
    for f in sorted(CACHE_DIR.glob("daily_matches_*.json")):
        if ".prev" in f.name:
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, list):
            continue
        for m in data:
            if isinstance(m, dict) and m.get("status") == "TIMED":
                code = f.stem.replace("daily_matches_", "")
                return code, m
    return None


def main():
    result = find_timed_match()
    if not result:
        print("No hay partidos TIMED en el caché. Proba durante un dia con partidos programados.")
        sys.exit(1)

    code, match = result
    mid = match.get("match_id")
    home = match.get("home") or match.get("home_team")
    away = match.get("away") or match.get("away_team")
    print(f"Partido encontrado: {home} vs {away} (mid={mid}, liga={code})")

    matches_file = CACHE_DIR / f"daily_matches_{code}.json"
    prev_file    = CACHE_DIR / f"daily_matches_{code}.json.prev"

    # Backup originals
    orig_curr = matches_file.read_bytes()
    orig_prev = prev_file.read_bytes() if prev_file.exists() else None

    try:
        all_matches = json.loads(orig_curr)

        # .prev = estado anterior (TIMED, sin score)
        prev_version = json.loads(orig_curr)  # copia
        prev_file.write_text(json.dumps(prev_version, ensure_ascii=False, indent=2), encoding="utf-8")

        # .json actual = partido "en vivo" (IN_PLAY, score 0-0)
        for m in all_matches:
            if isinstance(m, dict) and m.get("match_id") == mid:
                m["status"] = "IN_PLAY"
                m["score"]  = {"home": 0, "away": 0}
                break
        matches_file.write_text(json.dumps(all_matches, ensure_ascii=False, indent=2), encoding="utf-8")

        print(f"\nEstado simulado: {home} vs {away} ahora figura como IN_PLAY")
        print("Llamando a process_cache_live_events...\n")

        from services.live_events import process_cache_live_events
        n = process_cache_live_events([code])
        print(f"\nResultado: {n} notificacion(es) enviada(s)")

        if n == 0:
            print("\nPor qué puede ser 0:")
            print("  1. No tenés ningún usuario siguiendo picks de este partido")
            print("  2. No tenés tracker bets para este partido")
            print("  3. No hay push subscriptions registradas en la DB")
            print("  4. Problema con VAPID keys")
            print("\nVerificá: https://tu-app.onrender.com/user/push/debug")

    finally:
        # Restaurar archivos originales
        matches_file.write_bytes(orig_curr)
        if orig_prev is not None:
            prev_file.write_bytes(orig_prev)
        elif prev_file.exists():
            prev_file.unlink()
        print("\nArchivos de caché restaurados.")


if __name__ == "__main__":
    main()
