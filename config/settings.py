"""
Configuración centralizada cargada desde variables de entorno.
Para producción: definir env vars o usar .env (python-dotenv).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
_logger = logging.getLogger("aftr.settings")

#Seguridad / SaaS
SECRET_KEY: str = (os.getenv("AFTR_SECRET_KEY", "dev-secret-change-me") or "").strip()

#Tiers (string)
PLAN_FREE: str = "FREE"
PLAN_PREMIUM: str = "PREMIUM"
PLAN_PRO: str = "PRO"
DEFAULT_PLAN: str = os.getenv("AFTR_DEFAULT_PLAN", PLAN_FREE)

#Precios (solo UI por ahora)
PRICE_PREMIUM_USD: str = os.getenv("AFTR_PRICE_PREMIUM_USD", "9.99")
PRICE_PRO_USD: str = os.getenv("AFTR_PRICE_PRO_USD", "19.99")

# Raíz del proyecto (donde está engine/, app/, config/)
BASE_DIR: Path = Path(__file__).resolve().parents[1]

# Cache: AFTR_CACHE_DIR (ej. /var/data/cache en Render); fallback "data/cache" local
CACHE_DIR: Path = Path(os.getenv("AFTR_CACHE_DIR", "data/cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)
print("CACHE_DIR:", CACHE_DIR)

# Fallback legacy (solo lectura; no escribir aquí)
DAILY_DIR: Path = BASE_DIR / "daily"

# Base de datos SQLite. Siempre usa AFTR_DB_PATH si está definido y no vacío/"None"; si no, path local.
_aftr_db_env: str = (os.getenv("AFTR_DB_PATH") or "").strip()
DB_PATH: str = (
    _aftr_db_env
    if _aftr_db_env and _aftr_db_env.upper() != "NONE"
    else str(BASE_DIR / "aftr.db")
)

# App base URL (para links absolutos en emails / Stripe)
APP_BASE_URL: str = (os.getenv("APP_BASE_URL") or "").strip().rstrip("/")

# SMTP (password recovery, etc.)
SMTP_SERVER: str = (os.getenv("SMTP_SERVER") or "").strip()
SMTP_PORT: int = int(os.getenv("SMTP_PORT") or "0") or 0
SMTP_USER: str = (os.getenv("SMTP_USER") or "").strip()
SMTP_PASSWORD: str = (os.getenv("SMTP_PASSWORD") or "").strip()
EMAIL_FROM: str = (os.getenv("EMAIL_FROM") or "").strip()

# API Football-Data.org
FOOTBALL_DATA_API_KEY: str = (os.getenv("FOOTBALL_DATA_API_KEY") or "").strip()

# API-Sports (Basketball, etc.)
API_SPORTS_KEY: str = (os.getenv("API_SPORTS_KEY") or os.getenv("APISPORTS_KEY") or "").strip()
# Optional override for NBA season (YYYY-YYYY). If set, no automatic fallback to previous season.
NBA_SEASON: str = (os.getenv("NBA_SEASON") or os.getenv("AFTR_NBA_SEASON") or "").strip()
# Optional NBA date window for filtering (YYYY-MM-DD). When both set, used instead of system date in get_upcoming_games/get_finished_games.
NBA_DATE_FROM: str = (os.getenv("NBA_DATE_FROM") or os.getenv("AFTR_NBA_DATE_FROM") or "").strip()
NBA_DATE_TO: str = (os.getenv("NBA_DATE_TO") or os.getenv("AFTR_NBA_DATE_TO") or "").strip()

# Odds (The Odds API) — football only for now; extensible for NBA later. Set in .env as ODDS_API_KEY.
ODDS_API_KEY: str = (os.getenv("ODDS_API_KEY") or os.getenv("THE_ODDS_API_KEY") or "").strip()
ODDS_API_BASE: str = (os.getenv("ODDS_API_BASE") or "https://api.the-odds-api.com").strip().rstrip("/")
# AFTR league_code -> The Odds API sport_key (soccer_*). Leagues not in map skip odds.
ODDS_LEAGUE_SPORT_KEYS: dict[str, str] = {
    "PL": "soccer_epl",
    "PD": "soccer_spain_la_liga",
    "BL1": "soccer_germany_bundesliga",
    "SA": "soccer_italy_serie_a",
    "FL1": "soccer_france_ligue_one",
    "ELC": "soccer_england_championship",
    "DED": "soccer_netherlands_eredivisie",
    "PPL": "soccer_portugal_primeira_liga",
    "CL": "soccer_uefa_champs_league",
    "EL": "soccer_uefa_europa_league",
    "BSA": "soccer_brazil_campeonato",
    "EC": "soccer_uefa_european_championship",
    "WC": "soccer_fifa_world_cup",
}

# Stripe (suscripciones)
STRIPE_SECRET_KEY: str = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
STRIPE_PUBLISHABLE_KEY: str = (os.getenv("STRIPE_PUBLISHABLE_KEY") or "").strip()
STRIPE_PRICE_ID: str = (os.getenv("STRIPE_PRICE_ID") or "").strip()
STRIPE_WEBHOOK_SECRET: str = (os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()

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
    "EL": "UEFA Europa League",
    "NBA": "NBA",
}
# Liga -> deporte para el pipeline (football usa Football-Data; basketball usa API-Sports)
LEAGUE_SPORT: dict[str, str] = {
    "NBA": "basketball",
}
FREE_LEAGUES: list[str] = ["PL", "PD", "SA", "NBA"]
DEFAULT_LEAGUE: str = (os.getenv("AFTR_DEFAULT_LEAGUE", "PL") or "PL").strip()

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
REFRESH_TOPN_MODEL_B: int = int(os.getenv("AFTR_REFRESH_TOPN_MODEL_B", "20"))

# App
DEBUG: bool = os.getenv("AFTR_DEBUG", "").lower() in ("1", "true", "yes")
LOG_LEVEL: str = os.getenv("AFTR_LOG_LEVEL", "INFO")


def _env_bool(name: str, default: bool = False) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


# Background refresh (same as `python -m app.cli refresh` → services.refresh.refresh_all)
AUTO_REFRESH: bool = _env_bool("AUTO_REFRESH", False)
try:
    REFRESH_EVERY_MIN = int((os.getenv("REFRESH_EVERY_MIN") or "15").strip())
except ValueError:
    REFRESH_EVERY_MIN = 15
if REFRESH_EVERY_MIN < 1:
    REFRESH_EVERY_MIN = 1

# Auto-refresh (light mode): skip leagues refreshed recently; batch size 0 = all per cycle
try:
    REFRESH_SKIP_IF_FRESH_MIN = int((os.getenv("REFRESH_SKIP_IF_FRESH_MIN") or "30").strip())
except ValueError:
    REFRESH_SKIP_IF_FRESH_MIN = 30
if REFRESH_SKIP_IF_FRESH_MIN < 0:
    REFRESH_SKIP_IF_FRESH_MIN = 0

try:
    AUTO_REFRESH_LEAGUES_PER_CYCLE = int((os.getenv("AUTO_REFRESH_LEAGUES_PER_CYCLE") or "4").strip())
except ValueError:
    AUTO_REFRESH_LEAGUES_PER_CYCLE = 4

try:
    AUTO_REFRESH_FINISHED_DAYS = int((os.getenv("AUTO_REFRESH_FINISHED_DAYS") or "3").strip())
except ValueError:
    AUTO_REFRESH_FINISHED_DAYS = 3
if AUTO_REFRESH_FINISHED_DAYS < 1:
    AUTO_REFRESH_FINISHED_DAYS = 1

AUTO_REFRESH_FETCH_ODDS = _env_bool("AUTO_REFRESH_FETCH_ODDS", False)

try:
    RATE_LIMIT_COOLDOWN_CAP_SEC = int((os.getenv("RATE_LIMIT_COOLDOWN_CAP_SEC") or "600").strip())
except ValueError:
    RATE_LIMIT_COOLDOWN_CAP_SEC = 600
if RATE_LIMIT_COOLDOWN_CAP_SEC < 0:
    RATE_LIMIT_COOLDOWN_CAP_SEC = 0


class Settings:
    """Objeto de configuración accesible en toda la app."""

    def __init__(self) -> None:
        self.base_dir = BASE_DIR
        self.cache_dir = CACHE_DIR
        self.daily_dir = DAILY_DIR
        self.db_path = DB_PATH
        self.app_base_url = APP_BASE_URL
        self.football_data_api_key = FOOTBALL_DATA_API_KEY
        self.api_sports_key = API_SPORTS_KEY

        self.leagues = LEAGUES
        self.league_sport = LEAGUE_SPORT
        self.default_league = DEFAULT_LEAGUE
        self.free_leagues = FREE_LEAGUES

        self.min_prob_for_candidate = MIN_PROB_FOR_CANDIDATE
        self.default_xg_home = DEFAULT_XG_HOME
        self.default_xg_away = DEFAULT_XG_AWAY
        self.max_goals_poisson = MAX_GOALS_POISSON

        self.picks_model = PICKS_MODEL
        self.team_form_days_back = TEAM_FORM_DAYS_BACK
        self.team_form_limit = TEAM_FORM_LIMIT
        self.refresh_topn_model_b = REFRESH_TOPN_MODEL_B

        self.debug = DEBUG
        self.log_level = LOG_LEVEL

        self.auto_refresh = AUTO_REFRESH
        self.refresh_every_min = REFRESH_EVERY_MIN
        self.refresh_skip_if_fresh_min = REFRESH_SKIP_IF_FRESH_MIN
        self.auto_refresh_leagues_per_cycle = AUTO_REFRESH_LEAGUES_PER_CYCLE
        self.auto_refresh_finished_days = AUTO_REFRESH_FINISHED_DAYS
        self.auto_refresh_fetch_odds = AUTO_REFRESH_FETCH_ODDS
        self.rate_limit_cooldown_cap_sec = RATE_LIMIT_COOLDOWN_CAP_SEC

        self.secret_key = SECRET_KEY

        self.plan_free = PLAN_FREE
        self.plan_premium = PLAN_PREMIUM
        self.plan_pro = PLAN_PRO
        self.default_plan = DEFAULT_PLAN

        self.price_premium_usd = PRICE_PREMIUM_USD
        self.price_pro_usd = PRICE_PRO_USD

        # Stripe / billing
        self.stripe_secret_key = STRIPE_SECRET_KEY
        self.stripe_publishable_key = STRIPE_PUBLISHABLE_KEY
        self.stripe_price_id = STRIPE_PRICE_ID
        self.stripe_webhook_secret = STRIPE_WEBHOOK_SECRET

    def league_codes(self) -> list[str]:
        return list(self.leagues.keys())

    def is_valid_league(self, code: str) -> bool:
        return code in self.leagues

    def use_model_b(self) -> bool:
        return (self.picks_model or "B").upper() == "B"


settings = Settings()