import math

# Función Poisson
def poisson_probability(lmbda, k):
    return (math.exp(-lmbda) * (lmbda ** k)) / math.factorial(k)

# Calcular matriz de probabilidades
def match_prediction(home_avg_goals, away_avg_goals):

    max_goals = 6  # límite razonable de goles

    home_win = 0
    draw = 0
    away_win = 0
    over_25 = 0

    for home_goals in range(max_goals):
        for away_goals in range(max_goals):

            prob_home = poisson_probability(home_avg_goals, home_goals)
            prob_away = poisson_probability(away_avg_goals, away_goals)

            match_prob = prob_home * prob_away

            if home_goals > away_goals:
                home_win += match_prob
            elif home_goals == away_goals:
                draw += match_prob
            else:
                away_win += match_prob

            if home_goals + away_goals > 2:
                over_25 += match_prob

    return home_win, draw, away_win, over_25


# 🔥 EJEMPLO DE PRUEBA
home_avg = 1.8
away_avg = 1.2

home, draw, away, over = match_prediction(home_avg, away_avg)

print("Home Win Probability:", round(home * 100, 2), "%")
print("Draw Probability:", round(draw * 100, 2), "%")
print("Away Win Probability:", round(away * 100, 2), "%")
print("Over 2.5 Probability:", round(over * 100, 2), "%")

print("\nFair Odds:")
print("Home:", round(1/home, 2))
print("Draw:", round(1/draw, 2))
print("Away:", round(1/away, 2))
bookmaker_home_odds = 2.20
bookmaker_draw_odds = 3.80
bookmaker_away_odds = 3.50

print("\nExpected Value:")

ev_home = (home * bookmaker_home_odds) - 1
ev_draw = (draw * bookmaker_draw_odds) - 1
ev_away = (away * bookmaker_away_odds) - 1

print("Home EV:", round(ev_home * 100, 2), "%")
print("Draw EV:", round(ev_draw * 100, 2), "%")
print("Away EV:", round(ev_away * 100, 2), "%")