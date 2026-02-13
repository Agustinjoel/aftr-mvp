from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
import json
import os
import math
import requests
from datetime import datetime, timezone
from urllib.parse import quote

app = FastAPI()

# =========================
# Monetización / Acceso
# =========================
BASE_FREE_PICKS = 4          # gratis sin ads
REWARDED_FREE_MAX = 6        # 1 ad = +1 pick extra (máx 6)
PREMIUM_MESSAGE = "🔒 Premium: desbloqueá el resto de picks"

# =========================
# Ligas
# =========================
LEAGUES = {
    "PL": "Premier League",
    "PD": "LaLiga",
    "SA": "Serie A",
    "BL1": "Bundesliga",
    "FL1": "Ligue 1",
    # "LPF": "Argentina LPF",  # football-data v4 no lo tiene (404)
}
DEFAULT_LEAGUE = "PL"

# =========================
# Football-data (escudos)
# =========================
API_KEY = os.getenv("FOOTBALL_DATA_API_KEY", "")
HEADERS = {"X-Auth-Token": API_KEY}
BASE = "https://api.football-data.org/v4"
APP_LOGO_URL = "https://upload.wikimedia.org/wikipedia/commons/3/3b/Football_icon.svg"

# Cache escudos por liga
TEAM_CRESTS_BY_LEAGUE: dict[str, dict[str, str]] = {}

# =========================
# Telegram (ventas manual)
# =========================
TELEGRAM_USERNAME = "TUUSUARIO"  # <-- sin @
TELEGRAM_MSG = (
    "Hola! Quiero activar AFTR Premium.\n"
    "Vengo desde la app y quiero pagar el plan mensual.\n"
    "Pasame el link de pago y cómo obtengo el acceso."
)

# =========================
# Utils JSON
# =========================
def picks_file(league: str) -> str:
    return f"daily_picks_{league}.json"


def matches_file(league: str) -> str:
    return f"daily_matches_{league}.json"


def read_json(path: str):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


# =========================
# Cookies: rewarded credits
# =========================
def get_ad_credits(request: Request) -> int:
    try:
        v = int(request.cookies.get("ad_credits", "0"))
        return max(0, min(REWARDED_FREE_MAX, v))
    except Exception:
        return 0


def set_ad_credits(resp, credits: int):
    credits = max(0, min(REWARDED_FREE_MAX, credits))
    # 7 días
    resp.set_cookie(
        "ad_credits",
        str(credits),
        max_age=60 * 60 * 24 * 7,
        httponly=False,
        samesite="lax",
    )
    return resp


def free_limit_for_request(request: Request) -> int:
    credits = get_ad_credits(request)
    return min(BASE_FREE_PICKS + credits, BASE_FREE_PICKS + REWARDED_FREE_MAX)


# =========================
# Escudos
# =========================
def load_team_crests(league: str):
    if league in TEAM_CRESTS_BY_LEAGUE:
        return

    if not API_KEY:
        TEAM_CRESTS_BY_LEAGUE[league] = {}
        return

    try:
        url = f"{BASE}/competitions/{league}/teams"
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            TEAM_CRESTS_BY_LEAGUE[league] = {}
            return

        data = r.json()
        teams = data.get("teams", [])
        mapping = {}

        for t in teams:
            name = t.get("name")
            crest = t.get("crest") or t.get("crestUrl")
            if name and crest:
                mapping[name] = crest

        TEAM_CRESTS_BY_LEAGUE[league] = mapping
    except Exception:
        TEAM_CRESTS_BY_LEAGUE[league] = {}


def crest_img(league: str, team_name: str, size=22):
    mapping = TEAM_CRESTS_BY_LEAGUE.get(league, {})
    url = mapping.get(team_name, "")
    if not url:
        return ""
    return f'<img src="{url}" width="{size}" height="{size}" style="object-fit:contain;">'


# =========================
# Confidence chips
# =========================
def confidence(prob: float):
    p = _safe_float(prob, 0.0)
    if p >= 0.70:
        return ("HIGH", "conf-high")
    elif p >= 0.60:
        return ("MED", "conf-med")
    else:
        return ("LOW", "conf-low")


