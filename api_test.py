import os
import json
import subprocess
import threading
import time
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse


APP_TITLE = "AFTR MVP"
DEFAULT_LEAGUE = "PL"

# Argentina UTC-3
LOCAL_TZ = timezone(timedelta(hours=-3))

# Refresh settings
AUTO_REFRESH = os.getenv("AUTO_REFRESH", "1") == "1"
REFRESH_EVERY_MIN = int(os.getenv("REFRESH_EVERY_MIN", "15"))
REFRESH_KEY = os.getenv("REFRESH_KEY", "").strip()

# Optional: link de Telegram en el dashboard
TELEGRAM_LINK = os.getenv("TELEGRAM_LINK", "").strip()

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

# football-data statuses (y usamos nuestros defaults si falta)
LIVE_STATUSES = {"IN_PLAY", "PAUSED", "LIVE"}
UPCOMING_STATUSES = {"SCHEDULED", "TIMED", "UPCOMING"}
FINISHED_STATUS = "FINISHED"

app = FastAPI(title=APP_TITLE)


# =========================
# Time utils
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


def fmt_num(x):
    if x is None:
        return "-"
    try:
        return f"{float(x):.2f}"
    except Exception:
        return str(x)


def fmt_prob(p):
    if p is None:
        return "-"
    try:
        return f"{round(float(p) * 100, 1)}%"
    except Exception:
        return "-"


# =========================
# ID normalization (CLAVE)
# =========================
def make_composite_id(home: str, away: str, utcDate: str):
    # id estable aunque no venga match_id (caso Render)
    home = (home or "?").strip()
    away = (away or "?").strip()
    utcDate = (utcDate or "?").strip()
    return f"{home}__{away}__{utcDate}"


def get_match_id(m: dict):
    # soporta todos los formatos que vimos
    mid = (
        m.get("match_id")
        or m.get("matchId")
        or m.get("id")
        or m.get("fixture", {}).get("id")
    )
    return mid


# =========================
# JSON normalization
# =========================
def norm_team_name(x):
    if isinstance(x, str):
        return x
    if isinstance(x, dict):
        return x.get("name") or x.get("shortName") or "?"
    return "?"


def norm_date(m: dict):
    return m.get("utcDate") or m.get("date") or m.get("fixture", {}).get("date") or ""


def norm_status(m: dict):
    st = (
        m.get("status")
        or m.get("fixture", {}).get("status", {}).get("short")
        or m.get("state")
        or ""
    )
    return st


def norm_score(m: dict):
    # formato "plano" que venías guardando
    hg = m.get("home_goals", None)
    ag = m.get("away_goals", None)
    if hg is not None or ag is not None:
        return hg, ag

    # football-data raw: score.fullTime.home/away
    sc = m.get("score", {}) or {}
    ft = sc.get("fullTime", {}) or {}
    hg = ft.get("home")
    ag = ft.get("away")
    if hg is not None or ag is not None:
        return hg, ag

    # api-football: goals.home/away
    goals = m.get("goals")
    if isinstance(goals, dict):
        return goals.get("home"), goals.get("away")

    return None, None


def norm_xg(m: dict):
    # Render sample trae xg_* directo
    xh = m.get("xg_home", m.get("xG_home"))
    xa = m.get("xg_away", m.get("xG_away"))
    xt = m.get("xg_total", m.get("xG_total"))
    if xt is None and xh is not None and xa is not None:
        try:
            xt = float(xh) + float(xa)
        except Exception:
            pass
    return xh, xa, xt


def normalize_match(m: dict):
    utcDate = norm_date(m)
    status = norm_status(m)

    home = (
        m.get("home")
        or norm_team_name(m.get("homeTeam"))
        or norm_team_name(m.get("teams", {}).get("home"))
    )
    away = (
        m.get("away")
        or norm_team_name(m.get("awayTeam"))
        or norm_team_name(m.get("teams", {}).get("away"))
    )

    mid = get_match_id(m)
    if mid is None:
        mid = make_composite_id(home, away, utcDate)

    hg, ag = norm_score(m)
    xh, xa, xt = norm_xg(m)

    # Si no viene status, lo inferimos por fecha/score (caso Render)
    if not status:
        dt = safe_parse_dt(utcDate)
        if dt:
            now = datetime.now(timezone.utc)
            if hg is not None and ag is not None:
                status = FINISHED_STATUS
            elif dt <= now:
                # si ya pasó pero no tenemos FT, lo tratamos como LIVE/IN_PLAY? (suave)
                status = "TIMED"
            else:
                status = "TIMED"
        else:
            status = "TIMED"

    return {
        "match_id": str(mid),
        "utcDate": utcDate,
        "status": status,
        "home": home,
        "away": away,
        "home_goals": hg,
        "away_goals": ag,
        "xg_home": xh,
        "xg_away": xa,
        "xg_total": xt,
        # probs si existen (Render sample)
        "probs": m.get("probs") if isinstance(m.get("probs"), dict) else None,
    }


