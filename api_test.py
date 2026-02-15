import os
import json
import time
import sqlite3
import subprocess
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

APP_TITLE = "AFTR • MVP"
DB_PATH = os.getenv("AFTR_DB_PATH", "aftr.db")

REFRESH_KEY = os.getenv("REFRESH_KEY", "").strip()
AUTO_REFRESH = os.getenv("AUTO_REFRESH", "0").strip() == "1"
AUTO_REFRESH_EVERY_MIN = int(os.getenv("AUTO_REFRESH_EVERY_MIN", "60"))

TELEGRAM_LINK = os.getenv(
    "AFTR_TELEGRAM", "https://t.me/"
)  # poné el tuyo en Render si querés

# UI locking / ads
FREE_CARDS = 10
ADS_PER_DAY = 4
ADS_SECONDS = 10

import os, json
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

LEAGUE_TABS = [
    ("PL", "Premier League"),
    ("PD", "LaLiga"),
    ("SA", "Serie A"),
    ("BL1", "Bundesliga"),
    ("FL1", "Ligue 1"),
    ("CL", "UCL"),
]

LEAGUES = {
    "PL": "Premier League",
    "PD": "LaLiga",
    "SA": "Serie A",
    "BL1": "Bundesliga",
    "FL1": "Ligue 1",
    "CL": "UEFA Champions League",
}

# alias por si el front manda nombres raros
LEAGUE_ALIASES = {
    "EPL": "PL",
    "BUNDESLIGA": "BL1",
    "LIGUE1": "FL1",
    "LIGUE 1": "FL1",
    "UCL": "CL",
    "CHAMPIONS": "CL",
    "CHAMPIONSLEAGUE": "CL",
}

def normalize_league(code: str) -> str:
    if not code:
        return "PL"
    x = str(code).strip().upper()
    x = LEAGUE_ALIASES.get(x, x)
    return x if x in LEAGUES else "PL"

def json_paths_for_league(league: str):
    league = normalize_league(league)
    matches_file = BASE_DIR / f"daily_matches_{league}.json"
    picks_file = BASE_DIR / f"daily_picks_{league}.json"
    # fallback viejo (por si algún archivo quedó sin sufijo)
    if not matches_file.exists():
        alt = BASE_DIR / "daily_matches.json"
        if alt.exists():
            matches_file = alt
    if not picks_file.exists():
        alt = BASE_DIR / "daily_picks.json"
        if alt.exists():
            picks_file = alt
    return matches_file, picks_file

def load_json_bundle(league: str):
    matches_file, picks_file = json_paths_for_league(league)

    matches = []
    picks = []
    try:
        if matches_file.exists():
            with open(matches_file, "r", encoding="utf-8") as f:
                matches = json.load(f) or []
    except Exception:
        matches = []

    try:
        if picks_file.exists():
            with open(picks_file, "r", encoding="utf-8") as f:
                picks = json.load(f) or []
    except Exception:
        picks = []

    return matches, picks, str(matches_file.name), str(picks_file.name)

app = FastAPI(title="AFTR MVP", version="0.1.0")


# -------------------------
# DB helpers
# -------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_exists():
    return os.path.exists(DB_PATH)


def get_meta(key, default=None):
    if not db_exists():
        return default
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT value FROM meta WHERE key=?", (key,))
    row = cur.fetchone()
    conn.close()
    return row["value"] if row else default