# =========================
# Drivers + Rationale (solo para unlocked)
# =========================
def model_drivers(match_item: dict):
    home = match_item.get("home", "Home")
    away = match_item.get("away", "Away")

    xh = _safe_float(match_item.get("xg_home"))
    xa = _safe_float(match_item.get("xg_away"))
    xt = _safe_float(match_item.get("xg_total"))

    p = match_item.get("probs") or {}
    ph = _safe_float(p.get("home"))
    pd = _safe_float(p.get("draw"))
    pa = _safe_float(p.get("away"))
    p_under = _safe_float(p.get("under_25"))
    p_over = _safe_float(p.get("over_25"))
    p_btts_yes = _safe_float(p.get("btts_yes"))
    p_btts_no = _safe_float(p.get("btts_no"))

    drivers = []

    edge = xa - xh
    if abs(edge) >= 0.80:
        leader = away if edge > 0 else home
        drivers.append(f"Dominio esperado: **{leader}** por edge xG grande ({xh:.2f} vs {xa:.2f}).")
    elif abs(edge) >= 0.35:
        leader = away if edge > 0 else home
        drivers.append(f"Ventaja ligera: **{leader}** por edge xG ({xh:.2f} vs {xa:.2f}).")
    else:
        drivers.append(f"Partido parejo por xG ({xh:.2f} vs {xa:.2f}) → más volatilidad.")

    if xt <= 2.10:
        drivers.append(f"Ambiente cerrado: total xG **{xt:.2f}** (tendencia a pocos goles).")
    elif xt >= 2.80:
        drivers.append(f"Ambiente abierto: total xG **{xt:.2f}** (tendencia a goles).")
    else:
        drivers.append(f"Ambiente medio: total xG **{xt:.2f}** (equilibrado).")

    if ph or pd or pa:
        fav = max([(ph, home), (pd, "Draw"), (pa, away)], key=lambda t: t[0])
        if fav[1] != "Draw" and fav[0] >= 0.55:
            drivers.append(f"Favorito claro según 1X2: **{fav[1]}** ({fav[0]:.3f}).")
        elif fav[1] == "Draw" and fav[0] >= 0.33:
            drivers.append(f"Empate con peso (Draw {fav[0]:.3f}) → ojo con double chance.")
        else:
            drivers.append("1X2 sin súper favorito → mejor mirar mercados de goles.")

    if p_under >= 0.62:
        drivers.append(f"Señal Under fuerte: Under2.5 **{p_under:.3f}**.")
    elif p_over >= 0.50 and xt >= 2.6:
        drivers.append(f"Over con argumento: Over2.5 **{p_over:.3f}** + total xG alto.")

    if p_btts_no >= 0.62 and min(xh, xa) <= 0.9:
        drivers.append(f"BTTS NO respaldado: BTTS No **{p_btts_no:.3f}** + ataque flojo esperado.")
    elif p_btts_yes >= 0.55 and xt >= 2.6:
        drivers.append(f"BTTS YES con argumento: BTTS Yes **{p_btts_yes:.3f}** + total xG alto.")

    return drivers