# =========================
# Load matches + picks
# =========================
def load_json_league(league: str):
    mf = f"daily_matches_{league}.json"
    pf = f"daily_picks_{league}.json"

    if not os.path.exists(mf):
        return []

    try:
        with open(mf, "r", encoding="utf-8") as f:
            raw_matches = json.load(f)
        if not isinstance(raw_matches, list):
            return []
    except Exception:
        return []

    # picks indexados por:
    # 1) match_id string si existe
    # 2) fallback composite home/away/utcDate si no existe id
    picks_by_id = {}
    picks_by_comp = {}

    if os.path.exists(pf):
        try:
            with open(pf, "r", encoding="utf-8") as f:
                raw_picks = json.load(f)
            if isinstance(raw_picks, list):
                for p in raw_picks:
                    # match_id si viene
                    pid = p.get("match_id") or p.get("matchId") or p.get("id") or p.get("fixture_id")
                    if pid is not None:
                        picks_by_id[str(pid)] = p
                        continue

                    # fallback: si el pick trae home/away/utcDate
                    comp = make_composite_id(p.get("home"), p.get("away"), p.get("utcDate"))
                    picks_by_comp[comp] = p
        except Exception:
            pass

    out = []
    for m in raw_matches:
        nm = normalize_match(m)
        mid = nm["match_id"]

        # 1) intentamos por ID
        p = picks_by_id.get(mid)

        # 2) si no hay, intentamos composite
        if not p:
            comp = make_composite_id(nm["home"], nm["away"], nm["utcDate"])
            p = picks_by_comp.get(comp)

        # attach pick fields
        nm["market"] = (p or {}).get("market")
        nm["prob"] = (p or {}).get("prob")
        nm["fair"] = (p or {}).get("fair")
        nm["confidence"] = (p or {}).get("confidence")
        nm["result"] = (p or {}).get("result") or "PENDING"
        nm["result_reason"] = (p or {}).get("result_reason", "")

        out.append(nm)

    return out


# =========================
# UI helpers
# =========================
def pill(text, cls="pill"):
    return f'<span class="{cls}">{text}</span>'


def badge_status(status):
    if status in LIVE_STATUSES:
        return pill("LIVE", "pill live")
    if status == FINISHED_STATUS:
        return pill("FINISHED", "pill fin")
    return pill("UPCOMING", "pill upc")


def badge_result(res):
    res = res or "PENDING"
    if res == "WIN":
        return pill("WIN", "pill win")
    if res == "LOSS":
        return pill("LOSS", "pill loss")
    return pill("PENDING", "pill pend")


def pick_line(item):
    if not item.get("market"):
        return '<div class="muted">Sin pick (todavía)</div>'

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


def split_sections(items):
    live = [x for x in items if x.get("status") in LIVE_STATUSES]
    upcoming = [x for x in items if x.get("status") in UPCOMING_STATUSES]
    recent = [x for x in items if x.get("status") == FINISHED_STATUS]

    # si algunos vienen como TIMED siempre, al menos ordenamos por fecha
    live.sort(key=lambda x: x.get("utcDate") or "")
    upcoming.sort(key=lambda x: x.get("utcDate") or "")
    recent.sort(key=lambda x: x.get("utcDate") or "", reverse=True)
    recent = recent[:60]

    return live, upcoming, recent


