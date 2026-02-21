"""
Punto de entrada CLI para tareas de producción (refresco, etc.).
Uso: python -m app.cli refresh
"""
from __future__ import annotations

import logging
import sys

from config.settings import settings
from services.refresh import refresh_all

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def cmd_refresh() -> int:
    refresh_all()
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print("Uso: python -m app.cli <comando>")
        print("  refresh  - Actualizar partidos y picks para todas las ligas")
        return 1
    cmd = sys.argv[1].lower()
    if cmd == "refresh":
        return cmd_refresh()
    print(f"Comando desconocido: {cmd}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