def bet_type_rationale(market: str, match_item: dict, prob: float):
    xh = _safe_float(match_item.get("xg_home"))
    xa = _safe_float(match_item.get("xg_away"))
    xt = _safe_float(match_item.get("xg_total"))
    p = match_item.get("probs") or {}

    ph = _safe_float(p.get("home"))
    pa = _safe_float(p.get("away"))
    p_under = _safe_float(p.get("under_25"))
    p_over = _safe_float(p.get("over_25"))
    p_btts_yes = _safe_float(p.get("btts_yes"))
    p_btts_no = _safe_float(p.get("btts_no"))

    m = (market or "").lower()
    pr = _safe_float(prob)

    if "under" in m:
        bits = []
        if xt <= 2.2:
            bits.append(f"total xG bajo ({xt:.2f})")
        if p_under:
            bits.append(f"Under2.5 prob {p_under:.3f}")
        if min(xh, xa) <= 0.9:
            bits.append(f"uno con ataque flojo (min xG {min(xh, xa):.2f})")
        return " + ".join(bits) + f" → Under (pick {pr:.3f})."

    if "over" in m:
        bits = []
        if xt >= 2.8:
            bits.append(f"total xG alto ({xt:.2f})")
        if p_over:
            bits.append(f"Over2.5 prob {p_over:.3f}")
        if min(xh, xa) >= 1.1:
            bits.append(f"ambos generan (min xG {min(xh, xa):.2f})")
        return " + ".join(bits) + f" → Over (pick {pr:.3f})."

    if "btts" in m and "yes" in m:
        bits = []
        if xt >= 2.6:
            bits.append(f"total xG alto ({xt:.2f})")
        if min(xh, xa) >= 1.0:
            bits.append(f"ambos llegan (min xG {min(xh, xa):.2f})")
        if p_btts_yes:
            bits.append(f"BTTS Yes prob {p_btts_yes:.3f}")
        return " + ".join(bits) + f" → BTTS Yes (pick {pr:.3f})."

    if "btts" in m and "no" in m:
        bits = []
        if min(xh, xa) <= 0.9:
            bits.append(f"uno con poca producción (min xG {min(xh, xa):.2f})")
        if p_btts_no:
            bits.append(f"BTTS No prob {p_btts_no:.3f}")
        return " + ".join(bits) + f" → BTTS No (pick {pr:.3f})."

    if "home" in m and "win" in m:
        bits = []
        if xh > xa:
            bits.append(f"home xG arriba ({xh:.2f} vs {xa:.2f})")
        if ph:
            bits.append(f"Home prob {ph:.3f}")
        return " + ".join(bits) + f" → Home Win (pick {pr:.3f})."

    if "away" in m and "win" in m:
        bits = []
        if xa > xh:
            bits.append(f"away xG arriba ({xa:.2f} vs {xh:.2f})")
        if pa:
            bits.append(f"Away prob {pa:.3f}")
        return " + ".join(bits) + f" → Away Win (pick {pr:.3f})."

    return f"xG ({xh:.2f}/{xa:.2f}, total {xt:.2f}) + prob {pr:.3f}."


# =========================
# Picks helpers
# =========================
def best_candidate_for_match(match_item):
    candidates = match_item.get("candidates") or []
    if not candidates:
        return None
    return max(candidates, key=lambda c: (_safe_float(c.get("prob", 0)), -_safe_float(c.get("fair"), 999)))


def best_pick_overall(picks):
    best = None
    for p in picks:
        c = best_candidate_for_match(p)
        if not c:
            continue
        score = _safe_float(c.get("prob"), 0)
        if (best is None) or (score > best[2]):
            best = (p, c, score)
    return (best[0], best[1]) if best else (None, None)


def ranked_candidates(picks):
    ranking = []
    for p in picks:
        for c in p.get("candidates", []):
            ranking.append({
                "home": p.get("home", ""),
                "away": p.get("away", ""),
                "utcDate": p.get("utcDate", ""),
                "xg_total": p.get("xg_total", 0),
                "market": c.get("market", ""),
                "prob": c.get("prob", 0),
                "fair": c.get("fair", None),
            })
    ranking.sort(key=lambda x: _safe_float(x["prob"]), reverse=True)
    return ranking


# =========================
# UI
# =========================
def league_nav(league: str):
    pills = []
    for code, name in LEAGUES.items():
        active = "pill-active" if code == league else ""
        pills.append(f'<a class="pill {active}" href="/?league={code}">{name}</a>')
    return '<div class="leaguebar">' + "".join(pills) + "</div>"


