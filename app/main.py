import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.routes.matches import router as matches_router
from app.routes.picks import router as picks_router
from app.ui import router as ui_router
from config.settings import settings

# Logging
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("aftr")

app = FastAPI(
    title="AFTR Pick",
    description="API y dashboard de picks deportivos",
    version="1.0.0",
)

# Static (ruta absoluta para funcionar desde cualquier CWD)
static_dir = settings.base_dir / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(ui_router)
app.include_router(matches_router, prefix="/api", tags=["matches"])
app.include_router(picks_router, prefix="/api", tags=["picks"])


@app.get("/health")
def health():
    return {"ok": True, "debug": settings.debug}