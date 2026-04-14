from __future__ import annotations

import time
import os
from datetime import datetime

from services.refresh_apifootball import apif_refresh_league
from services.refresh_basketball import refresh_league_basketball
from config.settings import settings


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def main() -> None:
    sleep_between = int(os.getenv("AFTR_REFRESH_SLEEP_BETWEEN", "5"))

    # si querés que haga loop infinito: AFTR_REFRESH_LOOP=1
    loop = (os.getenv("AFTR_REFRESH_LOOP", "0").strip() == "1")

    # si querés elegir ligas puntuales:
    # AFTR_REFRESH_LEAGUES="PL,PD,SA"
    leagues_env = os.getenv("AFTR_REFRESH_LEAGUES", "").strip()
    if leagues_env:
        leagues = [x.strip().upper() for x in leagues_env.split(",") if x.strip()]
    else:
        leagues = settings.league_codes()

    print(f"[{_now()}] Refresh spaced: {len(leagues)} ligas | sleep={sleep_between}s | loop={loop}")
    print("Ligas:", ", ".join(leagues))

    while True:
        for code in leagues:
            print(f"\n[{_now()}] === Refresh {code} ===")
            try:
                sport = settings.league_sport.get(code, "football")
                if sport == "basketball":
                    nm, np = refresh_league_basketball(code)
                else:
                    nm, np = apif_refresh_league(code)
                print(f"[{_now()}] OK {code}: {nm} matches | {np} picks")
            except Exception as e:
                print(f"[{_now()}] ERROR {code}: {e}")

            print(f"[{_now()}] Sleeping {sleep_between}s...")
            time.sleep(sleep_between)

        if not loop:
            print(f"\n[{_now()}] Fin (una vuelta).")
            break


if __name__ == "__main__":
    main()