from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.routes.matches import router as matches_router
from app.routes.picks import router as picks_router
from app.ui import router as ui_router

app = FastAPI(title="AFTR Pick Local")

# Static (CSS)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Routers
app.include_router(ui_router)
app.include_router(matches_router, prefix="/api", tags=["matches"])
app.include_router(picks_router, prefix="/api", tags=["picks"])

@app.get("/health")
def health():
    return {"ok": True}