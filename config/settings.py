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
_logger.debug("CACHE_DIR resolved: %s", CACHE_DIR)

# Fallback legacy (solo lectura; no escribir aquí)
DAILY_DIR: Path = BASE_DIR / "daily"

# PostgreSQL connection URL.
# En producción: postgresql://user:pass@host:5432/dbname
# Fallback local para dev sin Docker (requiere PostgreSQL instalado localmente).
DATABASE_URL: str = (
    (os.getenv("DATABASE_URL") or "").strip()
    or "postgresql://aftr:aftrdev@localhost:5432/aftr"
)

# Legacy SQLite path — usado SOLO por el script de migración scripts/migrate_sqlite_to_pg.py.
# No se usa en la app principal.
_aftr_db_env: str = (os.getenv("AFTR_DB_PATH") or "").strip()
DB_PATH: str = (
    _aftr_db_env
    if _aftr_db_env and _aftr_db_env.upper() != "NONE"
    else str(BASE_DIR / "aftr.db")
)

# App base URL (para links absolutos en emails / Stripe)
APP_BASE_URL: str = (os.getenv("APP_BASE_URL") or "").strip().rstrip("/")

# Session cookies: Secure flag (required on HTTPS for reliable behavior in modern browsers).
_cookie_secure_raw = (os.getenv("COOKIE_SECURE") or "").strip().lower()
if _cookie_secure_raw in ("1", "true", "yes"):
    COOKIE_SECURE = True
elif _cookie_secure_raw in ("0", "false", "no"):
    COOKIE_SECURE = False
else:
    COOKIE_SECURE = APP_BASE_URL.lower().startswith("https://")

# SMTP (password recovery, etc.)
SMTP_SERVER: str = (os.getenv("SMTP_SERVER") or "").strip()
SMTP_PORT: int = int(os.getenv("SMTP_PORT") or "0") or 0
SMTP_USER: str = (os.getenv("SMTP_USER") or "").strip()
SMTP_PASSWORD: str = (os.getenv("SMTP_PASSWORD") or "").strip()
EMAIL_FROM: str = (os.getenv("EMAIL_FROM") or "").strip()

# Push notifications (VAPID)
VAPID_PUBLIC_KEY: str  = (os.getenv("VAPID_PUBLIC_KEY")  or "").strip()
VAPID_PRIVATE_KEY: str = (os.getenv("VAPID_PRIVATE_KEY") or "").strip()
VAPID_EMAIL: str       = (os.getenv("VAPID_EMAIL")       or "mailto:aftrapp@outlook.com").strip()

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

