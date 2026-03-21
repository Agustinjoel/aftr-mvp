import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.auto_refresh import spawn_auto_refresh_task
from app.routes.matches import router as matches_router
from app.routes.picks import router as picks_router
from app.routes.user import router as user_router
from app.ui import router as ui_router
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
    Startup: optional background auto-refresh (AUTO_REFRESH=true).
    Shutdown: cancel the asyncio task (in-flight refresh may finish in thread pool).
    """
    task: asyncio.Task | None = None
    if settings.auto_refresh:
        interval_sec = float(settings.refresh_every_min) * 60.0
        logger.info(
            "AUTO REFRESH: starting scheduler on app startup | every %.0f min (%.0fs) | %s",
            settings.refresh_every_min,
            interval_sec,
            datetime.now(timezone.utc).isoformat(),
        )
        task = spawn_auto_refresh_task(interval_sec)
        if task.done():
            logger.error(
                "AUTO REFRESH: task exited immediately (scheduler broken?) | %s",
                datetime.now(timezone.utc).isoformat(),
            )
    else:
        logger.info(
            "AUTO REFRESH: scheduler not enabled (set AUTO_REFRESH=true) | %s",
            datetime.now(timezone.utc).isoformat(),
        )
    yield
    if task is not None:
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
)

app.mount("/static", StaticFiles(directory="static"), name="static")

# Static (ruta absoluta para funcionar desde cualquier CWD)
static_dir = settings.base_dir / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(ui_router)
app.include_router(matches_router, prefix="/api", tags=["matches"])
app.include_router(picks_router, prefix="/api", tags=["picks"])
app.include_router(auth_router)
app.include_router(user_router, prefix="/user", tags=["user"])
app.include_router(pay_router)


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