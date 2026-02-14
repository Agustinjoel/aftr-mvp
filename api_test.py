import os
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

APP_TITLE = "AFTR MVP"
DB_FILE = os.getenv("AFTR_DB_FILE", "aftr.db")

# Timezone local (Argentina)
LOCAL_TZ = timezone(timedelta(hours=-3))

# Auto-refresh settings (opcional)
AUTO_REFRESH = os.getenv("AUTO_REFRESH", "1") == "1"
REFRESH_EVERY_MIN = int(os.getenv("REFRESH_EVERY_MIN", "15"))
REFRESH_KEY = os.getenv("REFRESH_KEY", "").strip()  # para endpoint /refresh

DEFAULT_LEAGUE = "PL"

LEAGUES = {
    "PL": "Premier League",
    "PD": "LaLiga",
    "SA": "Serie A",
    "BL1": "Bundesliga",
    "FL1": "Ligue 1",
    "CL": "Champions League",
    "EL": "Europa League",
    "FAC": "FA Cup",
}

LIVE_STATUSES = {"IN_PLAY", "PAUSED"}
UPCOMING_STATUSES = {"SCHEDULED", "TIMED"}
FINISHED_STATUS = "FINISHED"

app = FastAPI(title=APP_TITLE)


# =========================
# DB helpers
# =========================
def db_connect():
    try:
        return sqlite3.connect(DB_FILE, check_same_thread=False)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"No pude abrir DB ({DB_FILE}): {e}")


def safe_parse_dt(utc_iso: str):
    if not utc_iso:
        return None
    # football-data suele venir "2026-02-12T20:00:00Z"
    s = utc_iso.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def fmt_local_dt(utc_iso: str):
    dt = safe_parse_dt(utc_iso)
    if not dt:
        return utc_iso or ""
    return dt.astimezone(LOCAL_TZ).strftime("%d/%m %H:%M")


def get_last_updated():
    if not os.path.exists(DB_FILE):
        return None
    with db_connect() as con:
        cur = con.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {t[0] for t in cur.fetchall()}
        if "meta" not in tables:
            return None
        cur.execute("SELECT value FROM meta WHERE key='last_updated'")
        row = cur.fetchone()
        return row[0] if row else None


def row_to_item(r):
    # r:
    # match_id, utcDate, status, home, away, home_goals, away_goals, xg_home, xg_away, xg_total,
    # market, prob, fair, confidence, result, result_reason
    return {
        "match_id": r[0],
        "utcDate": r[1],
        "status": r[2],
        "home": r[3],
        "away": r[4],
        "home_goals": r[5],
        "away_goals": r[6],
        "xg_home": r[7],
        "xg_away": r[8],
        "xg_total": r[9],
        "market": r[10],
        "prob": r[11],
        "fair": r[12],
        "confidence": r[13],
        "result": r[14],
        "result_reason": r[15],
    }


def fetch_all_for_league(league: str):
    """
    Trae Live + Upcoming + Finished (últimos 60) con pick (si existe).
    """
    if not os.path.exists(DB_FILE):
        return []

    with db_connect() as con:
        cur = con.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {t[0] for t in cur.fetchall()}
        if not {"matches", "picks"}.issubset(tables):
            return []

        cur.execute("""
            SELECT m.match_id, m.utcDate, m.status, m.home, m.away, m.home_goals, m.away_goals,
                   m.xg_home, m.xg_away, m.xg_total,
                   p.market, p.prob, p.fair, p.confidence, p.result, p.result_reason
            FROM matches m
            LEFT JOIN picks p ON p.league=m.league AND p.match_id=m.match_id
            WHERE m.league=?
            ORDER BY m.utcDate ASC
        """, (league,))
        return [row_to_item(r) for r in cur.fetchall()]


