"""
Helpers de equipos: slugs, rutas de logos y crest HTML.
Sin dependencias de FastAPI ni de otros módulos de la app.
"""
from __future__ import annotations

import html as html_lib
import unicodedata


# =========================================================
# Constantes de rutas estáticas
# =========================================================

TEAM_LOGO_FALLBACK_PATH = "/static/teams/default.svg"

LEAGUE_LOGO_PATHS: dict[str, str] = {
    "PL":  "/static/leagues/pl.png",
    "CL":  "/static/leagues/cl.png",
    "PD":  "/static/leagues/pd.png",
    "SA":  "/static/leagues/sa.png",
    "NBA": "/static/leagues/nba.png",
}

LEAGUE_LOGO_FALLBACK_PATH = "/static/leagues/fallback.svg"

FEATURED_LEAGUE_CODES = ["PL", "CL", "PD", "SA", "NBA"]

HOME_NAV_LEAGUES = [
    ("PL",  "Premier League"),
    ("CL",  "UEFA Champions League"),
    ("PD",  "LaLiga"),
    ("SA",  "Serie A"),
    ("NBA", "NBA"),
]


# =========================================================
# Team logo slug + path
# =========================================================

def _team_logo_slug(name: str) -> str:
    """
    Normaliza el nombre de un equipo a un slug para la ruta estática de logo.
    Ej. 'Eintracht Frankfurt' → 'eintracht-frankfurt'.
    """
    if not name or not isinstance(name, str):
        return ""
    # Quita acentos: NFD descompone, luego se descartan los combining chars
    s = unicodedata.normalize("NFD", name)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = "".join(c for c in s if c.isalnum() or c in " -")
    s = s.strip().replace(" ", "-").lower()
    while "--" in s:
        s = s.replace("--", "-")
    return s.strip("-")


def _team_logo_path(team_name: str) -> str:
    """Devuelve la ruta estática al logo del equipo; fallback si el slug está vacío."""
    slug = _team_logo_slug(team_name)
    if not slug:
        return TEAM_LOGO_FALLBACK_PATH
    return f"/static/teams/{slug}.png"


# =========================================================
# Team crest HTML
# =========================================================

def _team_with_crest(crest: str | None, name: str) -> str:
    """
    Renderiza la fila de equipo: usa crest URL si está presente,
    sino /static/teams/{slug}.png. Fallback a default.svg en 404.
    """
    def _normalize_team_name(raw: str) -> str:
        n = (raw or "").strip()
        if not n:
            return ""
        n = n.replace("Football Club", "FC")
        n = n.replace("Club Atlético", "Atl.")
        # Quita "Hotspur" para evitar wrapping feo (Tottenham Hotspur → Tottenham)
        words = [w for w in n.split() if w.strip().lower() != "hotspur"]
        n = " ".join(words).strip()
        n = " ".join(n.split())
        return n

    normalized_name  = _normalize_team_name(name)
    safe_name        = html_lib.escape(normalized_name or "")
    small_name       = len(normalized_name) >= 18

    if crest and isinstance(crest, str) and crest.strip():
        src = crest.strip()
    else:
        src = _team_logo_path(name or "")

    safe_src         = html_lib.escape(src)
    fallback         = html_lib.escape(TEAM_LOGO_FALLBACK_PATH)
    team_name_class  = "team-name team-name--small" if small_name else "team-name"

    return (
        f'<span class="team-row">'
        f'<img src="{safe_src}" alt="" class="crest" loading="lazy" width="28" height="28" '
        f'onerror="this.src=\'{fallback}\';this.onerror=null;"/>'
        f'<span class="{team_name_class}">{safe_name}</span>'
        f"</span>"
    )
