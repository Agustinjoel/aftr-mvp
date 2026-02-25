from __future__ import annotations
from dataclasses import dataclass 
from config.settings import settings

@dataclass
class TeamForm:
    gf_avg: float #goles a favor promedio
    ga_avg: float #goles en contra promedio
    n: int

def compute_team_form(team_id: int, matches: list[dict]) -> TeamForm:
    """
    matches: lista de dicts con:
    home_id, away_id, home_goals, away_goals
    """
    gf = ga = n = 0

    for m in matches:
        hid = m.get("home_id")
        aid = m.get("away_id")
        hg = int(m.get("home_goals") or 0)
        ag = int(m.get("away_goals") or 0)

        if hid == team_id:
            gf += hg
            ga += ag
            n += 1
        elif aid == team_id:
            gf += ag
            ga += ag
            n += 1

    if n == 0:
        return TeamForm(0.0, 0.0, 0)
    return TeamForm(gf_avg=gf / n, ga_avg=ga / n, n=n)

def estimate_xg_dynamic(home_form, away_form):
    if home_form.n == 0 or away_form.n == 0:
        return settings.default_xg_home, settings.default_xg_away

    raw_home = (home_form.gf_avg + away_form.ga_avg) / 2.0
    raw_away = (away_form.gf_avg + home_form.ga_avg) / 2.0

    # Mezcla con default para estabilizar
    xg_home = raw_home * 0.75 + settings.default_xg_home * 0.25
    xg_away = raw_away * 0.75 + settings.default_xg_away * 0.25

    xg_home = max(0.2, min(3.5, xg_home))
    xg_away = max(0.2, min(3.5, xg_away))

    return xg_home, xg_away

def compute_team_form_split(team_id: int, matches: list[dict], mode: str) -> TeamForm:
    """
    mode: "home" -> solo partidos donde team_id jugó como local
          "away" -> solo partidos donde team_id jugó como visitante
    Pondera por recencia: los primeros partidos (más recientes) pesan más.
    """
    gf = ga = 0.0
    wsum = 0.0
    n = 0

    # asumimos matches vienen del endpoint ya ordenados por más recientes primero
    weights = [1.50, 1.35, 1.25, 1.15, 1.10, 1.05, 1.00, 1.00, 1.00, 1.00]

    for i, m in enumerate(matches):
        w = weights[i] if i < len(weights) else 1.0

        hid = m.get("home_id")
        aid = m.get("away_id")
        hg = float(m.get("home_goals") or 0)
        ag = float(m.get("away_goals") or 0)

        if mode == "home" and hid == team_id:
            gf += hg * w
            ga += ag * w
            wsum += w
            n += 1
        elif mode == "away" and aid == team_id:
            gf += ag * w
            ga += hg * w
            wsum += w
            n += 1

    if n == 0 or wsum == 0:
        return TeamForm(0.0, 0.0, 0)

    return TeamForm(gf / wsum, ga / wsum, n)

def estimate_xg_dynamic_split(home_id: int, away_id: int, home_matches: list[dict], away_matches: list[dict]) -> tuple[float, float]:
    """
    xG con split real:
    home usa sus stats jugando de local
    away usa sus stats jugando de visitante
    """
    home_home = compute_team_form_split(home_id, home_matches, "home")
    home_def = compute_team_form_split(home_id, home_matches, "home")  # mismo set, pero ga_avg sirve de defensa
    away_away = compute_team_form_split(away_id, away_matches, "away")
    away_def = compute_team_form_split(away_id, away_matches, "away")

    if home_home.n == 0 or away_away.n == 0:
        return settings.default_xg_home, settings.default_xg_away

    raw_home = (home_home.gf_avg + away_def.ga_avg) / 2.0
    raw_away = (away_away.gf_avg + home_def.ga_avg) / 2.0

    # blend para estabilizar
    xg_home = raw_home * 0.75 + settings.default_xg_home * 0.25
    xg_away = raw_away * 0.75 + settings.default_xg_away * 0.25

    xg_home = max(0.2, min(3.5, xg_home))
    xg_away = max(0.2, min(3.5, xg_away))

    return xg_home, xg_away
    