def split_sections(items):
    live = [x for x in items if x["status"] in LIVE_STATUSES]
    upcoming = [x for x in items if x["status"] in UPCOMING_STATUSES]
    finished = [x for x in items if x["status"] == FINISHED_STATUS]

    # orden
    live.sort(key=lambda x: x["utcDate"] or "")
    upcoming.sort(key=lambda x: x["utcDate"] or "")
    finished.sort(key=lambda x: x["utcDate"] or "", reverse=True)

    # recortamos finished para no explotar la UI
    finished = finished[:60]
    return live, upcoming, finished


# =========================
# UI helpers
# =========================
def pill(text, cls="pill"):
    return f'<span class="{cls}">{text}</span>'


def fmt_prob(p):
    if p is None:
        return "-"
    return f"{round(float(p) * 100, 1)}%"


def fmt_num(x):
    if x is None:
        return "-"
    try:
        return f"{float(x):.2f}"
    except Exception:
        return str(x)


def badge_result(res):
    if not res:
        return pill("PENDING", "pill pend")
    if res == "WIN":
        return pill("WIN", "pill win")
    if res == "LOSS":
        return pill("LOSS", "pill loss")
    if res == "PENDING":
        return pill("PENDING", "pill pend")
    return pill(res, "pill pend")


def badge_status(status):
    if status in LIVE_STATUSES:
        return pill("LIVE", "pill live")
    if status == FINISHED_STATUS:
        return pill("FINISHED", "pill fin")
    return pill("UPCOMING", "pill upc")


def pick_line(item):
    if not item.get("market"):
        return '<div class="muted">Sin pick (falta strength/data)</div>'

    return f"""
    <div class="pickbox">
        <div class="pickhead">
            <b>{item['market']}</b> {badge_result(item.get("result"))}
        </div>
        <div class="pickmeta">
            prob <b>{fmt_prob(item.get('prob'))}</b> • fair <b>{fmt_num(item.get('fair'))}</b> • conf <b>{item.get('confidence','-')}</b><br/>
            <span class="muted">{item.get('result_reason','')}</span>
        </div>
    </div>
    """


def match_card(item, compact=True):
    local_dt = fmt_local_dt(item.get("utcDate", ""))
    status = item.get("status", "")
    score = ""
    if item.get("home_goals") is not None and item.get("away_goals") is not None:
        score = f"{item['home_goals']}-{item['away_goals']}"

    xg = ""
    if item.get("xg_home") is not None and item.get("xg_away") is not None:
        xg = f"xG {fmt_num(item.get('xg_home'))}-{fmt_num(item.get('xg_away'))} (tot {fmt_num(item.get('xg_total'))})"

    meta_line = f"{local_dt} • {xg}".strip(" •")

    return f"""
    <div class="card{' compact' if compact else ''}">
        <div class="rowtitle">
            <span class="teams">{item['home']} vs {item['away']}</span>
            {badge_status(status)}
            {pill(score, "pill score") if score else ""}
        </div>
        <div class="meta">{meta_line}</div>
        {pick_line(item)}
    </div>
    """


def league_select(current):
    options = []
    for code, name in LEAGUES.items():
        sel = "selected" if code == current else ""
        options.append(f'<option value="{code}" {sel}>{code} • {name}</option>')
    return f"""
    <select id="leagueSel" class="select" onchange="location.href='/?league='+this.value">
        {''.join(options)}
    </select>
    """


def filter_bar(league: str, view: str, res: str):
    # view: all | picks
    # res: ALL | PENDING | WIN | LOSS
    def a(label, v=None, r=None):
        v2 = v if v is not None else view
        r2 = r if r is not None else res
        active = "active" if (v2 == view and r2 == res) else ""
        return f'<a class="chip {active}" href="/?league={league}&view={v2}&res={r2}">{label}</a>'

    return f"""
    <div class="filterbar">
      {a("Todo", "all", "ALL")}
      {a("Solo picks", "picks", "ALL")}
      <span class="sep"></span>
      {a("Pending", view, "PENDING")}
      {a("WIN", view, "WIN")}
      {a("LOSS", view, "LOSS")}
    </div>
    """