def page_shell(title, inner_html, league: str):
    return f"""
    <html>
    <head>
        <meta charset="utf-8" />
        <title>{title}</title>
        <style>
            body {{ background:#0b1220; color:#e5e7eb; font-family: Arial; padding:24px; }}
            .topbar {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:14px; gap:12px; flex-wrap:wrap; }}
            .brand {{ font-weight:900; font-size:20px; letter-spacing:0.4px; display:flex; align-items:center; gap:10px; }}
            .brand img {{ width:26px; height:26px; }}
            .links a {{ color:#60a5fa; text-decoration:none; margin-left:12px; font-size:14px; }}

            .leaguebar {{ display:flex; gap:8px; flex-wrap:wrap; margin:10px 0 16px; }}
            .pill {{
                padding:8px 10px; border-radius:999px; font-size:12px; font-weight:800;
                border:1px solid #223457; text-decoration:none; color:#cbd5e1; background:#0f172a;
            }}
            .pill-active {{ background:#0f2440; border-color:#38bdf8; color:#e5e7eb; }}

            .section-title {{ margin:18px 0 10px; font-size:16px; color:#cbd5e1; }}
            .grid {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap:14px; }}
            .card {{ background:#111a2e; border:1px solid #1f2a44; padding:14px; border-radius:12px; }}

            .meta {{ color:#94a3b8; font-size:12px; margin-bottom:10px; }}
            .muted {{ color:#94a3b8; }}
            .divider {{ height:1px; background:#1f2a44; margin:18px 0; }}

            .hero {{ background: linear-gradient(135deg, #0f172a, #0b2a1a); border:1px solid #1f2a44; padding:18px; border-radius:16px; margin-bottom:18px; }}
            .hero-title {{ font-size:13px; color:#86efac; letter-spacing:1px; text-transform:uppercase; margin-bottom:8px; }}
            .hero-match {{ font-size:22px; font-weight:900; margin-bottom:6px; display:flex; align-items:center; gap:10px; flex-wrap:wrap; }}
            .market {{ font-size:18px; font-weight:900; color:#38bdf8; margin-top:10px; }}
            .rowtitle {{ font-size:16px; font-weight:800; margin-bottom:6px; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }}
            .badge {{ display:inline-block; padding:4px 8px; border-radius:8px; background:#0b2a1a; border:1px solid #14532d; color:#86efac; font-size:12px; margin-left:8px; }}

            .conf-chip {{
                display:inline-block;
                padding:4px 8px;
                border-radius:999px;
                font-size:12px;
                font-weight:800;
                letter-spacing:0.5px;
                margin-left:8px;
                border:1px solid #223457;
                background:#0f172a;
            }}
            .conf-high {{ color:#86efac; border-color:#14532d; background:#0b2a1a; }}
            .conf-med  {{ color:#fde68a; border-color:#92400e; background:#2a1d0b; }}
            .conf-low  {{ color:#fca5a5; border-color:#7f1d1d; background:#2a0b0b; }}

            .drivers {{
                margin-top:10px;
                padding:10px 12px;
                border-radius:14px;
                background:#0f172a;
                border:1px solid #223457;
            }}
            .drivers-title {{
                font-weight:900;
                color:#86efac;
                font-size:12px;
                letter-spacing:0.6px;
                text-transform:uppercase;
                margin-bottom:8px;
            }}
            .drivers ul {{
                margin:0;
                padding-left:18px;
                color:#cbd5e1;
                font-size:12px;
                line-height:1.5;
            }}

            .pickbox {{
                margin-top:10px;
                padding:12px;
                border-radius:14px;
                background:#0f2440;
                border:1px solid #223457;
            }}
            .pickhead {{
                display:flex;
                align-items:center;
                justify-content:space-between;
                gap:10px;
                flex-wrap:wrap;
                font-weight:800;
            }}
            .pickmeta {{
                margin-top:6px;
                color:#cbd5e1;
                font-size:12px;
                line-height:1.4;
            }}

            .premium-card {{
                background: rgba(17,26,46,0.6);
                border:1px dashed #334155;
                padding:14px;
                border-radius:12px;
            }}
            .cta {{
                margin-top:10px;
                padding:10px 12px;
                border-radius:10px;
                background:#0f2440;
                border:1px solid #223457;
                color:#e5e7eb;
                font-weight:900;
                display:inline-block;
                text-decoration:none;
                cursor:pointer;
            }}
            .cta:disabled {{
                opacity:0.6;
                cursor:not-allowed;
            }}
            .adbox {{
                height:160px;
                border:1px dashed #334155;
                border-radius:12px;
                display:flex;
                align-items:center;
                justify-content:center;
                margin-top:12px;
                background:#0f172a;
            }}
        </style>
    </head>
    <body>
        <div class="topbar">
            <div class="brand">
                <img src="{APP_LOGO_URL}" alt="logo">
                AFTR • AI Picks
            </div>
            <div class="links">
                <a href="/?league={league}">Dashboard</a>
                <a href="/picks?league={league}">Picks</a>
                <a href="/matches?league={league}">Matches</a>
                <a href="/premium">Premium</a>
                <a href="/api/picks?league={league}" target="_blank">JSON Picks</a>
                <a href="/api/matches?league={league}" target="_blank">JSON Matches</a>
            </div>
        </div>

        {league_nav(league)}

        {inner_html}
    </body>
    </html>
    """


