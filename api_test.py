import os
import sqlite3
import subprocess
import threading
import time
import json
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse


APP_TITLE = "AFTR MVP"

# Si no seteás nada, usa aftr.db en la carpeta actual.
DB_FILE = os.getenv("AFTR_DB_FILE", "aftr.db")

# Argentina UTC-3
LOCAL_TZ = timezone(timedelta(hours=-3))

# Auto-refresh (ojo: en Render Free se duerme, por eso conviene cron externo)
AUTO_REFRESH = os.getenv("AUTO_REFRESH", "1") == "1"
REFRESH_EVERY_MIN = int(os.getenv("REFRESH_EVERY_MIN", "15"))

# Para /refresh
REFRESH_KEY = os.getenv("REFRESH_KEY", "").strip()

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
# Utils time
# =========================
def safe_parse_dt(utc_iso: str):
    if not utc_iso:
        return None
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


# =========================
# DB helpers (OPCIONAL)
# =========================
def db_connect():
    return sqlite3.connect(DB_FILE, check_same_thread=False)


def row_to_item(r):
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


def get_last_updated():
    # 1) DB meta (si existe)
    if os.path.exists(DB_FILE):
        try:
            with db_connect() as con:
                cur = con.cursor()
                cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = {t[0] for t in cur.fetchall()}
                if "meta" in tables:
                    cur.execute("SELECT value FROM meta WHERE key='last_updated'")
                    row = cur.fetchone()
                    if row:
                        return row[0]
        except Exception:
            pass

    # 2) fallback: mtime del JSON más nuevo
    newest = None
    for code in LEAGUES.keys():
        fn = f"daily_matches_{code}.json"
        if os.path.exists(fn):
            ts = os.path.getmtime(fn)
            newest = ts if newest is None else max(newest, ts)

    if newest is None:
        return None

    dt = datetime.fromtimestamp(newest, tz=LOCAL_TZ)
    return dt.isoformat()


# =========================
# JSON fallback
# =========================
def _norm_team_name(x):
    # acepta string o dict
    if isinstance(x, str):
        return x
    if isinstance(x, dict):
        return x.get("name") or x.get("shortName") or x.get("team") or x.get("id") or "?"
    return "?"

def _norm_status(m):
    return m.get("status") or m.get("fixture", {}).get("status", {}).get("short") or m.get("state") or ""

def _norm_date(m):
    # acepta utcDate o date o fixture.date
    return m.get("utcDate") or m.get("date") or m.get("fixture", {}).get("date") or ""

def _norm_score(m):
    # football-data: score.fullTime.home/away
    sc = m.get("score", {}) or {}
    ft = sc.get("fullTime", {}) or {}
    hg = m.get("home_goals", None)
    ag = m.get("away_goals", None)
    if hg is None and ag is None:
        hg = ft.get("home")
        ag = ft.get("away")
    # API-Football style fallback:
    if hg is None and ag is None:
        goals = m.get("goals") or m.get("score", {}).get("goals")
        if isinstance(goals, dict):
            hg = goals.get("home")
            ag = goals.get("away")
    return hg, ag

def _norm_xg(m):
    # intenta varios nombres
    xh = m.get("xg_home", m.get("xG_home"))
    xa = m.get("xg_away", m.get("xG_away"))
    xt = m.get("xg_total", m.get("xG_total"))
    if xt is None and (xh is not None and xa is not None):
        try:
            xt = float(xh) + float(xa)
        except Exception:
            pass
    return xh, xa, xt

def load_json_league(league: str):
    mf = f"daily_matches_{league}.json"
    pf = f"daily_picks_{league}.json"

    if not os.path.exists(mf):
        return []

    try:
        with open(mf, "r", encoding="utf-8") as f:
            matches = json.load(f)
    except Exception:
        return []

    # picks opcional
    picks_by_match = {}
    if os.path.exists(pf):
        try:
            with open(pf, "r", encoding="utf-8") as f:
                picks = json.load(f)
            for p in picks:
                mid = p.get("match_id") or p.get("id") or p.get("fixture_id")
                if mid is not None:
                    picks_by_match[str(mid)] = p
        except Exception:
            pass

    out = []
    for m in matches:
        # match_id puede venir como match_id o id o fixture.id
        mid = m.get("match_id")
        if mid is None:
            mid = m.get("id")
        if mid is None:
            mid = m.get("fixture", {}).get("id")

        # normalizo a string para mapear con picks
        mid_key = str(mid) if mid is not None else None
        p = picks_by_match.get(mid_key, {}) if mid_key else {}

        # home/away pueden venir como strings o dicts
        home = m.get("home") or _norm_team_name(m.get("homeTeam")) or _norm_team_name(m.get("teams", {}).get("home"))
        away = m.get("away") or _norm_team_name(m.get("awayTeam")) or _norm_team_name(m.get("teams", {}).get("away"))

        status = _norm_status(m)
        utcDate = _norm_date(m)
        hg, ag = _norm_score(m)
        xh, xa, xt = _norm_xg(m)

        out.append({
            "match_id": mid,
            "utcDate": utcDate,
            "status": status,
            "home": home,
            "away": away,
            "home_goals": hg,
            "away_goals": ag,
            "xg_home": xh,
            "xg_away": xa,
            "xg_total": xt,
            "market": p.get("market"),
            "prob": p.get("prob"),
            "fair": p.get("fair"),
            "confidence": p.get("confidence"),
            "result": p.get("result") or "PENDING",
            "result_reason": p.get("result_reason", ""),
        })

    return out



