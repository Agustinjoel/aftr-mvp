"""
Bootstrap histórico de todas las ligas AFTR con API-Football.

Hace un fetch de los últimos DAYS_FINISHED días de partidos finalizados
para cada liga configurada en APIF_LEAGUE_MAP.  Esto construye el caché
de team_form y picks para que el modelo Poisson tenga xG reales en vez
de usar los defaults.

Uso:
    python scripts/bootstrap_leagues.py                   # todas las ligas, 60 días
    python scripts/bootstrap_leagues.py --leagues PL PD   # solo esas ligas
    python scripts/bootstrap_leagues.py --days 90         # 90 días de historial
    python scripts/bootstrap_leagues.py --dry-run         # solo muestra qué haría
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

# ── path setup ───────────────────────────────────────────────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("aftr.bootstrap")


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap histórico de ligas AFTR")
    parser.add_argument("--leagues", nargs="*", help="Ligas a procesar (ej: PL PD ARG). Default: todas.")
    parser.add_argument("--days", type=int, default=60, help="Días de historial a traer (default: 60)")
    parser.add_argument("--dry-run", action="store_true", help="Solo muestra qué haría, sin llamar la API")
    parser.add_argument("--sleep", type=float, default=1.5, help="Segundos entre ligas para no exceder rate limit (default: 1.5)")
    args = parser.parse_args()

    from config.settings import settings
    from data.providers.api_football import _api_key

    if not _api_key():
        logger.error("API_FOOTBALL_KEY no está configurada. Abortando.")
        sys.exit(1)

    # Determine leagues to process
    all_leagues = [code for code in settings.leagues if settings.get_apif_league_id(code)]
    target = args.leagues if args.leagues else all_leagues

    invalid = [l for l in target if l not in settings.leagues]
    if invalid:
        logger.warning("Ligas desconocidas (se ignoran): %s", invalid)
        target = [l for l in target if l in settings.leagues]

    no_apif = [l for l in target if not settings.get_apif_league_id(l)]
    if no_apif:
        logger.warning("Sin APIF ID mapeado (se ignoran): %s", no_apif)
        target = [l for l in target if settings.get_apif_league_id(l)]

    logger.info("Bootstrap: %d ligas | %d días de historial", len(target), args.days)
    logger.info("Ligas: %s", ", ".join(target))

    if args.dry_run:
        logger.info("[DRY-RUN] No se llamará la API ni se escribirá nada.")
        for code in target:
            lid  = settings.get_apif_league_id(code)
            seas = settings.get_apif_season(code)
            logger.info("  %-6s → league_id=%s  season=%s  days_finished=%d", code, lid, seas, args.days)
        return

    from services.refresh_apifootball import apif_refresh_league

    results: dict[str, tuple[int, int]] = {}
    errors:  list[str] = []

    for i, code in enumerate(target, 1):
        logger.info("[%d/%d] %-6s — fetching %d days...", i, len(target), code, args.days)
        try:
            n_up, n_picks = apif_refresh_league(
                code,
                days_upcoming=7,
                days_finished=args.days,
            )
            results[code] = (n_up, n_picks)
            logger.info("  ✓ %s: upcoming=%d  picks=%d", code, n_up, n_picks)
        except Exception as exc:
            logger.error("  ✗ %s: %s", code, exc)
            errors.append(f"{code}: {exc}")

        if i < len(target):
            time.sleep(args.sleep)

    # ── Resumen ───────────────────────────────────────────────────────────────
    print("\n" + "="*50)
    print(f"Bootstrap completado: {len(results)}/{len(target)} ligas OK")
    if errors:
        print(f"Errores ({len(errors)}):")
        for e in errors:
            print(f"  ✗ {e}")
    print("="*50)
    for code, (n_up, n_picks) in results.items():
        print(f"  {code:<6} upcoming={n_up}  picks={n_picks}")


if __name__ == "__main__":
    main()