def render_unlock_card(request: Request, league: str, back_url: str):
    credits = get_ad_credits(request)
    remaining_ads = max(0, REWARDED_FREE_MAX - credits)
    free_now = free_limit_for_request(request)
    max_free = BASE_FREE_PICKS + REWARDED_FREE_MAX

    watch_link = f"/watch-ad?league={league}&back={quote(back_url, safe='/?=&')}"
    return f"""
    <div class="card" style="margin-bottom:14px;">
        <div style="font-weight:900;">Free unlock</div>
        <div class="muted">Gratis: {BASE_FREE_PICKS}. Desbloqueado por ads: +{credits}/{REWARDED_FREE_MAX}. Total free ahora: <b>{free_now}</b> / {max_free}.</div>
        <div style="margin-top:10px; display:flex; gap:10px; flex-wrap:wrap;">
            <a class="cta" href="{watch_link}">🎬 Ver anuncio (+1 pick)</a>
            <a class="cta" href="/premium">💎 Premium</a>
        </div>
        <div class="muted" style="margin-top:8px;">Ads restantes para desbloquear hoy: {remaining_ads}</div>
    </div>
    """


def render_cards(
    request: Request,
    items,
    title_text,
    league: str,
    back_url: str,
    show_probs=True,
    premium_lock=False,
    show_candidates=True,
):
    if not items:
        return f'<div class="section-title">{title_text} (0)</div><div class="muted">No hay data para esta liga. Corré team_strength.py</div>'

    html = f'<div class="section-title">{title_text} ({len(items)})</div>'

    if premium_lock:
        html += render_unlock_card(request, league, back_url)

    html += '<div class="grid">'

    visible = items
    locked = []

    if premium_lock:
        free_limit = free_limit_for_request(request)
        visible = items[:free_limit]
        locked = items[free_limit:]

    # Cargar escudos una vez por render
    load_team_crests(league)

    # Unlocked cards
    for it in visible:
        badge = '<span class="badge">PICK</span>' if (it.get("candidates") and len(it["candidates"]) > 0 and show_candidates) else ""
        home = it.get("home", "")
        away = it.get("away", "")

        home_crest = crest_img(league, home, 22)
        away_crest = crest_img(league, away, 22)

        html += f"""
        <div class="card">
            <div class="rowtitle">
                {home_crest} {home} <span class="muted">vs</span> {away} {away_crest}
                {badge}
            </div>
            <div class="meta">{it.get('utcDate','')} • xG {it.get('xg_home',0)} - {it.get('xg_away',0)} (total {it.get('xg_total',0)})</div>
        """

        if show_probs and it.get("probs"):
            p = it["probs"]
            html += f"""
            <div class="muted">1X2: H {p.get('home')} • D {p.get('draw')} • A {p.get('away')}</div>
            <div class="muted">U2.5 {p.get('under_25')} • O2.5 {p.get('over_25')} • BTTS Yes {p.get('btts_yes')}</div>
            """

        # Drivers (solo si está desbloqueado)
        drv = model_drivers(it)
        if drv:
            bullets = "".join([f"<li>{d}</li>" for d in drv[:4]])
            html += f"""
            <div class="drivers">
                <div class="drivers-title">Model Drivers</div>
                <ul>{bullets}</ul>
            </div>
            """

        # Candidates (solo unlocked + show_candidates)
        if show_candidates and it.get("candidates"):
            for c in it["candidates"]:
                prob_pct = round(_safe_float(c.get("prob")) * 100, 1)
                label, cls = confidence(c.get("prob"))
                rationale = bet_type_rationale(c.get("market", ""), it, c.get("prob"))

                html += f"""
                <div class="pickbox">
                    <div class="pickhead">
                        <span>{c.get("market")} • {prob_pct}% • Fair {c.get("fair")}</span>
                        <span class="conf-chip {cls}">{label}</span>
                    </div>
                    <div class="pickmeta"><b>Bet rationale:</b> {rationale}</div>
                </div>
                """

        html += "</div>"

    # Locked cards (premium hard lock, sin data sensible)
    if premium_lock and locked:
        for it in locked[:6]:
            home = it.get("home", "")
            away = it.get("away", "")
            html += f"""
            <div class="premium-card">
                <div class="rowtitle">
                    {home} <span class="muted">vs</span> {away}
                    <span class="badge">PREMIUM</span>
                </div>
                <div class="meta">{it.get('utcDate','')}</div>
                <div class="muted">{PREMIUM_MESSAGE}</div>
                <a href="/premium" class="cta">💎 Desbloquear Premium</a>
            </div>
            """
        remaining = max(0, len(locked) - 6)
        if remaining > 0:
            html += f"""
            <div class="premium-card">
                <div class="rowtitle">🔒 +{remaining} picks más bloqueados</div>
                <div class="muted">Activá Premium para verlos todos.</div>
                <a href="/premium" class="cta">💎 Desbloquear Premium</a>
            </div>
            """

    html += "</div>"
    return html


