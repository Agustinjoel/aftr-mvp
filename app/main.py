import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Response
from fastapi.staticfiles import StaticFiles

from app.auto_refresh import spawn_auto_refresh_tasks
from app.routes.matches import router as matches_router
from app.routes.picks import router as picks_router
from app.routes.match_detail import router as match_detail_router
from app.routes.user import router as user_router
from app.routes.tracker import router as tracker_router
from app.ui import router as ui_router
from app.routes.live import router as live_router
from config.settings import settings
from app.db import init_db
from app.auth import router as auth_router, clear_session_if_invalid
from app.payments import router as pay_router
from starlette.middleware.base import BaseHTTPMiddleware
from data.cache import read_cache_meta, read_json_with_fallback

# Logging
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("aftr")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup: optional multi-tier auto-refresh (AUTO_REFRESH=true): LIVE + UPCOMING + RESULTS.
    Shutdown: cancel all asyncio tasks (in-flight work may finish in thread pool).
    """
    tasks: list[asyncio.Task[None]] = []
    if settings.auto_refresh:
        logger.info(
            "AUTO REFRESH: starting tiered scheduler | live=%ss odds=%dm results=%dm | %s",
            getattr(settings, "live_refresh_seconds", 60),
            getattr(settings, "upcoming_refresh_min", 15),
            getattr(settings, "results_refresh_min", 10),
            datetime.now(timezone.utc).isoformat(),
        )
        tasks = spawn_auto_refresh_tasks()
    else:
        logger.info(
            "AUTO REFRESH: scheduler not enabled (set AUTO_REFRESH=true) | %s",
            datetime.now(timezone.utc).isoformat(),
        )
    yield
    for task in tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="AFTR Pick",
    description="API y dashboard de picks deportivos",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,      # disable /docs in production
    redoc_url=None,     # disable /redoc in production
    openapi_url=None,   # disable /openapi.json
)

# Absolute path so uvicorn works when CWD is not the project root (e.g. some PaaS layouts).
static_dir = settings.base_dir / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/sw.js", include_in_schema=False)
async def service_worker():
    """Sirve el service worker desde el root para que tenga scope '/'."""
    sw_path = static_dir / "sw.js"
    content = sw_path.read_bytes()
    return Response(
        content=content,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-store"},
    )

# Auth before UI so /auth/* is not shadowed by ui_router (e.g. legacy duplicate paths).
app.include_router(auth_router)
app.include_router(ui_router)
app.include_router(live_router)
app.include_router(matches_router, prefix="/api", tags=["matches"])
app.include_router(picks_router, prefix="/api", tags=["picks"])
app.include_router(match_detail_router, prefix="/api", tags=["match-detail"])
app.include_router(user_router, prefix="/user", tags=["user"])
app.include_router(tracker_router, prefix="/tracker", tags=["tracker"])
app.include_router(pay_router)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to every response."""
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        # Remove server fingerprint
        try:
            del response.headers["server"]
        except (KeyError, Exception):
            pass
        return response


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log every incoming request method + path."""
    async def dispatch(self, request, call_next):
        logger.info("REQ %s %s", request.method, request.url.path)
        response = await call_next(request)
        return response


class ClearInvalidSessionMiddleware(BaseHTTPMiddleware):
    """Clear aftr_session cookie when the stored uid does not exist in DB."""
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        clear_session_if_invalid(request, response)
        return response


app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(ClearInvalidSessionMiddleware)
app.add_middleware(RequestLoggingMiddleware)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/status", tags=["status"])
def api_status():
    """
    Lightweight status: refresh state, last update, leagues with data, total picks count.
    """
    meta = read_cache_meta()
    leagues_loaded: list[str] = []
    picks_total = 0
    for code in settings.league_codes():
        picks = read_json_with_fallback(f"daily_picks_{code}.json")
        if isinstance(picks, list) and len(picks) > 0:
            leagues_loaded.append(code)
            picks_total += len(picks)
    return {
        "refresh_running": meta.get("refresh_running", False),
        "last_update": meta.get("last_updated"),
        "leagues_loaded": leagues_loaded,
        "picks_total": picks_total,
    }


init_db()