def page_shell(title, inner, league):
    last = get_last_updated() or "n/a"

    admin_note = """
    <div class="adminnote">
      ⚡ <b>Forzar update</b>: <code>/refresh?key=TU_KEY</code> (requiere <code>REFRESH_KEY</code> en env).
      <span class="muted">Render: Settings → Environment → Add env var → REFRESH_KEY</span>
    </div>
    """

    return f"""
<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>{title}</title>
<style>
:root {{
  --bg:#0b1220;
  --card:#111a2e;
  --muted:#94a3b8;
  --line:#1f2a44;
  --acc:#7c3aed;
}}
*{{box-sizing:border-box}}
body {{
  margin:0; font-family: ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto;
  background:var(--bg); color:#e5e7eb;
}}
a{{color:#c4b5fd; text-decoration:none}}
.wrap{{max-width:1120px; margin:0 auto; padding:16px}}
.topbar{{display:flex; gap:12px; align-items:center; justify-content:space-between; margin-bottom:14px}}
.brand{{font-weight:900; letter-spacing:0.5px}}
.links{{display:flex; gap:12px; align-items:center}}
.muted{{color:var(--muted)}}
.hero{{background:linear-gradient(135deg,#111a2e,#0f2440); border:1px solid var(--line); padding:14px; border-radius:14px; margin-bottom:12px}}
.hero-title{{font-size:20px; font-weight:900}}
.controls{{display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-top:10px}}
.select{{background:#0b1730; border:1px solid var(--line); color:#e5e7eb; padding:8px 10px; border-radius:10px}}
.btn{{background:var(--acc); color:white; border:0; padding:8px 12px; border-radius:10px; font-weight:800; cursor:pointer}}
.grid {{
  display:grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap:10px;
}}
.card {{
  background:var(--card);
  border:1px solid var(--line);
  padding:10px;
  border-radius:12px;
}}
.card.compact {{ padding:10px; }}
.rowtitle {{
  font-size:14px;
  font-weight:900;
  margin-bottom:4px;
  display:flex;
  align-items:center;
  gap:8px;
  flex-wrap:wrap;
}}
.teams{{font-weight:900}}
.meta{{color:var(--muted); font-size:11px; margin-top:4px}}
.pill{{display:inline-block; padding:3px 8px; border-radius:999px; font-size:11px; border:1px solid var(--line); background:#0b1730}}
.pill.win{{border-color:#14532d; background:#052e16; color:#86efac}}
.pill.loss{{border-color:#7f1d1d; background:#450a0a; color:#fca5a5}}
.pill.pend{{border-color:#78350f; background:#2a1707; color:#fde68a}}
.pill.live{{border-color:#7f1d1d; background:#450a0a; color:#fecaca}}
.pill.fin{{border-color:#0f172a; background:#0b1730; color:#cbd5e1}}
.pill.upc{{border-color:#334155; background:#0b1730; color:#cbd5e1}}
.pill.score{{opacity:.95}}
.pickbox {{
  margin-top:8px;
  padding:10px;
  border-radius:12px;
  background:#0f2440;
  border:1px solid #223457;
}}
.pickhead{{font-size:12px}}
.pickmeta{{font-size:11px; margin-top:6px}}
.sectionTitle{{margin:14px 0 8px; font-size:13px; font-weight:900; color:#cbd5e1}}
hr{{border:0; border-top:1px solid var(--line); margin:14px 0}}
.adminnote{{margin-top:10px; padding:10px; border-radius:12px; border:1px dashed #334155; background:#0b1730; font-size:12px}}
.filterbar{{display:flex; gap:8px; flex-wrap:wrap; margin-top:10px}}
.chip{{border:1px solid var(--line); background:#0b1730; padding:6px 10px; border-radius:999px; font-size:12px; font-weight:800; color:#e5e7eb}}
.chip.active{{border-color:#a78bfa; box-shadow:0 0 0 2px rgba(167,139,250,.12) inset}}
.sep{{width:10px}}
.topPickWrap{{margin-top:12px}}
.topPickTitle{{font-weight:900; margin:2px 0 10px; color:#e2e8f0}}
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div class="brand">AFTR • MVP</div>
    <div class="links">
      <a href="/?league={league}">Dashboard</a>
      <a href="/stats">Stats</a>
      <a href="/docs">Docs</a>
    </div>
  </div>

  <div class="hero">
    <div class="hero-title">{title}</div>
    <div class="muted">Última actualización: <b>{last}</b> • Horario: <b>Argentina (-03)</b></div>
    <div class="controls">
      {league_select(league)}
      <button class="btn" onclick="refreshNow()">⚡ Refresh</button>
      <span class="muted">Si no tenés key, te va a decir Unauthorized.</span>
    </div>
    {admin_note}
  </div>

  {inner}

</div>

<script>
async function refreshNow() {{
  const key = prompt("REFRESH key?");
  if(!key) return;
  const r = await fetch(`/refresh?key=${{encodeURIComponent(key)}}`);
  const j = await r.json();
  alert(JSON.stringify(j));
  location.reload();
}}
</script>
</body>
</html>
"""