# =========================
# Rewarded flow
# =========================
@app.get("/watch-ad", response_class=HTMLResponse)
def watch_ad(request: Request, league: str = Query(DEFAULT_LEAGUE), back: str = Query("/")):
    league = league if league in LEAGUES else DEFAULT_LEAGUE
    credits = get_ad_credits(request)

    if credits >= REWARDED_FREE_MAX:
        inner = f"""
        <div class="hero">
          <div class="hero-title">Rewarded</div>
          <div class="hero-match">Ya desbloqueaste todo lo posible por ads 😈</div>
          <div class="muted">Máximo: {REWARDED_FREE_MAX} picks extra.</div>
          <a class="cta" href="{back}">Volver</a>
        </div>
        """
        return page_shell("Watch Ad", inner, league)

    inner = f"""
    <div class="hero">
        <div class="hero-title">Rewarded Ad</div>
        <div class="hero-match">Desbloqueás +1 pick</div>
        <div class="muted">Esperá 8 segundos y se habilita el botón.</div>
    </div>

    <div class="card">
        <div style="font-weight:900; margin-bottom:10px;">Sponsor slot</div>
        <div class="muted">Acá va un anuncio real (patrocinador / red compatible). Por ahora placeholder.</div>
        <div class="adbox"><div class="muted">AD SPACE</div></div>

        <div style="margin-top:14px;">
            <button id="btn" class="cta" disabled>⏳ Esperando...</button>
        </div>
        <div class="muted" style="margin-top:10px;">Tip: esto mantiene el free vivo mientras premium despega.</div>
    </div>

    <script>
      let t = 8;
      const btn = document.getElementById("btn");
      const tick = () => {{
        if (t <= 0) {{
          btn.disabled = false;
          btn.textContent = "✅ Desbloquear +1 pick";
          btn.onclick = () => {{
            window.location.href = "/ad-reward?league={league}&back=" + encodeURIComponent("{back}");
          }};
          return;
        }}
        btn.textContent = "⏳ Esperando... " + t + "s";
        t -= 1;
        setTimeout(tick, 1000);
      }};
      tick();
    </script>
    """
    return page_shell("Watch Ad", inner, league)


