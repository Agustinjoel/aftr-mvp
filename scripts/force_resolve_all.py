"""
Refresh completo de todas las ligas en una sola pasada.

Útil para poblar la app desde cero o resolver picks históricos sin esperar
el ciclo normal de round-robin. Adquiere el lock de refresh para no interferir
con los jobs automáticos mientras corre.

Uso:
    python scripts/force_resolve_all.py                    # refresh de todas las ligas
    python scripts/force_resolve_all.py --fix-history      # repara PUSH en picks_history_*.json
    python scripts/force_resolve_all.py --leagues PL SA    # solo esas ligas
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


def fix_history(codes: list[str]) -> None:
    """
    Repara PUSH incorrectos en picks_history_*.json directamente.

    Para cada pick con result=PUSH:
    1. Si tiene score_home/score_away y best_market → re-evalúa
    2. Si tiene score pero no best_market → re-deriva market desde probs
    3. Si no tiene score → deja como está (partido sin resultado aún)
    """
    from data.cache import read_json, write_json
    from core.evaluation import evaluate_market
    from core.poisson import build_candidates, select_best_candidate

    for code in codes:
        hist_file = f"picks_history_{code}.json"
        history = read_json(hist_file)
        if not isinstance(history, list) or not history:
            continue

        fixed = 0
        for p in history:
            if not isinstance(p, dict):
                continue
            result = (p.get("result") or "").strip().upper()
            if result != "PUSH":
                continue

            hg = p.get("score_home")
            ag = p.get("score_away")
            if hg is None or ag is None:
                continue  # partido sin score — no se puede evaluar

            market = (p.get("best_market") or "").strip()

            # Sin market → intentar recuperar desde probs
            if not market:
                stored_probs = p.get("probs")
                if isinstance(stored_probs, dict) and stored_probs:
                    try:
                        cands = build_candidates(stored_probs, min_prob=0.0)
                        best = select_best_candidate(cands)
                        if best and best.get("market"):
                            market = best["market"]
                            p["best_market"] = market
                    except Exception:
                        pass

            if not market:
                continue

            try:
                new_result, _ = evaluate_market(market, int(hg), int(ag))
            except Exception:
                continue

            if new_result != "PUSH":
                p["result"] = new_result
                fixed += 1

        if fixed > 0:
            write_json(hist_file, history)
            logger.info("  %s history: %d picks corregidos de PUSH → WIN/LOSS", code, fixed)
        else:
            logger.info("  %s history: sin cambios", code)


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh completo de todas las ligas AFTR")
    parser.add_argument("--leagues", nargs="*", help="Ligas a procesar (ej: PL SA). Default: todas con APIF ID.")
    parser.add_argument("--days-finished", type=int, default=7, help="Días de partidos finalizados a traer (default: 7)")
    parser.add_argument("--days-upcoming", type=int, default=7, help="Días de próximos partidos (default: 7)")
    parser.add_argument("--delay", type=float, default=1.5, help="Segundos entre ligas (default: 1.5)")
    parser.add_argument("--fix-history", action="store_true", help="Repara PUSH incorrectos en picks_history_*.json")
    parser.add_argument("--dry-run", action="store_true", help="Solo lista las ligas, sin ejecutar")
    parser.add_argument("--no-lock", action="store_true", help="No adquirir el lock")
    args = parser.parse_args()

    from config.settings import settings
    from services.refresh_apifootball import apif_refresh_league

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

    if args.dry_run:
        logger.info("--dry-run: nada se ejecuta.")
        return

    if not args.no_lock:
        if not _acquire_lock():
            return

    ok = 0
    failed = []

    try:
        # 1) Reparar historial primero (no necesita API)
        if args.fix_history:
            logger.info("=== Reparando picks_history_*.json ===")
            fix_history(codes)

        # 2) Refresh completo de cada liga
        logger.info("=== Refresh de picks activos ===")
        logger.info("Ventana: upcoming=%dd | finished=%dd", args.days_upcoming, args.days_finished)
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

    logger.info("=" * 50)
    logger.info("Completado: %d/%d ligas OK", ok, len(codes))
    if failed:
        logger.warning("Fallidas: %s", failed)
    else:
        logger.info("Sin errores.")


if __name__ == "__main__":
    main()
