import requests

API_KEY = "3c139115487a45faa9ed84c633120c21"

headers = {
    "X-Auth-Token": API_KEY
}

url = "https://api.football-data.org/v4/competitions/PL/matches"

response = requests.get(url, headers=headers)
data = response.json()

matches = data.get("matches", [])

home_goals = 0
away_goals = 0
finished_matches = 0

for match in matches:
    if match["status"] == "FINISHED":
        home_goals += match["score"]["fullTime"]["home"] or 0
        away_goals += match["score"]["fullTime"]["away"] or 0
        finished_matches += 1

avg_home = home_goals / finished_matches
avg_away = away_goals / finished_matches

print("Finished matches:", finished_matches)
print("Average Home Goals:", round(avg_home, 2))
print("Average Away Goals:", round(avg_away, 2))