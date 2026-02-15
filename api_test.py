import os
import json
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse


APP_TITLE = "AFTR MVP"

# Argentina UTC-3
LOCAL_TZ = timezone(timedelta(hours=-3))

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

# football-data statuses
LIVE_STATUSES = {"IN_PLAY", "PAUSED"}
UPCOMING_STATUSES = {"SCHEDULED", "TIMED"}
FINISHED_STATUS = "FINISHED"

# auto refresh (solo ayuda si el server está despierto)
AUTO_REFRESH = os.getenv("AUTO_REFRESH", "1") == "1"
REFRESH_EVERY_MIN = int(os.getenv("REFRESH_EVERY_MIN", "15"))

# refresh endpoint key
REFRESH_KEY = os.getenv("REFRESH_KEY", "").strip()

app = FastAPI(title=APP_TITLE)


# -------------------------
# time helpers
# -------------------------
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


# -------------------------
# JSON loader (robusto)
# -------------------------
def _norm_team_name(x):
    if isinstance(x, str):
        return x
    if isinstance(x, dict):
        return x.get("name") or x.get("shortName") or "?"
    return "?"


def _norm_status(m):
    return m.get("status") or m.get("fixture", {}).get("status", {}).get("short") or ""


def _norm_date(m):
    return m.get("utcDate") or m.get("date") or m.get("fixture", {}).get("date") or ""


def _norm_score(m):
    # Preferimos nuestros JSON “planos”
    hg = m.get("home_goals", None)
    ag = m.get("away_goals", None)
    if hg is not None or ag is not None:
        return hg, ag

    # football-data raw fallback
    sc = m.get("score", {}) or {}
    ft = sc.get("fullTime", {}) or {}
    hg = ft.get("home")
    ag = ft.get("away")
    if hg is not None or ag is not None:
        return hg, ag

    # api-football fallback
    goals = m.get("goals")
    if isinstance(goals, dict):
        return goals.get("home"), goals.get("away")

    return None, None


def _norm_xg(m):
    xh = m.get("xg_home", m.get("xG_home"))
    xa = m.get("xg_away", m.get("xG_away"))
    xt = m.get("xg_total", m.get("xG_total"))
    if xt is None and xh is not None and xa is not None:
        try:
            xt = float(xh) + float(xa)
        except Exception:
            pass
    return xh, xa, xt


def load_json_league(league: str):
    matches_file = f"daily_matches_{league}.json"
    picks_file = f"daily_picks_{league}.json"

    if not os.path.exists(matches_file):
        return []

    try:
        with open(matches_file, "r", encoding="utf-8") as f:
            matches = json.load(f)
    except Exception:
        return []

    # picks map por match_id STRING ✅
    picks_by_match = {}
    if os.path.exists(picks_file):
        try:
            with open(picks_file, "r", encoding="utf-8") as f:
                picks = json.load(f)
            for p in picks:
                mid = m.get("match_id") or m.get("matchId") or m.get("id") or m.get("fixture", {}).get("id")
                if mid is None:
                    continue
                picks_by_match[str(mid)] = p
        except Exception:
            pass

    out = []
    for m in matches:
        mid = (
    m.get("match_id")
    or m.get("matchId")
    or m.get("id")
    or m.get("fixture", {}).get("id")
)

# 🔥 fallback inteligente si no hay id
if mid is None:
    mid = f"{m.get('home','?')}_{m.get('away','?')}_{m.get('utcDate','?')}"

mid_key = str(mid)
p = picks_by_match.get(mid_key, {})


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


def split_sections(items):
    live = [x for x in items if x.get("status") in LIVE_STATUSES]
    upcoming = [x for x in items if x.get("status") in UPCOMING_STATUSES]
    recent = [x for x in items if x.get("status") == FINISHED_STATUS]

    live.sort(key=lambda x: x.get("utcDate") or "")
    upcoming.sort(key=lambda x: x.get("utcDate") or "")
    recent.sort(key=lambda x: x.get("utcDate") or "", reverse=True)

    return live, upcoming, recent[:60]


# -------------------------
# UI
# -------------------------
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


def match_card(item):
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
    <div class="card">
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


