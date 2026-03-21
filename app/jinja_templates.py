"""
Jinja2 templates for HTML fragments (league carousel, etc.).
"""
from __future__ import annotations

from jinja2 import Environment, FileSystemLoader, select_autoescape

from config.settings import settings

_jinja_env: Environment | None = None


def get_jinja_env() -> Environment:
    global _jinja_env
    if _jinja_env is None:
        _jinja_env = Environment(
            loader=FileSystemLoader(str(settings.base_dir / "templates")),
            autoescape=select_autoescape(["html", "xml"]),
        )
    return _jinja_env


def render_league_carousel(
    *,
    active_league: str,
    unsupported: set[str] | None = None,
    carousel_id: str = "leagueCarousel",
    home_mode: bool = False,
) -> str:
    """
    Premium coverflow league strip. ``active_league`` syncs center card with ``?league=`` on dashboard.
    """
    unsupported = unsupported or set()
    leagues: list[dict[str, str]] = []
    for code, name in settings.leagues.items():
        if code in unsupported:
            continue
        leagues.append(
            {
                "code": code,
                "name": name,
                "logo": f"/static/leagues/{code.lower()}.png",
                "initial": (name or code)[:1].upper(),
            }
        )
    return get_jinja_env().get_template("components/league_carousel.html").render(
        leagues=leagues,
        active_league=active_league or "",
        carousel_id=carousel_id,
        home_mode=home_mode,
    )