def fetch_all_for_league(league: str):
    """
    1) Intento DB si existe y tiene las tablas.
    2) Si falla o está vacía -> JSON fallback SIEMPRE.
    """
    if os.path.exists(DB_FILE):
        try:
            with db_connect() as con:
                cur = con.cursor()
                cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = {t[0] for t in cur.fetchall()}

                if {"matches", "picks"}.issubset(tables):
                    cur.execute("""
                        SELECT m.match_id, m.utcDate, m.status, m.home, m.away, m.home_goals, m.away_goals,
                               m.xg_home, m.xg_away, m.xg_total,
                               p.market, p.prob, p.fair, p.confidence, p.result, p.result_reason
                        FROM matches m
                        LEFT JOIN picks p ON p.league=m.league AND p.match_id=m.match_id
                        WHERE m.league=?
                        ORDER BY m.utcDate ASC
                    """, (league,))
                    rows = cur.fetchall()
                    if rows:
                        return [row_to_item(r) for r in rows]
        except Exception:
            pass

    return load_json_league(league)


# =========================
# UI helpers
# =========================
def fmt_prob(p):
    if p is None:
        return "-"
    try:
        return f"{round(float(p) * 100, 1)}%"
    except Exception:
        return "-"


def fmt_num(x):
    if x is None:
        return "-"
    try:
        return f"{float(x):.2f}"
    except Exception:
        return str(x)


def pill(text, cls="pill"):
    return f'<span class="{cls}">{text}</span>'


def badge_result(res):
    res = res or "PENDING"
    if res == "WIN":
        return pill("WIN", "pill win")
    if res == "LOSS":
        return pill("LOSS", "pill loss")
    return pill("PENDING", "pill pend")


def badge_status(status):
    if status in LIVE_STATUSES:
        return pill("LIVE", "pill live")
    if status == FINISHED_STATUS:
        return pill("FINISHED", "pill fin")
    return pill("UPCOMING", "pill upc")