@app.get("/ad-reward")
def ad_reward(request: Request, league: str = Query(DEFAULT_LEAGUE), back: str = Query("/")):
    credits = get_ad_credits(request)
    credits = min(REWARDED_FREE_MAX, credits + 1)
    resp = RedirectResponse(url=back, status_code=302)
    return set_ad_credits(resp, credits)


# =========================
# JSON endpoints
# =========================
@app.get("/api/picks")
def picks_json(league: str = Query(DEFAULT_LEAGUE)):
    league = league if league in LEAGUES else DEFAULT_LEAGUE
    path = picks_file(league)
    if not os.path.exists(path):
        return {"error": f"No picks file found for {league}. Run team_strength.py."}
    return read_json(path)


@app.get("/api/matches")
def matches_json(league: str = Query(DEFAULT_LEAGUE)):
    league = league if league in LEAGUES else DEFAULT_LEAGUE
    path = matches_file(league)
    if not os.path.exists(path):
        return {"error": f"No matches file found for {league}. Run team_strength.py."}
    return read_json(path)


# =========================
# Pages
# =========================
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, league: str = Query(DEFAULT_LEAGUE)):
    league = league if league in LEAGUES else DEFAULT_LEAGUE

    picks = read_json(picks_file(league))
    matches = read_json(matches_file(league))

    top_pick, top_candidate = best_pick_overall(picks)
    ranking = ranked_candidates(picks)

    if top_pick and top_candidate:
        prob_pct = round(_safe_float(top_candidate.get("prob")) * 100, 1)
        label, cls = confidence(top_candidate.get("prob"))

        home = top_pick.get("home", "")
        away = top_pick.get("away", "")

        load_team_crests(league)
        home_crest = crest_img(league, home, 26)
        away_crest = crest_img(league, away, 26)

        rationale = bet_type_rationale(top_candidate.get("market", ""), top_pick, top_candidate.get("prob"))
        drv = model_drivers(top_pick)
        bullets = "".join([f"<li>{d}</li>" for d in drv[:3]]) if drv else ""

        inner = f"""
        <div class="hero">
            <div class="hero-title">Top pick del día • {LEAGUES.get(league)}</div>
            <div class="hero-match">
                {home_crest} {home} <span class="muted">vs</span> {away} {away_crest}
                <span class="conf-chip {cls}">{label}</span>
            </div>
            <div class="meta">{top_pick.get('utcDate','')} • xG {top_pick.get('xg_home')} - {top_pick.get('xg_away')} (total {top_pick.get('xg_total')})</div>
            <div class="market">{top_candidate.get('market')} → {prob_pct}% • Fair {top_candidate.get('fair')}</div>

            <div class="drivers" style="margin-top:12px;">
                <div class="drivers-title">Top Drivers</div>
                <ul>{bullets}</ul>
            </div>

            <div class="pickbox" style="margin-top:12px;">
                <div class="pickhead">
                    <span>Bet rationale</span>
                    <span class="conf-chip {cls}">{label}</span>
                </div>
                <div class="pickmeta">{rationale}</div>
            </div>

            <div class="muted" style="margin-top:10px;">
                Picks: <b>{len(picks)}</b> • Matches: <b>{len(matches)}</b> • Free base: <b>{BASE_FREE_PICKS}</b> • Rewarded max: <b>{REWARDED_FREE_MAX}</b>
            </div>
        </div>
        """
    else:
        inner = f"""
        <div class="hero">
            <div class="hero-title">Top pick del día • {LEAGUES.get(league)}</div>
            <div class="muted">No hay picks todavía para esta liga. Corré team_strength.py</div>
        </div>
        """

    # Ranking top 5 (solo “teaser” igual, es info agregada)
    inner += '<div class="section-title">🏆 Ranking de confianza (Top 5)</div>'
    if not ranking:
        inner += '<div class="muted">No hay ranking todavía.</div>'
    else:
        inner += '<div class="grid">'
        load_team_crests(league)
        for i, r in enumerate(ranking[:5], start=1):
            prob_pct = round(_safe_float(r.get("prob")) * 100, 1)
            label, cls = confidence(r.get("prob"))
            home_crest = crest_img(league, r.get("home", ""), 20)
            away_crest = crest_img(league, r.get("away", ""), 20)

            inner += f"""
            <div class="card">
                <div class="rowtitle">
                    #{i} {home_crest} {r.get('home')} <span class="muted">vs</span> {r.get('away')} {away_crest}
                    <span class="conf-chip {cls}">{label}</span>
                </div>
                <div class="meta">{r.get('utcDate','')} • xG total {r.get('xg_total')}</div>
                <div class="pickbox">
                    <div class="pickhead">
                        <span>{r.get('market')} • {prob_pct}% • Fair {r.get('fair')}</span>
                        <span class="conf-chip {cls}">{label}</span>
                    </div>
                </div>
            </div>
            """
        inner += "</div>"

    inner += '<div class="divider"></div>'

    inner += render_cards(
        request,
        picks,
        "🔥 Picks detectados (Free + Rewarded + 🔒 Premium)",
        league=league,
        back_url=f"/?league={league}",
        show_probs=True,
        premium_lock=True,
        show_candidates=True,
    )

    inner += '<div class="divider"></div>'

    inner += render_cards(
        request,
        matches,
        "📅 Próximos partidos (transparente, sin recomendaciones)",
        league=league,
        back_url=f"/matches?league={league}",
        show_probs=True,
        premium_lock=False,
        show_candidates=False,
    )

    return page_shell("AFTR Dashboard", inner, league)


