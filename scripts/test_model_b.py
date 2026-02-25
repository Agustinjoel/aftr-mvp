from core.model_b import compute_team_form, estimate_xg_dynamic
from config.settings import settings

TEAM_HOME = 1
TEAM_AWAY = 2

matches_home = [
    {"home_id": 1, "away_id": 9, "home_goals": 2, "away_goals": 0},
    {"home_id": 8, "away_id": 1, "home_goals": 1, "away_goals": 1},
    {"home_id": 1, "away_id": 7, "home_goals": 3, "away_goals": 2},
]

matches_away = [
    {"home_id": 2, "away_id": 6, "home_goals": 2, "away_goals": 1},
    {"home_id": 5, "away_id": 2, "home_goals": 1, "away_goals": 2},
    {"home_id": 2, "away_id": 4, "home_goals": 3, "away_goals": 1},
]
home_form = compute_team_form(TEAM_HOME, matches_home)
away_form = compute_team_form(TEAM_AWAY, matches_away)

xgH, xgA = estimate_xg_dynamic(home_form, away_form)

print("home_form:", home_form)
print("away_form:", away_form)
print("xg_dynamic:", xgH, xgA)
print("defaults:", settings.default_xg_home, settings.default_xg_away)
