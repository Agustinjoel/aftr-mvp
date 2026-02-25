"""
Configuración centralizada cargada desde variables de entorno.
Para producción: definir env vars o usar .env (python-dotenv).
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Raíz del proyecto (donde está engine/, app/, config/)
BASE_DIR: Path = Path(__file__).resolve().parents[1]

# Cache de datos (JSON por liga)
CACHE_DIR: Path = BASE_DIR / "data" / "cache"
# Fallback legacy
DAILY_DIR: Path = BASE_DIR / "daily"

# Base de datos SQLite (opcional; si existe se usa para stats y evaluación)
DB_PATH: str = os.getenv("AFTR_DB_PATH") or os.getenv("DB_PATH") or str(BASE_DIR / "aftr.db")

# API Football-Data.org
FOOTBALL_DATA_API_KEY: str = (os.getenv("FOOTBALL_DATA_API_KEY") or "").strip()

# Ligas soportadas (código -> nombre)
LEAGUES: dict[str, str] = {
    "BSA": "Campeonato Brasileiro Série A",
    "ELC": "Championship",
    "PL": "Premier League",
    "EC": "European Championship",
    "DED": "Eredivisie",
    "PPL": "Primeira Liga",
    "CLI": "Copa Libertadores",
    "WC": "FIFA World Cup",
    "PD": "LaLiga",
    "SA": "Serie A",
    "BL1": "Bundesliga",
    "FL1": "Ligue 1",
    "CL": "UEFA Champions League",
}
DEFAULT_LEAGUE: str = os.getenv("AFTR_DEFAULT_LEAGUE", "PL")

# Lógica de picks (Modelo A base)
MIN_PROB_FOR_CANDIDATE: float = float(os.getenv("AFTR_MIN_PROB", "0.50"))
DEFAULT_XG_HOME: float = float(os.getenv("AFTR_DEFAULT_XG_HOME", "1.45"))
DEFAULT_XG_AWAY: float = float(os.getenv("AFTR_DEFAULT_XG_AWAY", "1.15"))
MAX_GOALS_POISSON: int = int(os.getenv("AFTR_MAX_GOALS_POISSON", "8"))

# ✅ Selector de modelo de picks
# "A" = xG fijo / modelo básico
# "B" = modelo dinámico (split + recencia)
PICKS_MODEL: str = (os.getenv("AFTR_PICKS_MODEL", "B") or "B").strip().upper()

# ✅ Parámetros del modelo B (forma)
TEAM_FORM_DAYS_BACK: int = int(os.getenv("AFTR_TEAM_FORM_DAYS_BACK", "30"))
TEAM_FORM_LIMIT: int = int(os.getenv("AFTR_TEAM_FORM_LIMIT", "10"))

# ✅ Para no reventar rate limits: aplicar modelo B solo a los primeros N partidos por liga
# (los demás quedan con A por fallback)
REFRESH_TOPN_MODEL_B: int = int(os.getenv("AFTR_REFRESH_TOPN_MODEL_B", "20"))

# App
DEBUG: bool = os.getenv("AFTR_DEBUG", "").lower() in ("1", "true", "yes")
LOG_LEVEL: str = os.getenv("AFTR_LOG_LEVEL", "INFO")


class Settings:
    """Objeto de configuración accesible en toda la app."""

    def __init__(self) -> None:
        self.base_dir = BASE_DIR
        self.cache_dir = CACHE_DIR
        self.daily_dir = DAILY_DIR
        self.db_path = DB_PATH
        self.football_data_api_key = FOOTBALL_DATA_API_KEY

        self.leagues = LEAGUES
        self.default_league = DEFAULT_LEAGUE

        self.min_prob_for_candidate = MIN_PROB_FOR_CANDIDATE
        self.default_xg_home = DEFAULT_XG_HOME
        self.default_xg_away = DEFAULT_XG_AWAY
        self.max_goals_poisson = MAX_GOALS_POISSON

        # ✅ nuevos settings
        self.picks_model = PICKS_MODEL
        self.team_form_days_back = TEAM_FORM_DAYS_BACK
        self.team_form_limit = TEAM_FORM_LIMIT
        self.refresh_topn_model_b = REFRESH_TOPN_MODEL_B

        self.debug = DEBUG
        self.log_level = LOG_LEVEL

    def league_codes(self) -> list[str]:
        return list(self.leagues.keys())

    def is_valid_league(self, code: str) -> bool:
        return code in self.leagues

    def use_model_b(self) -> bool:
        return (self.picks_model or "B").upper() == "B"


settings = Settings()
