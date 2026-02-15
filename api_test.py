import os
import json
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI()

LEAGUES = ["PL", "PD", "SA", "BL1", "FL1"]

def load_league_data(league):
    matches_file = f"daily_matches_{league}.json"
    picks_file = f"daily_picks_{league}.json"

    matches = []
    picks = {}

    if os.path.exists(matches_file):
        with open(matches_file, "r", encoding="utf-8") as f:
            matches = json.load(f)

    if os.path.exists(picks_file):
        with open(picks_file, "r", encoding="utf-8") as f:
            raw_picks = json.load(f)
            for p in raw_picks:
                key = str(
                    p.get("match_id")
                    or p.get("id")
                    or f"{p.get('home','?')}_{p.get('away','?')}_{p.get('utcDate','?')}"
                )
                picks[key] = p

    result = []

    for m in matches:
        mid = (
            m.get("match_id")
            or m.get("id")
            or f"{m.get('home','?')}_{m.get('away','?')}_{m.get('utcDate','?')}"
        )

        key = str(mid)
        pick = picks.get(key, {})

        result.append({
            "home": m.get("home"),
            "away": m.get("away"),
            "date": m.get("utcDate"),
            "status": m.get("status", "TIMED"),
            "xg_home": m.get("xg_home"),
            "xg_away": m.get("xg_away"),
            "market": pick.get("market"),
            "prob": pick.get("prob"),
            "fair": pick.get("fair"),
            "confidence": pick.get("confidence"),
        })

    return result

@app.get("/", response_class=HTMLResponse)
def dashboard(league: str = "PL"):

    data = load_league_data(league)

    html = """
    <html>
    <head>
        <title>AFTR MVP</title>
        <style>
            body { font-family: Arial; background:#111; color:#fff; padding:20px; }
            .card { background:#1c1c1c; padding:15px; margin:10px 0; border-radius:8px; }
            .title { font-weight:bold; font-size:16px; }
            .pick { color:#00ff99; margin-top:5px; }
        </style>
    </head>
    <body>
    <h2>AFTR MVP - """ + league + """</h2>
    """

    if not data:
        html += "<p>No matches available.</p>"

    for m in data:
        html += f"""
        <div class='card'>
            <div class='title'>{m['home']} vs {m['away']}</div>
            <div>{m['date']} | {m['status']}</div>
            <div>xG: {m['xg_home']} - {m['xg_away']}</div>
        """

        if m["market"]:
            html += f"""
            <div class='pick'>
                Pick: {m['market']} |
                Prob: {m['prob']} |
                Fair: {m['fair']} |
                Conf: {m['confidence']}
            </div>
            """

        html += "</div>"

    html += "</body></html>"

    return html