@app.get("/picks", response_class=HTMLResponse)
def picks_page(request: Request, league: str = Query(DEFAULT_LEAGUE)):
    league = league if league in LEAGUES else DEFAULT_LEAGUE
    picks = read_json(picks_file(league))

    inner = render_cards(
        request,
        picks,
        "🔥 Picks detectados (Free + Rewarded + 🔒 Premium)",
        league=league,
        back_url=f"/picks?league={league}",
        show_probs=True,
        premium_lock=True,
        show_candidates=True,
    )
    return page_shell("AFTR Picks", inner, league)


@app.get("/matches", response_class=HTMLResponse)
def matches_page(request: Request, league: str = Query(DEFAULT_LEAGUE)):
    league = league if league in LEAGUES else DEFAULT_LEAGUE
    matches = read_json(matches_file(league))

    inner = render_cards(
        request,
        matches,
        "📅 Próximos partidos (transparente, sin recomendaciones)",
        league=league,
        back_url=f"/matches?league={league}",
        show_probs=True,
        premium_lock=False,
        show_candidates=False,
    )
    return page_shell("AFTR Matches", inner, league)


@app.get("/premium", response_class=HTMLResponse)
def premium_page():
    contact_link = f"https://t.me/{AFTERPICK}?text={requests.utils.quote(TELEGRAM_MSG)}"

    inner = f"""
    <div class="hero">
        <div class="hero-title">AFTR Premium</div>
        <div class="hero-match">Desbloqueá todos los picks.</div>
        <div class="muted">
            Premium desbloquea el resto de picks (más allá de {BASE_FREE_PICKS + REWARDED_FREE_MAX}),
            con drivers y rationale completo.
        </div>
    </div>

    <div class="section-title">🔓 Qué desbloqueás</div>
    <div class="card">
        <div>✅ Todos los picks diarios</div>
        <div>✅ Ranking completo</div>
        <div>✅ Model Drivers</div>
        <div>✅ Bet rationale</div>
        <div class="muted" style="margin-top:8px;">Sin spam, sin humo. Solo data.</div>
    </div>

    <div class="section-title">💵 Precio</div>
    <div class="card">
        <div style="font-size:22px; font-weight:900;">24.99 USD / mes</div>
        <div class="muted">Cancelás cuando quieras. Acceso inmediato.</div>
    </div>

    <div class="section-title">🚀 Activar Premium</div>
    <div class="card">
        <div>Hacé click y te abrimos el chat con el mensaje listo:</div>
        <div style="margin-top:12px;">
            <a href="{contact_link}" target="_blank" class="cta">💎 Quiero Premium</a>
        </div>
    </div>
    """
    return page_shell("AFTR Premium", inner, DEFAULT_LEAGUE)



   





