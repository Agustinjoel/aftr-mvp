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
            ga += hg  # FIX: goals against for away team = home team's goals
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

    # Ponderación fuertemente decreciente: los últimos 5 partidos pesan mucho más
    weights = [2.00, 1.75, 1.50, 1.25, 1.10, 0.95, 0.85, 0.80, 0.75, 0.70]

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

def _blend_weight(n: int, min_w: float = 0.55, max_w: float = 0.90, scale: int = 5) -> float:
    """
    Peso para los datos de forma según tamaño de muestra.
    Con n=0 → min_w, con n>=scale(5) → max_w. Crece linealmente entre ambos.
    Con 5+ partidos recientes el modelo usa 90% forma real y solo 10% default_xg.
    """
    if n <= 0:
        return min_w
    return min(max_w, min_w + (max_w - min_w) * (n / scale))


def estimate_xg_dynamic_split(
    home_id: int,
    away_id: int,
    home_matches: list[dict],
    away_matches: list[dict],
) -> tuple[float, float]:
    """
    xG con split home/away real:
    - home_home.gf_avg: goles que el local hace jugando en casa
    - home_home.ga_avg: goles que el local recibe jugando en casa (su defensa local)
    - away_away.gf_avg: goles que el visitante hace jugando afuera
    - away_away.ga_avg: goles que el visitante recibe jugando afuera (su defensa visitante)

    xG home = promedio entre (ataque del local en casa) y (defensa del visitante afuera)
    xG away = promedio entre (ataque del visitante afuera) y (defensa del local en casa)

    El blend con el default se ajusta por tamaño de muestra:
    más partidos → más peso a los datos reales, menos al default global.
    """
    home_stats = compute_team_form_split(home_id, home_matches, "home")
    away_stats = compute_team_form_split(away_id, away_matches, "away")

    if home_stats.n == 0 or away_stats.n == 0:
        return settings.default_xg_home, settings.default_xg_away

    raw_home = (home_stats.gf_avg + away_stats.ga_avg) / 2.0
    raw_away = (away_stats.gf_avg + home_stats.ga_avg) / 2.0

    # Blend adaptativo: más muestra → más confianza en los datos reales
    min_n = min(home_stats.n, away_stats.n)
    form_w = _blend_weight(min_n)
    default_w = 1.0 - form_w

    xg_home = raw_home * form_w + settings.default_xg_home * default_w
    xg_away = raw_away * form_w + settings.default_xg_away * default_w

    xg_home = max(0.2, min(3.5, xg_home))
    xg_away = max(0.2, min(3.5, xg_away))

    return xg_home, xg_away
    