def pick_line(item):
    if not item.get("market"):
        return '<div class="muted">Sin pick</div>'

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
            <span class="teams">{item.get('home','?')} vs {item.get('away','?')}</span>
            {badge_status(status)}
            {pill(score, "pill score") if score else ""}
        </div>
        <div class="meta">{meta_line}</div>
        {pick_line(item)}
    </div>
    """


def league_select(current):
    opts = []
    for code, name in LEAGUES.items():
        sel = "selected" if code == current else ""
        opts.append(f'<option value="{code}" {sel}>{code} • {name}</option>')
    return f"""
    <select id="leagueSel" class="select" onchange="location.href='/?league='+this.value">
        {''.join(opts)}
    </select>
    """


def filter_bar(league: str, view: str, res: str):
    def chip(label, v=None, r=None):
        v2 = v if v is not None else view
        r2 = r if r is not None else res
        active = "active" if (v2 == view and r2 == res) else ""
        return f'<a class="chip {active}" href="/?league={league}&view={v2}&res={r2}">{label}</a>'

    return f"""
    <div class="filterbar">
      {chip("Todo", "all", "ALL")}
      {chip("Solo picks", "picks", "ALL")}
      <span class="sep"></span>
      {chip("Pending", view, "PENDING")}
      {chip("WIN", view, "WIN")}
      {chip("LOSS", view, "LOSS")}
    </div>
    """


def admin_block(admin: int):
    if admin != 1:
        return ""
    return """
    <div class="adminnote">
      ⚡ <b>Forzar update</b>: <code>/refresh?key=TU_KEY</code> (requiere <code>REFRESH_KEY</code> en env).
      <span class="muted">Render: Settings → Environment → Add env var → REFRESH_KEY</span>
    </div>
    """


def page_shell(title, inner, league, admin: int):
    last = get_last_updated() or "n/a"
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
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap:10px;
}}
.card {{
  background:var(--card);
  border:1px solid var(--line);
  padding:10px;
  border-radius:12px;
}}
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
    {admin_block(admin)}
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


def split_sections(items):
    live = [x for x in items if x.get("status") in LIVE_STATUSES]
    upcoming = [x for x in items if x.get("status") in UPCOMING_STATUSES]
    recent = [x for x in items if x.get("status") == FINISHED_STATUS]

    live.sort(key=lambda x: x.get("utcDate") or "")
    upcoming.sort(key=lambda x: x.get("utcDate") or "")
    recent.sort(key=lambda x: x.get("utcDate") or "", reverse=True)
    recent = recent[:60]

    return live, upcoming, recent


# =========================
# ROUTES
# =========================
@app.get("/", response_class=HTMLResponse)
def dashboard(league: str = DEFAULT_LEAGUE, view: str = "all", res: str = "ALL", admin: int = 0):
    if league not in LEAGUES:
        league = DEFAULT_LEAGUE

    items = fetch_all_for_league(league)
    live, upcoming, recent = split_sections(items)

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

    # TOP PICK: mejor prob en próximos
    top_pick = None
    pool = [x for x in upcoming if x.get("market") and x.get("prob") is not None]
    if pool:
        top_pick = sorted(pool, key=lambda x: float(x.get("prob", 0.0)), reverse=True)[0]

    inner = filter_bar(league, view, res)

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

    return page_shell("AFTR Dashboard", inner, league, admin)


@app.get("/api/stats")
def api_stats():
    # Si hay DB y tabla picks -> stats DB
    if os.path.exists(DB_FILE):
        try:
            with db_connect() as con:
                cur = con.cursor()
                cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = {t[0] for t in cur.fetchall()}
                if "picks" in tables:
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
        except Exception:
            pass

    # JSON fallback
    total = wins = losses = pending = 0
    for lg in LEAGUES.keys():
        fn = f"daily_picks_{lg}.json"
        if os.path.exists(fn):
            try:
                with open(fn, "r", encoding="utf-8") as f:
                    picks = json.load(f)
                for p in picks:
                    total += 1
                    r = (p.get("result") or "PENDING")
                    if r == "WIN":
                        wins += 1
                    elif r == "LOSS":
                        losses += 1
                    else:
                        pending += 1
            except Exception:
                pass

    decided = wins + losses
    winrate = round((wins / decided) * 100, 2) if decided > 0 else 0
    return {"total_picks": total, "wins": wins, "losses": losses, "pending": pending, "winrate": winrate}


@app.get("/stats", response_class=HTMLResponse)
def stats_page():
    s = api_stats()
    total_all = s.get("total_picks", 0)
    wins = s.get("wins", 0)
    losses = s.get("losses", 0)
    pending = s.get("pending", 0)
    winrate = s.get("winrate", 0)
    decided = wins + losses

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
            <div class="muted">Decididos: {decided} • wins {wins}</div>
        </div>
    </div>
    """
    return page_shell("AFTR Stats", inner, DEFAULT_LEAGUE, 0)


@app.get("/docs", response_class=HTMLResponse)
def docs_page():
    inner = """
    <div class="card">
      <div class="rowtitle">📚 Endpoints</div>
      <div class="muted" style="margin-top:8px; line-height:1.6;">
        • <b>/</b> Dashboard<br/>
        • <b>/stats</b> Stats bonito<br/>
        • <b>/api/stats</b> JSON stats<br/>
        • <b>/refresh?key=...</b> fuerza update (requiere REFRESH_KEY)<br/>
      </div>
    </div>
    """
    return page_shell("AFTR Docs", inner, DEFAULT_LEAGUE, 0)


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

    return {"ok": True, "msg": "JSON actualizados"}


# =========================
# AUTO REFRESH THREAD (local / mientras esté despierto)
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
    # Seed: si NO hay JSON para PL, intenta generar 1 vez al boot
    try:
        if not os.path.exists("daily_matches_PL.json"):
            subprocess.run([os.sys.executable, "team_strength.py"], check=True)
    except Exception as e:
        print(f"⚠️ Seed startup failed: {e}")

    if AUTO_REFRESH:
        t = threading.Thread(target=_auto_refresh_loop, daemon=True)
        t.start()
        print("✅ Auto-refresh thread started.")

@app.get("/api/debug")
def api_debug(league: str = "PL"):
    mf = f"daily_matches_{league}.json"
    pf = f"daily_picks_{league}.json"

    info = {
        "cwd": os.getcwd(),
        "db_file": DB_FILE,
        "matches_file": mf,
        "picks_file": pf,
        "matches_exists": os.path.exists(mf),
        "picks_exists": os.path.exists(pf),
        "matches_size": os.path.getsize(mf) if os.path.exists(mf) else 0,
        "picks_size": os.path.getsize(pf) if os.path.exists(pf) else 0,
    }

    sample = None
    if os.path.exists(mf):
        try:
            with open(mf, "r", encoding="utf-8") as f:
                data = json.load(f)
            info["matches_count"] = len(data) if isinstance(data, list) else "not_list"
            sample = data[0] if isinstance(data, list) and data else None
        except Exception as e:
            info["matches_read_error"] = str(e)

    return {"info": info, "sample_first_match": sample}