def page_shell(title, league, inner):
    return f"""
<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{title}</title>
<style>
:root {{
  --bg:#0b1220; --card:#111a2e; --muted:#94a3b8; --line:#1f2a44; --acc:#7c3aed;
}}
*{{box-sizing:border-box}}
body{{margin:0;font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto;background:var(--bg);color:#e5e7eb}}
a{{color:#c4b5fd;text-decoration:none}}
.wrap{{max-width:1120px;margin:0 auto;padding:16px}}
.topbar{{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}}
.brand{{font-weight:900;letter-spacing:.5px}}
.links{{display:flex;gap:12px;align-items:center}}
.hero{{background:linear-gradient(135deg,#111a2e,#0f2440);border:1px solid var(--line);padding:14px;border-radius:14px;margin-bottom:12px}}
.hero-title{{font-size:20px;font-weight:900}}
.controls{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:10px}}
.select{{background:#0b1730;border:1px solid var(--line);color:#e5e7eb;padding:8px 10px;border-radius:10px}}
.btn{{background:var(--acc);color:#fff;border:0;padding:8px 12px;border-radius:10px;font-weight:800;cursor:pointer}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:10px}}
.card{{background:var(--card);border:1px solid var(--line);padding:10px;border-radius:12px}}
.rowtitle{{font-size:14px;font-weight:900;display:flex;gap:8px;flex-wrap:wrap;align-items:center}}
.teams{{font-weight:900}}
.meta{{color:var(--muted);font-size:11px;margin-top:4px}}
.muted{{color:var(--muted)}}
.pill{{display:inline-block;padding:3px 8px;border-radius:999px;font-size:11px;border:1px solid var(--line);background:#0b1730}}
.pill.win{{border-color:#14532d;background:#052e16;color:#86efac}}
.pill.loss{{border-color:#7f1d1d;background:#450a0a;color:#fca5a5}}
.pill.pend{{border-color:#78350f;background:#2a1707;color:#fde68a}}
.pill.live{{border-color:#7f1d1d;background:#450a0a;color:#fecaca}}
.pill.fin{{border-color:#0f172a;background:#0b1730;color:#cbd5e1}}
.pill.upc{{border-color:#334155;background:#0b1730;color:#cbd5e1}}
.pill.score{{opacity:.95}}
.pickbox{{margin-top:8px;padding:10px;border-radius:12px;background:#0f2440;border:1px solid #223457}}
.pickhead{{font-size:12px}}
.pickmeta{{font-size:11px;margin-top:6px}}
.sectionTitle{{margin:14px 0 8px;font-size:13px;font-weight:900;color:#cbd5e1}}
hr{{border:0;border-top:1px solid var(--line);margin:14px 0}}
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
    <div class="muted">Liga: <b>{league} • {LEAGUES.get(league,"")}</b></div>
    <div class="controls">
      {league_select(league)}
      <button class="btn" onclick="refreshNow()">⚡ Refresh</button>
      <span class="muted">Te pide key (si no, Unauthorized).</span>
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


# -------------------------
# routes
# -------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(league: str = DEFAULT_LEAGUE):
    if league not in LEAGUES:
        league = DEFAULT_LEAGUE

    items = load_json_league(league)
    live, upcoming, recent = split_sections(items)

    def section(title, lst):
        if not lst:
            return f"<div class='muted'>No hay datos en {title}.</div>"
        return f"""
        <div class="sectionTitle">{title}</div>
        <div class="grid">
          {''.join(match_card(x) for x in lst)}
        </div>
        """

    inner = section("🔴 LIVE", live) + "<hr/>" + section("🗓️ UPCOMING", upcoming) + "<hr/>" + section("🧾 RECENT (últimos 60)", recent)
    return page_shell("AFTR Dashboard", league, inner)


@app.get("/api/stats")
def api_stats():
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
    inner = f"""
    <div class="grid">
      <div class="card"><div class="rowtitle">📌 Picks totales</div><div style="font-size:28px;font-weight:900;">{s['total_picks']}</div><div class="muted">Incluye pending</div></div>
      <div class="card"><div class="rowtitle">✅ Wins</div><div style="font-size:28px;font-weight:900;color:#86efac;">{s['wins']}</div></div>
      <div class="card"><div class="rowtitle">❌ Losses</div><div style="font-size:28px;font-weight:900;color:#fca5a5;">{s['losses']}</div></div>
      <div class="card"><div class="rowtitle">⏳ Pending</div><div style="font-size:28px;font-weight:900;color:#fde68a;">{s['pending']}</div></div>
      <div class="card" style="grid-column: span 2;"><div class="rowtitle">📈 Winrate (decididos)</div><div style="font-size:34px;font-weight:900;">{s['winrate']}%</div></div>
    </div>
    """
    return page_shell("AFTR Stats", DEFAULT_LEAGUE, inner)


@app.get("/docs", response_class=HTMLResponse)
def docs_page():
    inner = """
    <div class="card">
      <div class="rowtitle">📚 Endpoints</div>
      <div class="muted" style="margin-top:8px; line-height:1.6;">
        • <b>/</b> Dashboard<br/>
        • <b>/stats</b> Stats<br/>
        • <b>/api/stats</b> Stats JSON<br/>
        • <b>/refresh?key=...</b> fuerza update (requiere REFRESH_KEY)<br/>
      </div>
    </div>
    """
    return page_shell("AFTR Docs", DEFAULT_LEAGUE, inner)


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
    if os.path.exists(mf):
        try:
            with open(mf, "r", encoding="utf-8") as f:
                data = json.load(f)
            info["matches_count"] = len(data) if isinstance(data, list) else "not_list"
            sample = data[0] if isinstance(data, list) and data else None
        except Exception as e:
            info["matches_read_error"] = str(e)
    return {"info": info, "sample_first_match": sample}


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
    # seed
    try:
        if not os.path.exists("daily_matches_PL.json"):
            subprocess.run([os.sys.executable, "team_strength.py"], check=True)
    except Exception as e:
        print(f"⚠️ Seed startup failed: {e}")

    if AUTO_REFRESH:
        t = threading.Thread(target=_auto_refresh_loop, daemon=True)
        t.start()
        print("✅ Auto-refresh thread started.")