# Stripe (legacy — reemplazado por Lemon Squeezy)
STRIPE_SECRET_KEY: str = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
STRIPE_PUBLISHABLE_KEY: str = (os.getenv("STRIPE_PUBLISHABLE_KEY") or "").strip()
STRIPE_PRICE_ID: str = (os.getenv("STRIPE_PRICE_ID") or "").strip()
STRIPE_WEBHOOK_SECRET: str = (os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()

# Mercado Pago (pagos Argentina)
MP_ACCESS_TOKEN: str       = (os.getenv("MP_ACCESS_TOKEN")       or "").strip()
MP_PLAN_ID: str            = (os.getenv("MP_PLAN_ID")            or "").strip()
MP_SUBSCRIPTION_AMOUNT: str = (os.getenv("MP_SUBSCRIPTION_AMOUNT") or "4999").strip()
MP_WEBHOOK_SECRET: str     = (os.getenv("MP_WEBHOOK_SECRET")     or "").strip()

# Lemon Squeezy (pagos globales)
LEMONSQUEEZY_API_KEY: str = (os.getenv("LEMONSQUEEZY_API_KEY") or "").strip()
LEMONSQUEEZY_STORE_ID: str = (os.getenv("LEMONSQUEEZY_STORE_ID") or "").strip()
LEMONSQUEEZY_VARIANT_ID: str = (os.getenv("LEMONSQUEEZY_VARIANT_ID") or "1485723").strip()
LEMONSQUEEZY_WEBHOOK_SECRET: str = (os.getenv("LEMONSQUEEZY_WEBHOOK_SECRET") or "").strip()

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
AUTO_REFRESH: bool = _env_bool("AUTO_REFRESH", True)

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

# Si refresh_running queda en True (crash / kill -9), tras este tiempo se considera colgado y se libera al leer meta.
# Debe ser mayor que el peor tiempo de un refresh completo esperado (p. ej. 600–1800 en prod).
try:
    REFRESH_RUNNING_TTL_SEC = int((os.getenv("REFRESH_RUNNING_TTL_SEC") or "600").strip())
except ValueError:
    REFRESH_RUNNING_TTL_SEC = 600
if REFRESH_RUNNING_TTL_SEC < 0:
    REFRESH_RUNNING_TTL_SEC = 0

# --- Auto-refresh multi-tier (LIVE / UPCOMING / RESULTS) ---
try:
    LIVE_REFRESH_SECONDS = int((os.getenv("LIVE_REFRESH_SECONDS") or "60").strip())
except ValueError:
    LIVE_REFRESH_SECONDS = 60
if LIVE_REFRESH_SECONDS < 15:
    LIVE_REFRESH_SECONDS = 15

try:
    LIVE_REFRESH_MIN_INTERVAL_SEC = int((os.getenv("LIVE_REFRESH_MIN_INTERVAL_SEC") or "30").strip())
except ValueError:
    LIVE_REFRESH_MIN_INTERVAL_SEC = 30
if LIVE_REFRESH_MIN_INTERVAL_SEC < 10:
    LIVE_REFRESH_MIN_INTERVAL_SEC = 10

# UPCOMING_REFRESH_MIN overrides legacy REFRESH_EVERY_MIN if only the latter is set
_raw_upcoming = (os.getenv("UPCOMING_REFRESH_MIN") or "").strip()
if not _raw_upcoming:
    _raw_upcoming = (os.getenv("REFRESH_EVERY_MIN") or "15").strip()
try:
    UPCOMING_REFRESH_MIN = int(_raw_upcoming)
except ValueError:
    UPCOMING_REFRESH_MIN = 15
if UPCOMING_REFRESH_MIN < 1:
    UPCOMING_REFRESH_MIN = 1

try:
    RESULTS_REFRESH_MIN = int((os.getenv("RESULTS_REFRESH_MIN") or "10").strip())
except ValueError:
    RESULTS_REFRESH_MIN = 10
if RESULTS_REFRESH_MIN < 1:
    RESULTS_REFRESH_MIN = 1

try:
    REFRESH_BACKOFF_SECONDS = int((os.getenv("REFRESH_BACKOFF_SECONDS") or "120").strip())
except ValueError:
    REFRESH_BACKOFF_SECONDS = 120
if REFRESH_BACKOFF_SECONDS < 0:
    REFRESH_BACKOFF_SECONDS = 0

# Ventana de partidos FINISHED para job results (24–48h típico)
try:
    RESULTS_FINISHED_HOURS = int((os.getenv("RESULTS_FINISHED_HOURS") or "48").strip())
except ValueError:
    RESULTS_FINISHED_HOURS = 48
if RESULTS_FINISHED_HOURS < 6:
    RESULTS_FINISHED_HOURS = 6
if RESULTS_FINISHED_HOURS > 168:
    RESULTS_FINISHED_HOURS = 168

# Pre-match / odds: solo ligas con partido en las próximas N horas
try:
    ODDS_PREMATCH_HOURS = int((os.getenv("ODDS_PREMATCH_HOURS") or "24").strip())
except ValueError:
    ODDS_PREMATCH_HOURS = 24
if ODDS_PREMATCH_HOURS < 1:
    ODDS_PREMATCH_HOURS = 1

# No volver a pegarle a The Odds API si el archivo de caché es más reciente que esto
try:
    ODDS_MIN_REFRESH_MINUTES = int((os.getenv("ODDS_MIN_REFRESH_MINUTES") or "20").strip())
except ValueError:
    ODDS_MIN_REFRESH_MINUTES = 20
if ODDS_MIN_REFRESH_MINUTES < 0:
    ODDS_MIN_REFRESH_MINUTES = 0

try:
    FOOTBALL_HTTP_CACHE_TTL_SEC = int((os.getenv("FOOTBALL_HTTP_CACHE_TTL_SEC") or "45").strip())
except ValueError:
    FOOTBALL_HTTP_CACHE_TTL_SEC = 45
if FOOTBALL_HTTP_CACHE_TTL_SEC < 0:
    FOOTBALL_HTTP_CACHE_TTL_SEC = 0


class Settings:
    """Objeto de configuración accesible en toda la app."""

    def __init__(self) -> None:
        self.base_dir = BASE_DIR
        self.cache_dir = CACHE_DIR
        self.daily_dir = DAILY_DIR
        self.database_url = DATABASE_URL
        self.db_path = DB_PATH  # legacy — solo para script de migración
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
        self.refresh_every_min = UPCOMING_REFRESH_MIN
        self.refresh_skip_if_fresh_min = REFRESH_SKIP_IF_FRESH_MIN
        self.auto_refresh_leagues_per_cycle = AUTO_REFRESH_LEAGUES_PER_CYCLE
        self.auto_refresh_finished_days = AUTO_REFRESH_FINISHED_DAYS
        self.auto_refresh_fetch_odds = AUTO_REFRESH_FETCH_ODDS
        self.rate_limit_cooldown_cap_sec = RATE_LIMIT_COOLDOWN_CAP_SEC
        self.refresh_running_ttl_sec = REFRESH_RUNNING_TTL_SEC

        self.live_refresh_seconds = LIVE_REFRESH_SECONDS
        self.live_refresh_min_interval_sec = LIVE_REFRESH_MIN_INTERVAL_SEC
        self.upcoming_refresh_min = UPCOMING_REFRESH_MIN
        self.results_refresh_min = RESULTS_REFRESH_MIN
        self.refresh_backoff_seconds = REFRESH_BACKOFF_SECONDS
        self.results_finished_hours = RESULTS_FINISHED_HOURS
        self.odds_prematch_hours = ODDS_PREMATCH_HOURS
        self.odds_min_refresh_minutes = ODDS_MIN_REFRESH_MINUTES
        self.football_http_cache_ttl_sec = FOOTBALL_HTTP_CACHE_TTL_SEC

        self.secret_key = SECRET_KEY
        self.cookie_secure = COOKIE_SECURE

        self.plan_free = PLAN_FREE
        self.plan_premium = PLAN_PREMIUM
        self.plan_pro = PLAN_PRO
        self.default_plan = DEFAULT_PLAN

        self.price_premium_usd = PRICE_PREMIUM_USD
        self.price_pro_usd = PRICE_PRO_USD

        # Stripe (legacy)
        self.stripe_secret_key = STRIPE_SECRET_KEY
        self.stripe_publishable_key = STRIPE_PUBLISHABLE_KEY
        self.stripe_price_id = STRIPE_PRICE_ID
        self.stripe_webhook_secret = STRIPE_WEBHOOK_SECRET

        # Mercado Pago
        self.mp_access_token       = MP_ACCESS_TOKEN
        self.mp_plan_id            = MP_PLAN_ID
        self.mp_subscription_amount = MP_SUBSCRIPTION_AMOUNT
        self.mp_webhook_secret     = MP_WEBHOOK_SECRET
        # Lemon Squeezy
        self.lemonsqueezy_api_key = LEMONSQUEEZY_API_KEY
        self.lemonsqueezy_store_id = LEMONSQUEEZY_STORE_ID
        self.lemonsqueezy_variant_id = LEMONSQUEEZY_VARIANT_ID
        self.lemonsqueezy_webhook_secret = LEMONSQUEEZY_WEBHOOK_SECRET

    def league_codes(self) -> list[str]:
        return list(self.leagues.keys())

    def is_valid_league(self, code: str) -> bool:
        return code in self.leagues

    def use_model_b(self) -> bool:
        return (self.picks_model or "B").upper() == "B"


settings = Settings()