# =========================
# ROUTES
# =========================
@app.get("/", response_class=HTMLResponse)
def dashboard(league: str = DEFAULT_LEAGUE, view: str = "all", res: str = "ALL"):
    if league not in LEAGUES:
        league = DEFAULT_LEAGUE

    items = fetch_all_for_league(league)
    live, upcoming, recent = split_sections(items)

    # filtros
    def apply_filters(lst):
        out = lst
        if view == "picks":
            out = [x for x in out if x.get("market")]
        if res in {"WIN", "LOSS", "PENDING"}:
            out = [x for x in out if (x.get("result") or "PENDING") == res]
        return out

    live_f = apply_filters(live)
    upcoming_f = apply_filters(upcoming)
    recent_f = apply_filters(recent)

    # TOP PICK: elegimos el mejor de UPCOMING (con pick) por prob
    top_pick = None
    pool = [x for x in upcoming if x.get("market") and x.get("prob") is not None]
    if pool:
        top_pick = sorted(pool, key=lambda x: float(x.get("prob", 0.0)), reverse=True)[0]

    inner = ""
    inner += filter_bar(league, view, res)

    # Top Pick
    if top_pick:
        inner += f"""
        <div class="topPickWrap">
          <div class="topPickTitle">⭐ TOP PICK (mejor probabilidad en próximos)</div>
          <div class="grid">
            {match_card(top_pick, compact=False)}
          </div>
        </div>
        <hr/>
        """

    # Secciones
    def render_section(title, lst):
        if not lst:
            return f"<div class='muted'>No hay datos en {title} con esos filtros.</div>"
        return f"""
        <div class="sectionTitle">{title}</div>
        <div class="grid">
          {''.join([match_card(it, compact=True) for it in lst])}
        </div>
        """

    inner += render_section("🔴 LIVE", live_f)
    inner += "<hr/>"
    inner += render_section("🗓️ UPCOMING", upcoming_f)
    inner += "<hr/>"
    inner += render_section("🧾 RECENT (últimos 60)", recent_f)

    return page_shell("AFTR Dashboard", inner, league)


@app.get("/api/stats")
def api_stats():
    if not os.path.exists(DB_FILE):
        return {"error": "DB not found"}

    with db_connect() as con:
        cur = con.cursor()

        cur.execute("SELECT COUNT(*) FROM picks")
        total_all = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM picks WHERE result='WIN'")
        wins = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM picks WHERE result='LOSS'")
        losses = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM picks WHERE result='PENDING'")
        pending = cur.fetchone()[0]

        decided = wins + losses
        winrate = round((wins / decided) * 100, 2) if decided > 0 else 0

    return {
        "total_picks": total_all,
        "wins": wins,
        "losses": losses,
        "pending": pending,
        "winrate": winrate
    }