def fetch_sections(league: str):
    if not db_exists():
        raise HTTPException(status_code=500, detail="Base de datos no encontrada")

    conn = db()
    cur = conn.cursor()

    cur.execute(
        """
      SELECT
        m.league, m.match_id, m.utcDate, m.status, m.home, m.away,
        m.home_goals, m.away_goals,
        m.crest_home, m.crest_away,
        m.xg_home, m.xg_away, m.xg_total,
        m.probs_json,
        p.market, p.prob, p.fair, p.rationale, p.status as pick_status
      FROM matches m
      LEFT JOIN picks p
        ON p.league=m.league AND p.match_id=m.match_id
      WHERE m.league=?
      ORDER BY m.utcDate ASC
    """,
        (league,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    now = datetime.now(timezone.utc)

    live = []
    upcoming = []
    recent = []

    for r in rows:
        # utcDate can be None sometimes
        try:
            dt = datetime.fromisoformat((r.get("utcDate") or "").replace("Z", "+00:00"))
        except Exception:
            dt = None

        status = r.get("status") or ""
        is_live = status in ("IN_PLAY", "PAUSED")

        # parse probs
        probs = {}
        try:
            probs = json.loads(r.get("probs_json") or "{}")
        except Exception:
            probs = {}

        item = {
            "league": r.get("league"),
            "match_id": str(r.get("match_id")),
            "utcDate": r.get("utcDate"),
            "dt": dt,
            "status": status,
            "home": r.get("home"),
            "away": r.get("away"),
            "home_goals": r.get("home_goals"),
            "away_goals": r.get("away_goals"),
            "crest_home": r.get("crest_home"),
            "crest_away": r.get("crest_away"),
            "xg_home": r.get("xg_home"),
            "xg_away": r.get("xg_away"),
            "xg_total": r.get("xg_total"),
            "probs": probs,
            "pick": {
                "market": r.get("market"),
                "prob": r.get("prob"),
                "fair": r.get("fair"),
                "rationale": r.get("rationale"),
                "status": r.get("pick_status") or "PENDING",
            }
            if r.get("market")
            else None,
        }

        if is_live:
            live.append(item)
        else:
            if dt and dt >= now:
                upcoming.append(item)
            else:
                # finished and past
                recent.append(item)

    # recent last 60 (most recent first)
    recent.sort(
        key=lambda x: x["dt"] or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True
    )
    recent = recent[:60]

    return live, upcoming, recent


# -------------------------
# Stats
# -------------------------
def stats_summary():
    if not db_exists():
        return {"total_picks": 0, "wins": 0, "losses": 0, "pending": 0, "winrate": 0}

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT status, COUNT(*) c FROM picks GROUP BY status")
    rows = cur.fetchall()
    conn.close()

    wins = 0
    losses = 0
    pending = 0
    for r in rows:
        s = (r["status"] or "").upper()
        if s == "WIN":
            wins = r["c"]
        elif s == "LOSS":
            losses = r["c"]
        else:
            pending += r["c"]

    total = wins + losses + pending
    wr = (wins / max(1, (wins + losses))) * 100.0
    return {
        "total_picks": total,
        "wins": wins,
        "losses": losses,
        "pending": pending,
        "winrate": round(wr, 2),
    }


# -------------------------
# HTML shell + CSS
# -------------------------
def page_shell(title, inner, league):
    last = get_meta(f"last_update_{league}", "n/a")
    tz = "Argentina (-03)"
    tabs = "".join(
        [
            f'<a class="tab {"on" if code == league else ""}" href="/?league={code}">{name}</a>'
            for code, name in LEAGUE_TABS
        ]
    )

    return f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <style>
    :root {{
      --bg: #050b18;
      --panel: rgba(255,255,255,0.06);
      --panel2: rgba(255,255,255,0.08);
      --text: #e9f0ff;
      --muted: rgba(233,240,255,0.72);
      --accent: #7c3aed;
      --good: #22c55e;
      --bad: #ef4444;
      --warn: #f59e0b;
      --cyan: #22d3ee;
    }}
    body {{
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial;
      background: radial-gradient(1200px 900px at 20% 10%, rgba(124,58,237,0.25), transparent 55%),
                  radial-gradient(1000px 700px at 80% 0%, rgba(34,211,238,0.16), transparent 55%),
                  var(--bg);
      color: var(--text);
    }}
    a {{ color: inherit; text-decoration: none; }}
    .wrap {{ max-width: 1200px; margin: 0 auto; padding: 18px 16px 40px; }}
    .topbar {{
      display:flex; align-items:center; justify-content:space-between;
      gap:12px; margin-bottom: 12px;
    }}
    .brand {{
      font-weight: 900; letter-spacing: 0.5px;
    }}
    .nav a {{
      display:inline-block; padding: 8px 12px; border-radius: 999px;
      background: rgba(255,255,255,0.06);
      margin-left: 8px;
      font-weight: 700;
    }}
    .tabs {{
      display:flex; gap:8px; flex-wrap:wrap; margin: 10px 0 14px;
    }}
    .tab {{
      padding: 8px 12px; border-radius: 999px; background: rgba(255,255,255,0.06);
      font-weight: 800; font-size: 13px;
      border: 1px solid rgba(255,255,255,0.08);
    }}
    .tab.on {{
      background: rgba(124,58,237,0.35);
      border: 1px solid rgba(124,58,237,0.60);
    }}
    .hero {{
      background: linear-gradient(180deg, rgba(255,255,255,0.08), rgba(255,255,255,0.04));
      border: 1px solid rgba(255,255,255,0.10);
      border-radius: 18px;
      padding: 14px 14px;
      margin-bottom: 16px;
    }}
    .hero h1 {{ margin: 0; font-size: 22px; }}
    .hero .sub {{ margin-top: 4px; color: var(--muted); font-weight: 650; }}
    .controls {{ display:flex; align-items:center; gap:10px; margin-top: 10px; flex-wrap:wrap; }}
    select {{
      background: rgba(255,255,255,0.08);
      border: 1px solid rgba(255,255,255,0.14);
      color: var(--text);
      padding: 8px 10px;
      border-radius: 12px;
      font-weight: 800;
    }}
    .btn {{
      padding: 9px 12px; border-radius: 12px;
      background: rgba(124,58,237,0.92);
      border: none;
      color: white;
      font-weight: 900;
      cursor:pointer;
    }}
    .btn.secondary {{
      background: rgba(255,255,255,0.10);
      border: 1px solid rgba(255,255,255,0.14);
    }}
    .pill {{
      display:inline-flex; align-items:center; gap:8px;
      padding: 6px 10px; border-radius: 999px;
      background: rgba(255,255,255,0.07);
      border: 1px solid rgba(255,255,255,0.10);
      font-weight: 800; font-size: 12px;
      color: var(--muted);
    }}
    .barwrap {{
      width: 240px; height: 10px; border-radius: 999px;
      background: rgba(255,255,255,0.08);
      overflow:hidden;
      border: 1px solid rgba(255,255,255,0.10);
    }}
    .bar {{
      height: 100%;
      width: 0%;
      background: linear-gradient(90deg, rgba(124,58,237,1), rgba(34,211,238,0.95));
    }}

    .section {{
      margin-top: 18px;
      border-top: 1px solid rgba(255,255,255,0.08);
      padding-top: 16px;
    }}
    .sectitle {{
      display:flex; align-items:center; gap:10px;
      font-weight: 1000;
      letter-spacing: 0.4px;
      margin-bottom: 10px;
    }}
    .dot {{
      width: 10px; height: 10px; border-radius:999px;
      background: rgba(255,255,255,0.2);
    }}
    .dot.live {{ background: var(--bad); }}
    .dot.up {{ background: var(--cyan); }}
    .dot.rec {{ background: var(--warn); }}

    /* Compact cards */
    .grid {{
      display:grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 10px;
    }}
    @media (min-width: 980px) {{
      .grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
    }}
    @media (min-width: 1240px) {{
      .grid {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
    }}

    .card {{
      background: rgba(255,255,255,0.06);
      border: 1px solid rgba(255,255,255,0.10);
      border-radius: 16px;
      padding: 10px 10px;
      position: relative;
      overflow:hidden;
    }}
    .card.win {{ border-color: rgba(34,197,94,0.55); box-shadow: 0 0 0 1px rgba(34,197,94,0.20) inset; }}
    .card.loss {{ border-color: rgba(239,68,68,0.55); box-shadow: 0 0 0 1px rgba(239,68,68,0.18) inset; }}
    .rowtitle {{
      font-size: 13px;
      font-weight: 1000;
      margin-bottom: 4px;
    }}
    .teams {{
      display:flex; align-items:center; justify-content:space-between;
      gap: 8px;
    }}
    .team {{
      display:flex; align-items:center; gap:8px;
      font-weight: 900;
    }}
    .crest {{
      width: 18px; height: 18px; border-radius: 6px;
      background: rgba(255,255,255,0.10);
      object-fit: contain;
    }}
    .meta {{
      margin-top: 6px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 750;
      display:flex;
      flex-wrap:wrap;
      gap: 8px;
      align-items:center;
    }}
    .tag {{
      display:inline-flex; align-items:center; gap:6px;
      padding: 4px 8px;
      border-radius: 999px;
      background: rgba(255,255,255,0.07);
      border: 1px solid rgba(255,255,255,0.10);
      font-size: 11px;
      font-weight: 900;
    }}
    .pickpill {{
      margin-top: 8px;
      display:flex; align-items:center; justify-content:space-between;
      gap: 8px;
      padding: 8px 10px;
      border-radius: 14px;
      background: rgba(124,58,237,0.15);
      border: 1px solid rgba(124,58,237,0.35);
      font-weight: 1000;
    }}
    .pickpill .mkt {{
      padding: 4px 8px;
      border-radius: 10px;
      background: rgba(255,255,255,0.08);
      border: 1px solid rgba(255,255,255,0.12);
      font-size: 12px;
      font-weight: 1000;
    }}
    .pickpill .pct {{
      font-size: 12px;
      font-weight: 1000;
      color: rgba(255,255,255,0.92);
    }}
    .tiny {{
      font-size: 11px;
      color: var(--muted);
      font-weight: 750;
      margin-top: 6px;
      line-height: 1.2;
    }}
    .lock {{
      position:absolute; inset:0;
      background: rgba(5,11,24,0.76);
      backdrop-filter: blur(4px);
      display:flex;
      align-items:center;
      justify-content:center;
      flex-direction:column;
      gap: 10px;
      padding: 10px;
      text-align:center;
    }}
    .lock .locktitle {{
      font-weight: 1000;
      font-size: 13px;
    }}
    .footer {{
      margin-top: 26px;
      color: var(--muted);
      font-weight: 700;
      font-size: 12px;
      opacity: 0.9;
    }}

    /* Modal */
    .modal {{
      position: fixed; inset:0;
      background: rgba(0,0,0,0.55);
      display:none;
      align-items:center; justify-content:center;
      padding: 18px;
    }}
    .modal.on {{ display:flex; }}
    .modalbox {{
      width: min(420px, 96vw);
      background: rgba(255,255,255,0.08);
      border: 1px solid rgba(255,255,255,0.12);
      border-radius: 18px;
      padding: 14px;
    }}
    .modalbox h3 {{
      margin: 0 0 8px;
      font-size: 16px;
      font-weight: 1000;
    }}
    .countdown {{
      font-size: 28px;
      font-weight: 1100;
      letter-spacing: 1px;
      margin: 10px 0;
    }}
    .modalrow {{
      display:flex; gap:10px; justify-content:flex-end; flex-wrap:wrap;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div class="brand">{APP_TITLE}</div>
      <div class="nav">
        <a href="/?league={league}">Dashboard</a>
        <a href="/stats?league={league}">Stats</a>
        <a href="/docs">Docs</a>
      </div>
    </div>

    <div class="tabs">{tabs}</div>

    <div class="hero">
      <h1>AFTR Dashboard</h1>
      <div class="sub">Liga: <b>{league}</b> • Última actualización (DB): <b>{last}</b> • Horario: <b>{tz}</b></div>

      <div class="controls">
        <select id="leagueSel" onchange="location.href='/?league='+this.value">
          {"".join([f'<option value="{c}" {"selected" if c == league else ""}>{c} • {n}</option>' for c, n in LEAGUE_TABS])}
        </select>

        <button class="btn secondary" onclick="window.open('{TELEGRAM_LINK}','_blank')">💎 Premium</button>
        <button class="btn" onclick="openAdModal()">🎬 Ver un anuncio</button>

        <span class="pill">
          Ads disponibles hoy: <span id="adsLeft">-</span> / {ADS_PER_DAY}
        </span>

        <div class="pill" style="gap:10px;">
          <span>Progreso</span>
          <div class="barwrap"><div class="bar" id="adbar"></div></div>
        </div>

        <button class="btn secondary" onclick="forceRefresh()">⚡ Refresh</button>
      </div>

      <div class="tiny" style="margin-top:10px;">
        ⚡ Refresh llama <code>/refresh?key=TU_REFRESH_KEY</code>. Si no tenés key, te va a decir Unauthorized.
      </div>
    </div>

    {inner}

    <div class="footer">
      ⚠️ Esto es un MVP. Probabilidades basadas en Poisson + strengths (xG proxy). No es consejo financiero. AFTR 2026.
    </div>
  </div>

  <div class="modal" id="adModal">
    <div class="modalbox">
      <h3>🎬 “Anuncio” (placeholder)</h3>
      <div class="tiny">
        Simulamos un ad: contás hasta {ADS_SECONDS}s y se desbloquea 1 pick.
        Después metemos AdSense / AdMob / afiliados.
      </div>
      <div class="countdown" id="cd">--</div>
      <div class="modalrow">
        <button class="btn secondary" onclick="closeAdModal()">Cerrar</button>
        <button class="btn" id="btnStart" onclick="startAd()">Ver ahora</button>
      </div>
    </div>
  </div>

<script>
  const ADS_PER_DAY = {ADS_PER_DAY};
  const ADS_SECONDS = {ADS_SECONDS};
  const FREE_CARDS = {FREE_CARDS};

  function todayKey(){{
    const d = new Date();
    const y = d.getFullYear();
    const m = String(d.getMonth()+1).padStart(2,'0');
    const day = String(d.getDate()).padStart(2,'0');
    return `${{y}}-${{m}}-${{day}}`;
  }}

  function storageKey(name) {{
    const league = new URLSearchParams(location.search).get('league') || 'PL';
    return `aftr_${{name}}_${{league}}_${{todayKey()}}`;
  }}

  function getInt(k, def=0) {{
    const v = localStorage.getItem(k);
    if(!v) return def;
    const n = parseInt(v,10);
    return isNaN(n) ? def : n;
  }}

  function getUnlockedSet() {{
    const raw = localStorage.getItem(storageKey('unlocked')) || "[]";
    try {{
      const arr = JSON.parse(raw);
      return new Set(arr.map(String));
    }} catch(e) {{
      return new Set();
    }}
  }}

  function setUnlockedSet(setObj) {{
    const arr = Array.from(setObj);
    localStorage.setItem(storageKey('unlocked'), JSON.stringify(arr));
  }}

  function refreshAdsUI() {{
    const used = getInt(storageKey('ads_used'), 0);
    const left = Math.max(0, ADS_PER_DAY - used);
    document.getElementById('adsLeft').textContent = left;

    const pct = (used / ADS_PER_DAY) * 100;
    document.getElementById('adbar').style.width = `${{pct}}%`;
  }}

  function openAdModal(){{
    document.getElementById('adModal').classList.add('on');
    document.getElementById('cd').textContent = "--";
  }}
  function closeAdModal(){{
    document.getElementById('adModal').classList.remove('on');
  }}

  let timer = null;
  function startAd(){{
    const used = getInt(storageKey('ads_used'), 0);
    if(used >= ADS_PER_DAY) {{
      alert("Hoy ya usaste todos los anuncios. Premium o mañana 😉");
      return;
    }}
    let t = ADS_SECONDS;
    document.getElementById('cd').textContent = t + "s";
    document.getElementById('btnStart').disabled = true;

    timer = setInterval(() => {{
      t -= 1;
      document.getElementById('cd').textContent = t + "s";
      if(t <= 0) {{
        clearInterval(timer);
        timer = null;

        // consume one ad and unlock one more pick (global unlock count)
        localStorage.setItem(storageKey('ads_used'), String(used + 1));
        localStorage.setItem(storageKey('unlock_slots'), String(getInt(storageKey('unlock_slots'), 0) + 1));

        document.getElementById('btnStart').disabled = false;
        closeAdModal();
        refreshAdsUI();
        applyLocks();
      }}
    }}, 1000);
  }}

  function applyLocks(){{
    const unlocked = getUnlockedSet();
    const slots = getInt(storageKey('unlock_slots'), 0);
    let usedSlots = 0;

    const cards = Array.from(document.querySelectorAll('[data-card-index]'));
    cards.forEach(card => {{
      const idx = parseInt(card.getAttribute('data-card-index'), 10);
      const mid = card.getAttribute('data-match-id');

      // free cards always visible
      if(idx < FREE_CARDS) {{
        const lock = card.querySelector('.lock');
        if(lock) lock.remove();
        return;
      }}

      // already unlocked by choosing this match
      if(unlocked.has(String(mid))) {{
        const lock = card.querySelector('.lock');
        if(lock) lock.remove();
        return;
      }}

      // if we have available slots, show CTA to choose this pick to unlock
      const available = slots - usedSlots;
      if(available > 0) {{
        // show lock but with unlock button that consumes a slot and unlocks THIS match
        ensureLock(card, mid, true);
      }} else {{
        ensureLock(card, mid, false);
      }}
    }});
  }}

  function ensureLock(card, mid, canChoose){{
    let lock = card.querySelector('.lock');
    if(!lock) {{
      lock = document.createElement('div');
      lock.className = 'lock';
      card.appendChild(lock);
    }}
    lock.innerHTML = '';
    const t = document.createElement('div');
    t.className = 'locktitle';
    t.textContent = "🔒 Pick bloqueada";
    const s = document.createElement('div');
    s.className = 'tiny';
    s.textContent = canChoose
      ? "Tenés 1 unlock disponible: elegí cuál pick desbloquear."
      : "Mirá un anuncio o Premium para desbloquear más.";

    lock.appendChild(t);
    lock.appendChild(s);

    if(canChoose) {{
      const btn = document.createElement('button');
      btn.className = 'btn';
      btn.textContent = "Desbloquear esta";
      btn.onclick = () => {{
        const unlocked = getUnlockedSet();
        unlocked.add(String(mid));
        setUnlockedSet(unlocked);

        // consume 1 unlock slot
        const slots = getInt(storageKey('unlock_slots'), 0);
        localStorage.setItem(storageKey('unlock_slots'), String(Math.max(0, slots - 1)));
        applyLocks();
      }};
      lock.appendChild(btn);
    }} else {{
      const btn2 = document.createElement('button');
      btn2.className = 'btn secondary';
      btn2.textContent = "Ver un anuncio";
      btn2.onclick = openAdModal;
      lock.appendChild(btn2);
    }}
  }}

  async function forceRefresh(){{
    const key = prompt("REFRESH KEY:");
    if(!key) return;
    const res = await fetch(`/refresh?key=${{encodeURIComponent(key)}}`);
    const j = await res.json();
    alert(JSON.stringify(j));
    location.reload();
  }}

  refreshAdsUI();
  applyLocks();
</script>
</body>
</html>
"""


def fmt_dt_local(utc_iso):
    # show Argentina approx (UTC-3)
    if not utc_iso:
        return "-"
    try:
        dt = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
    except Exception:
        return utc_iso
    ar = dt.astimezone(timezone(timedelta(hours=-3)))
    return ar.strftime("%d/%m %H:%M")


def card_html(item, idx):
    home = item["home"]
    away = item["away"]
    crest_h = item.get("crest_home") or ""
    crest_a = item.get("crest_away") or ""
    mid = item["match_id"]
    status = item["status"]

    # match status label
    st = (
        "LIVE"
        if status in ("IN_PLAY", "PAUSED")
        else ("UPCOMING" if status in ("SCHEDULED", "TIMED") else "RECENT")
    )

    score = ""
    if item.get("home_goals") is not None and item.get("away_goals") is not None:
        score = f"{item['home_goals']} - {item['away_goals']}"

    # pick
    pick = item.get("pick")
    pick_html = '<div class="tiny">Sin pick (todavía)</div>'
    card_cls = "card"

    if pick:
        pstatus = (pick.get("status") or "PENDING").upper()
        if pstatus == "WIN":
            card_cls += " win"
        elif pstatus == "LOSS":
            card_cls += " loss"

        market = pick.get("market") or ""
        prob = pick.get("prob")
        fair = pick.get("fair")

        pct = ""
        if prob is not None:
            pct = f"{prob * 100:.1f}%"
        fair_txt = f"{fair:.2f}" if fair is not None else "-"

        pick_html = f"""
          <div class="pickpill">
            <span class="mkt">{market}</span>
            <span class="pct">↗ {pct} • Fair {fair_txt}</span>
          </div>
          <div class="tiny">{pick.get("rationale") or ""}</div>
        """

    xg = ""
    if item.get("xg_home") is not None and item.get("xg_away") is not None:
        xg = f"xG {float(item['xg_home']):.2f} - {float(item['xg_away']):.2f} (total {float(item['xg_total']):.2f})"

    meta = f"""
      <div class="meta">
        <span class="tag">{st}</span>
        <span class="tag">{fmt_dt_local(item.get("utcDate"))}</span>
        <span class="tag">{xg}</span>
        {f'<span class="tag">Score {score}</span>' if score else ""}
      </div>
    """

    return f"""
    <div class="{card_cls}" data-card-index="{idx}" data-match-id="{mid}">
      <div class="teams">
        <div class="team">
          <img class="crest" src="{crest_h}" onerror="this.style.display='none'" />
          <span>{home}</span>
        </div>
        <div class="tag" style="font-weight:1000;">vs</div>
        <div class="team" style="justify-content:flex-end;">
          <span>{away}</span>
          <img class="crest" src="{crest_a}" onerror="this.style.display='none'" />
        </div>
      </div>
      {meta}
      {pick_html}
    </div>
    """


def section_html(title, dotcls, items, start_index):
    if not items:
        return f"""
        <div class="section">
          <div class="sectitle"><span class="dot {dotcls}"></span>{title}</div>
          <div class="tiny">Nada por acá.</div>
        </div>
        """
    cards = []
    idx = start_index
    for it in items:
        cards.append(card_html(it, idx))
        idx += 1
    return (
        f"""
      <div class="section">
        <div class="sectitle"><span class="dot {dotcls}"></span>{title}</div>
        <div class="grid">
          {"".join(cards)}
        </div>
      </div>
    """,
        idx,
    )


# -------------------------
# Routes
# -------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, league: str = "PL"):
    league = (league or "PL").upper()
    # validate league
    if league not in [c for c, _ in LEAGUE_TABS]:
        league = "PL"

    live, upcoming, recent = fetch_sections(league)

    html_parts = []

    a = section_html("EN VIVO AHORA", "live", live, 0)
    html_parts.append(a if isinstance(a, str) else a)

    b = section_html("UPCOMING", "upcoming", upcoming, 0)
    html_parts.append(b)

    c = section_html("RECENT", "recent", recent, 0)
    html_parts.append(c)
    # --- SAFETY: por si algún helper devuelve (html, algo) ---
    fixed = []
    for p in html_parts:
        if isinstance(p, tuple):
            p = p[0]
        fixed.append(p if isinstance(p, str) else str(p))
    html_parts = fixed

    inner = "\n".join(html_parts)
    return HTMLResponse(page_shell("AFTR Dashboard", inner, league))


@app.get("/stats", response_class=HTMLResponse)
def stats_page(league: str = "PL"):
    s = stats_summary()
    inner = f"""
    <div class="hero">
      <h1>Stats</h1>
      <div class="sub">Picks totales: <b>{s["total_picks"]}</b> • WIN: <b style="color:var(--good)">{s["wins"]}</b> • LOSS: <b style="color:var(--bad)">{s["losses"]}</b> • PENDING: <b>{s["pending"]}</b></div>
      <div class="sub">Winrate (sin pendientes): <b>{s["winrate"]}%</b></div>
    </div>
    """
    return HTMLResponse(page_shell("AFTR Stats", inner, league.upper()))


@app.get("/api/stats")
def api_stats():
    return JSONResponse(stats_summary())


@app.get("/refresh")
def refresh(key: str = ""):
    if not REFRESH_KEY:
        raise HTTPException(
            status_code=401,
            detail="REFRESH_KEY no está seteada como variable de entorno.",
        )
    if key != REFRESH_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # run updater
    try:
        # IMPORTANT: use python executable running this env
        py = os.getenv("PYTHON", "python")
        subprocess.check_call([py, "team_strength.py"])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Refresh failed: {e}")

    return {"ok": True, "msg": "DB actualizado"}


@app.get("/debug/files")
def debug_files(league: str = "PL"):
    # quick sanity check
    info = {
        "cwd": os.getcwd(),
        "db_exists": db_exists(),
        "db_path": DB_PATH,
        "league": league,
    }
    return info


# -------------------------
# Auto-refresh background
# -------------------------
def _auto_refresh_loop():
    print("✅ Auto-refresh thread started.")
    while True:
        try:
            # only if we have both env vars
            if REFRESH_KEY and os.getenv("FOOTBALL_DATA_API_KEY"):
                # refresh with internal call (no key needed here)
                py = os.getenv("PYTHON", "python")
                subprocess.check_call([py, "team_strength.py"])
        except Exception as e:
            print(f"⚠️ Auto-refresh error: {e}")
        time.sleep(max(60, AUTO_REFRESH_EVERY_MIN * 60))


@app.on_event("startup")
def startup():
    # start auto refresh if enabled
    if AUTO_REFRESH:
        import threading

        t = threading.Thread(target=_auto_refresh_loop, daemon=True)
        t.start()