def page_shell(title, inner, league):
    tg = ""
    if TELEGRAM_LINK:
        tg = f'<a class="chip" href="{TELEGRAM_LINK}" target="_blank">💬 Telegram</a>'

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
.links{{display:flex; gap:12px; align-items:center; flex-wrap:wrap}}
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
.card.compact{{padding:10px}}
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
.chip{{border:1px solid var(--line); background:#0b1730; padding:6px 10px; border-radius:999px; font-size:12px; font-weight:800; color:#e5e7eb}}
.topPickTitle{{font-weight:900; margin:2px 0 10px; color:#e2e8f0}}
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div class="brand">AFTR • MVP</div>
    <div class="links">
      <a class="chip" href="/?league={league}">Dashboard</a>
      <a class="chip" href="/stats">Stats</a>
      <a class="chip" href="/docs">Docs</a>
      {tg}
    </div>
  </div>

  <div class="hero">
    <div class="hero-title">{title}</div>
    <div class="muted">Liga: <b>{league} • {LEAGUES.get(league,"")}</b> • Horario <b>AR (-03)</b></div>
    <div class="controls">
      {league_select(league)}
      <button class="btn" onclick="refreshNow()">⚡ Refresh</button>
      <span class="muted">Si no tenés key → Unauthorized (normal).</span>
    </div>
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
# Routes
# =========================
@app.get("/", response_class=HTMLResponse)
def dashboard(league: str = DEFAULT_LEAGUE):
    if league not in LEAGUES:
        league = DEFAULT_LEAGUE

    items = load_json_league(league)
    live, upcoming, recent = split_sections(items)

    # TOP PICK: mejor prob (si hay picks)
    top_pick = None
    pool = [x for x in upcoming if x.get("market") and x.get("prob") is not None]
    if pool:
        try:
            top_pick = sorted(pool, key=lambda x: float(x.get("prob", 0.0)), reverse=True)[0]
        except Exception:
            top_pick = pool[0]

    inner = ""
    if top_pick:
        inner += f"""
        <div class="topPickTitle">⭐ TOP PICK</div>
        <div class="grid">
            {match_card(top_pick, compact=False)}
        </div>
        <hr/>
        """

    def section(title, lst):
        if not lst:
            return f"<div class='muted'>No hay datos en {title}.</div>"
        return f"""
        <div class="sectionTitle">{title}</div>
        <div class="grid">
          {''.join([match_card(it, compact=True) for it in lst])}
        </div>
        """

    inner += section("🔴 LIVE", live)
    inner += "<hr/>"
    inner += section("🗓️ UPCOMING", upcoming)
    inner += "<hr/>"
    inner += section("🧾 RECENT (últimos 60)", recent)

    return page_shell("AFTR Dashboard", inner, league)


@app.get("/api/stats")
def api_stats():
    total = wins = losses = pending = 0
    for lg in LEAGUES.keys():
        pf = f"daily_picks_{lg}.json"
        if os.path.exists(pf):
            try:
                with open(pf, "r", encoding="utf-8") as f:
                    picks = json.load(f)
                if isinstance(picks, list):
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
    inner = f"""
    <div class="grid">
        <div class="card"><div class="rowtitle">📌 Picks totales</div><div style="font-size:28px;font-weight:900;">{s['total_picks']}</div><div class="muted">Incluye pending</div></div>
        <div class="card"><div class="rowtitle">✅ Wins</div><div style="font-size:28px;font-weight:900;color:#86efac;">{s['wins']}</div></div>
        <div class="card"><div class="rowtitle">❌ Losses</div><div style="font-size:28px;font-weight:900;color:#fca5a5;">{s['losses']}</div></div>
        <div class="card"><div class="rowtitle">⏳ Pending</div><div style="font-size:28px;font-weight:900;color:#fde68a;">{s['pending']}</div></div>
        <div class="card" style="grid-column: span 2;"><div class="rowtitle">📈 Winrate (decididos)</div><div style="font-size:34px;font-weight:900;">{s['winrate']}%</div></div>
    </div>
    """
    return page_shell("AFTR Stats", inner, DEFAULT_LEAGUE)


@app.get("/docs", response_class=HTMLResponse)
def docs_page():
    inner = f"""
    <div class="card">
      <div class="rowtitle">📚 Endpoints</div>
      <div class="muted" style="margin-top:8px; line-height:1.6;">
        • <b>/</b> Dashboard<br/>
        • <b>/stats</b> Stats<br/>
        • <b>/api/stats</b> Stats JSON<br/>
        • <b>/api/debug?league=PL</b> Debug JSON<br/>
        • <b>/refresh?key=...</b> fuerza update (requiere <b>REFRESH_KEY</b>)<br/>
        <hr/>
        <b>Auto-refresh</b>: cada {REFRESH_EVERY_MIN} min (si <b>AUTO_REFRESH=1</b>).<br/>
      </div>
    </div>
    """
    return page_shell("AFTR Docs", inner, DEFAULT_LEAGUE)


@app.get("/api/debug")
def api_debug(league: str = "PL"):
    mf = f"daily_matches_{league}.json"
    pf = f"daily_picks_{league}.json"

    info = {
        "cwd": os.getcwd(),
        "matches_file": mf,
        "picks_file": pf,
        "matches_exists": os.path.exists(mf),
        "picks_exists": os.path.exists(pf),
        "matches_size": os.path.getsize(mf) if os.path.exists(mf) else 0,
        "picks_size": os.path.getsize(pf) if os.path.exists(pf) else 0,
    }

    sample = None
    sample_norm = None

    if os.path.exists(mf):
        try:
            with open(mf, "r", encoding="utf-8") as f:
                data = json.load(f)
            info["matches_count"] = len(data) if isinstance(data, list) else "not_list"
            if isinstance(data, list) and data:
                sample = data[0]
                sample_norm = normalize_match(sample)
        except Exception as e:
            info["matches_read_error"] = str(e)

    return {"info": info, "sample_first_match": sample, "sample_normalized": sample_norm}


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
    # seed local (si faltan JSON)
    try:
        if not os.path.exists("daily_matches_PL.json"):
            subprocess.run([os.sys.executable, "team_strength.py"], check=True)
    except Exception as e:
        print(f"⚠️ Seed startup failed: {e}")

    if AUTO_REFRESH:
        t = threading.Thread(target=_auto_refresh_loop, daemon=True)
        t.start()
        print("✅ Auto-refresh thread started.")







