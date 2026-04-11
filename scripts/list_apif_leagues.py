#!/usr/bin/env python3
"""
Lista las ligas disponibles en API-Football para verificar/corregir los IDs.

Uso:
    python scripts/list_apif_leagues.py
    python scripts/list_apif_leagues.py --country "Argentina"
    python scripts/list_apif_leagues.py --search "Copa"
    python scripts/list_apif_leagues.py --verify   # Verifica los IDs configurados

Requiere la variable de entorno API_FOOTBALL_KEY configurada.
"""
from __future__ import annotations

import argparse
import os
import sys

# Asegurar que el root del proyecto esté en el path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> None:
    parser = argparse.ArgumentParser(description="Lista ligas de API-Football")
    parser.add_argument("--country", default="", help="Filtrar por país (ej: Argentina)")
    parser.add_argument("--search",  default="", help="Filtrar por nombre (ej: Copa)")
    parser.add_argument("--verify",  action="store_true", help="Verificar los IDs configurados en settings")
    args = parser.parse_args()

    from data.providers.api_football import list_leagues, _api_key

    if not _api_key():
        print("ERROR: API_FOOTBALL_KEY no configurada.")
        sys.exit(1)

    if args.verify:
        from config.settings import settings
        print("\n=== Verificación de IDs configurados en APIF_LEAGUE_MAP ===\n")
        print(f"{'Código AFTR':<12} {'ID configurado':<16} {'Nombre esperado':<40}")
        print("-" * 70)
        for code, configured_id in settings.apif_league_map.items():
            leagues = list_leagues()
            match = next((lg for lg in leagues if lg.get("id") == configured_id), None)
            if match:
                status = "✅"
                found_name = match.get("name", "?")
            else:
                status = "❌ ID NO ENCONTRADO"
                found_name = "—"
            print(f"{code:<12} {configured_id:<16} {found_name:<40} {status}")
        return

    leagues = list_leagues(search=args.search, country=args.country)
    if not leagues:
        print("Sin resultados.")
        return

    print(f"\n{'ID':<8} {'Nombre':<45} {'País':<25} {'Tipo':<12}")
    print("-" * 90)
    for lg in sorted(leagues, key=lambda x: (x.get("country") or "", x.get("id") or 0)):
        print(
            f"{lg.get('id', '?'):<8} "
            f"{(lg.get('name') or '?')[:44]:<45} "
            f"{(lg.get('country') or '?')[:24]:<25} "
            f"{(lg.get('type') or '?')[:11]:<12}"
        )
    print(f"\nTotal: {len(leagues)} ligas")


if __name__ == "__main__":
    main()
