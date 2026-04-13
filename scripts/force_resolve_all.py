"""
Refresh completo de todas las ligas en una sola pasada.

Útil para poblar la app desde cero o resolver picks históricos sin esperar
el ciclo normal de round-robin. Adquiere el lock de refresh para no interferir
con los jobs automáticos mientras corre.

Uso:
    python scripts/force_resolve_all.py                    # todas las ligas
    python scripts/force_resolve_all.py --leagues PL SA    # solo esas ligas
    python scripts/force_resolve_all.py --days-finished 7  # ventana de resultados
    python scripts/force_resolve_all.py --dry-run          # solo lista las ligas
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("aftr.force_resolve")


def _acquire_lock() -> bool:
    """Marca refresh_running=True para que los jobs automáticos esperen."""
    try:
        from data.cache import write_cache_meta, read_cache_meta
        meta = read_cache_meta()
        if meta.get("refresh_running"):
            logger.warning("Ya hay un refresh corriendo — esperando 15s y reintentando...")
            time.sleep(15)
            meta = read_cache_meta()
            if meta.get("refresh_running"):
                logger.error("El lock sigue ocupado. Abortando.")
                return False
        write_cache_meta({
            "refresh_running": True,
            "refresh_started_at": datetime.now(timezone.utc).isoformat(),
            "last_updated": meta.get("last_updated"),
        })
        logger.info("Lock adquirido.")
        return True
    except Exception as e:
        logger.error("No se pudo adquirir el lock: %s", e)
        return False


def _release_lock() -> None:
    try:
        from data.cache import release_refresh_running_meta
        release_refresh_running_meta()
        logger.info("Lock liberado.")
    except Exception as e:
        logger.error("No se pudo liberar el lock: %s", e)


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh completo de todas las ligas AFTR")
    parser.add_argument("--leagues", nargs="*", help="Ligas a procesar (ej: PL SA). Default: todas con APIF ID.")
    parser.add_argument("--days-finished", type=int, default=7, help="Días de partidos finalizados a traer (default: 7)")
    parser.add_argument("--days-upcoming", type=int, default=7, help="Días de próximos partidos (default: 7)")
    parser.add_argument("--delay", type=float, default=1.5, help="Segundos entre ligas para no saturar la API (default: 1.5)")
    parser.add_argument("--dry-run", action="store_true", help="Solo lista las ligas, sin llamar la API")
    parser.add_argument("--no-lock", action="store_true", help="No adquirir el lock (útil si el lock está colgado)")
    args = parser.parse_args()

    from config.settings import settings
    from services.refresh_apifootball import apif_refresh_league

    # Ligas con APIF ID configurado
    all_codes = [
        code for code in (settings.league_codes() or [])
        if settings.get_apif_league_id(code)
    ]

    if args.leagues:
        codes = [c.upper() for c in args.leagues if c.upper() in all_codes]
        missing = [c.upper() for c in args.leagues if c.upper() not in all_codes]
        if missing:
            logger.warning("Ligas sin APIF ID (se ignoran): %s", missing)
    else:
        codes = all_codes

    if not codes:
        logger.error("No hay ligas para procesar.")
        return

    logger.info("Ligas a procesar (%d): %s", len(codes), codes)
    logger.info("Ventana: upcoming=%dd | finished=%dd", args.days_upcoming, args.days_finished)

    if args.dry_run:
        logger.info("--dry-run: nada se ejecuta.")
        return

    # Adquirir lock
    if not args.no_lock:
        if not _acquire_lock():
            return

    ok = 0
    failed = []

    try:
        for i, code in enumerate(codes, 1):
            logger.info("[%d/%d] Procesando %s...", i, len(codes), code)
            try:
                n_upcoming, n_picks = apif_refresh_league(
                    code,
                    days_upcoming=args.days_upcoming,
                    days_finished=args.days_finished,
                    fetch_odds=False,
                )
                logger.info("  %s → upcoming=%d picks=%d", code, n_upcoming, n_picks)
                ok += 1
            except Exception as e:
                logger.error("  %s → ERROR: %s", code, e)
                failed.append(code)

            if i < len(codes):
                time.sleep(args.delay)

    finally:
        if not args.no_lock:
            _release_lock()

    # Resumen
    logger.info("=" * 50)
    logger.info("Completado: %d/%d ligas OK", ok, len(codes))
    if failed:
        logger.warning("Fallidas: %s", failed)
    else:
        logger.info("Sin errores.")


if __name__ == "__main__":
    main()