@app.get("/stats", response_class=HTMLResponse)
def stats_page():
    if not os.path.exists(DB_FILE):
        raise HTTPException(status_code=500, detail="DB not found")

    with db_connect() as con:
        cur = con.cursor()

        cur.execute("SELECT COUNT(*) FROM picks")
        total_all = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM picks WHERE result='WIN'")
        wins = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM picks WHERE result='LOSS'")
        losses = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM picks WHERE result='PENDING'")
        pending = cur.fetchone()[0]

        decided = wins + losses
        winrate = round((wins / decided) * 100, 2) if decided > 0 else 0

        cur.execute("""
            SELECT league,
                   SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as w,
                   SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END) as l,
                   SUM(CASE WHEN result='PENDING' THEN 1 ELSE 0 END) as p
            FROM picks
            GROUP BY league
            ORDER BY (w + l) DESC
        """)
        by_league = cur.fetchall()

    breakdown_html = ""
    for lg, w, l, p in by_league:
        decided_l = (w or 0) + (l or 0)
        wr = round(((w or 0) / decided_l) * 100, 2) if decided_l > 0 else 0
        breakdown_html += f"<div style='margin:6px 0;'><b>{lg}</b> — ✅ {w} / ❌ {l} / ⏳ {p} • <span class='muted'>WR {wr}%</span></div>"

    inner = f"""
    <div class="grid">
        <div class="card">
            <div class="rowtitle">📌 Picks totales</div>
            <div style="font-size:28px;font-weight:900;">{total_all}</div>
            <div class="muted">Incluye pending</div>
        </div>

        <div class="card">
            <div class="rowtitle">✅ Wins</div>
            <div style="font-size:28px;font-weight:900;color:#86efac;">{wins}</div>
            <div class="muted">Resultados positivos</div>
        </div>

        <div class="card">
            <div class="rowtitle">❌ Losses</div>
            <div style="font-size:28px;font-weight:900;color:#fca5a5;">{losses}</div>
            <div class="muted">Resultados negativos</div>
        </div>

        <div class="card">
            <div class="rowtitle">⏳ Pending</div>
            <div style="font-size:28px;font-weight:900;color:#fde68a;">{pending}</div>
            <div class="muted">Todavía no terminó</div>
        </div>

        <div class="card" style="grid-column: span 2;">
            <div class="rowtitle">📈 Winrate (decididos)</div>
            <div style="font-size:34px;font-weight:900;">{winrate}%</div>
            <div class="muted">Decididos: {decided} • (wins {wins} / {decided})</div>
        </div>

        <div class="card" style="grid-column: span 2;">
            <div class="rowtitle">🌍 Breakdown por liga</div>
            <div class="muted" style="margin-top:6px;">{breakdown_html}</div>
        </div>
    </div>
    """

    return page_shell("AFTR Stats", inner, DEFAULT_LEAGUE)


@app.get("/refresh")
def refresh(key: str = ""):
    if not REFRESH_KEY:
        raise HTTPException(status_code=401, detail="REFRESH_KEY no está seteada como variable de entorno.")
    if key != REFRESH_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        subprocess.run([os.sys.executable, "team_strength.py"], check=True)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Refresh failed: {e}")

    return {"ok": True, "msg": "SQLite actualizado"}


# =========================
# AUTO REFRESH THREAD
# =========================
def _auto_refresh_loop():
    while True:
        time.sleep(max(60, REFRESH_EVERY_MIN * 60))
        try:
            subprocess.run([os.sys.executable, "team_strength.py"], check=True)
            print("✅ Auto-refresh OK")
        except Exception as e:
            print(f"⚠️ Auto-refresh failed: {e}")


@app.on_event("startup")
def startup_event():
    if AUTO_REFRESH:
        t = threading.Thread(target=_auto_refresh_loop, daemon=True)
        t.start()
        print("✅ Auto-refresh thread started.")






