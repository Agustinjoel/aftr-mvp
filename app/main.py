import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Response
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
from app.routes.premium import router as premium_router
from app.routes.chat import router as chat_router
from starlette.middleware.base import BaseHTTPMiddleware
from data.cache import read_cache_meta, read_json_with_fallback, CACHE_DIR

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
    # ── Diagnóstico de memoria al arranque ──────────────────────────────────
    try:
        from data.cache import read_json_with_fallback
        _total_picks = 0
        _total_matches = 0
        _picks_by_league: list[str] = []
        for _code in settings.league_codes():
            _p = read_json_with_fallback(f"daily_picks_{_code}.json") or []
            _m = read_json_with_fallback(f"daily_matches_{_code}.json") or []
            _np = len(_p) if isinstance(_p, list) else 0
            _nm = len(_m) if isinstance(_m, list) else 0
            _total_picks += _np
            _total_matches += _nm
            if _np:
                _picks_by_league.append(f"{_code}:{_np}")
        logger.info(
            "STARTUP — Total de picks en memoria: %s | Total de partidos en memoria: %s | por liga: %s",
            _total_picks, _total_matches,
            ", ".join(_picks_by_league) if _picks_by_league else "(vacío — filesystem efímero)",
        )
    except Exception as _diag_err:
        logger.warning("STARTUP — error leyendo memoria: %s", _diag_err)

    # ── Limpiar locks colgados de arranques anteriores ──────────────────────────
    try:
        from data.cache import release_refresh_running_meta
        release_refresh_running_meta()
        logger.info("STARTUP — refresh_running liberado (limpieza de arranque)")
    except Exception as _lock_err:
        logger.warning("STARTUP — error liberando refresh_running: %s", _lock_err)
    try:
        from services.tiered_refresh import reset_live_lock
        reset_live_lock()
        logger.info("STARTUP — live lock reseteado")
    except Exception as _live_lock_err:
        logger.warning("STARTUP — error reseteando live lock: %s", _live_lock_err)

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


@app.get("/.well-known/assetlinks.json", include_in_schema=False)
async def assetlinks():
    """TWA (Trusted Web Activity) — Digital Asset Links para Google Play."""
    import json as _json
    fingerprint = os.getenv("TWA_SHA256_FINGERPRINT", "").strip()
    payload = [
        {
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {
                "namespace": "android_app",
                "package_name": "online.aftrapp.app",
                "sha256_cert_fingerprints": [fingerprint] if fingerprint else [],
            },
        }
    ]
    return Response(
        content=_json.dumps(payload),
        media_type="application/json",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/manifest.webmanifest", include_in_schema=False)
async def manifest():
    """Sirve el PWA manifest desde la raíz (requerido para TWA/bubblewrap)."""
    content = (static_dir / "manifest.webmanifest").read_bytes()
    return Response(content=content, media_type="application/manifest+json",
                    headers={"Cache-Control": "no-cache"})


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
app.include_router(premium_router, tags=["premium"])
app.include_router(chat_router)


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


@app.get("/api/admin/release-lock", tags=["status"])
def release_lock():
    """Libera refresh_running si lleva más de 5 minutos trabado."""
    from data.cache import release_refresh_running_meta, write_cache_meta, read_json
    from datetime import datetime, timezone
    try:
        raw = read_json("cache_meta.json")
        meta = dict(raw) if isinstance(raw, dict) else {}
        started = meta.get("refresh_started_at") or ""
        running = bool(meta.get("refresh_running"))
        if running and started:
            dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - dt).total_seconds()
            if age < 300:
                return {"ok": False, "message": f"Lock solo lleva {age:.0f}s, esperá que termine"}
    except Exception:
        pass
    release_refresh_running_meta()
    try:
        raw2 = read_json("cache_meta.json")
        base = dict(raw2) if isinstance(raw2, dict) else {}
        base["last_results_ts"] = 0
        write_cache_meta(base)
    except Exception:
        pass
    return {"ok": True, "message": "refresh_running liberado"}


@app.get("/api/history-stats", tags=["status"])
def api_history_stats():
    """Stats del historial de picks en disco (sin auth — solo conteos)."""
    import glob as _glob, os as _os
    from services.refresh_utils import _read_json_list
    result = {}
    push_sample = []  # sample de picks PUSH para ver si tienen probs/market
    for pattern in ["picks_history_*.json", "daily_picks_*.json"]:
        files = sorted(_glob.glob(_os.path.join(str(CACHE_DIR), pattern)))
        for fpath in files:
            fname = _os.path.basename(fpath)
            picks = _read_json_list(fname)
            by_result: dict = {}
            for p in picks or []:
                r = (p.get("result") or "PENDING").upper()
                by_result[r] = by_result.get(r, 0) + 1
                if r == "PUSH" and len(push_sample) < 3:
                    push_sample.append({
                        "file": fname,
                        "match_id": p.get("match_id"),
                        "best_market": p.get("best_market"),
                        "has_probs": bool(p.get("probs")),
                        "score_home": p.get("score_home"),
                        "score_away": p.get("score_away"),
                    })
            result[fname] = {"total": len(picks or []), "by_result": by_result}
    meta_path = CACHE_DIR / "cache_meta.json"
    try:
        import json as _json
        meta = _json.loads(meta_path.read_text()) if meta_path.exists() else {}
        rr = meta.get("refresh_running")
        rs = meta.get("refresh_started_at")
    except Exception:
        rr, rs = None, None
    return {"files": result, "refresh_running": rr, "refresh_started_at": rs, "push_sample": push_sample}


init_db()