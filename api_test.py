from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import json
import os
import requests

app = FastAPI()

PICKS_FILE = "daily_picks.json"
MATCHES_FILE = "daily_matches.json"

# ===== Premium settings =====
FREE_PICKS_LIMIT = 10
PREMIUM_MESSAGE = "🔒 Premium: desbloqueá el resto de picks"

# ===== football-data (escudos) =====
API_KEY = os.getenv("FOOTBALL_DATA_API_KEY", "")
HEADERS = {"X-Auth-Token": API_KEY}
COMP_CODE = "PL"
TEAMS_URL = f"https://api.football-data.org/v4/competitions/{COMP_CODE}/teams"

APP_LOGO_URL = "https://upload.wikimedia.org/wikipedia/commons/3/3b/Football_icon.svg"

TEAM_CRESTS = {}


# =========================
# Utils
# =========================
def read_json(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_team_crests():
    global TEAM_CRESTS
    if TEAM_CRESTS:
        return
    try:
        r = requests.get(TEAMS_URL, headers=HEADERS, timeout=20)
        r.raise_for_status()
        data = r.json()
        teams = data.get("teams", [])

        mapping = {}
        for t in teams:
            name = t.get("name")
            crest = t.get("crest") or t.get("crestUrl")
            if name and crest:
                mapping[name] = crest

        TEAM_CRESTS = mapping
    except Exception:
        TEAM_CRESTS = {}


def crest_img(team_name, size=22):
    url = TEAM_CRESTS.get(team_name, "")
    if not url:
        return ""
    return f'<img src="{url}" width="{size}" height="{size}" style="object-fit:contain;">'


def _safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


# =========================
# Confidence (semáforo)
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
# Model Drivers + Bet Rationale
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

    # xG edge
    edge = xa - xh
    if abs(edge) >= 0.80:
        leader = away if edge > 0 else home
        drivers.append(f"Dominio esperado: **{leader}** por edge xG grande ({xh:.2f} vs {xa:.2f}).")
    elif abs(edge) >= 0.35:
        leader = away if edge > 0 else home
        drivers.append(f"Ventaja ligera: **{leader}** por edge xG ({xh:.2f} vs {xa:.2f}).")
    else:
        drivers.append(f"Partido parejo por xG ({xh:.2f} vs {xa:.2f}) → más volatilidad.")

    # Game environment
    if xt <= 2.10:
        drivers.append(f"Ambiente cerrado: total xG **{xt:.2f}** (tendencia a pocos goles).")
    elif xt >= 2.80:
        drivers.append(f"Ambiente abierto: total xG **{xt:.2f}** (tendencia a goles).")
    else:
        drivers.append(f"Ambiente medio: total xG **{xt:.2f}** (ni ultra under ni festival).")

    # 1X2 favorite
    if ph or pd or pa:
        fav = max([(ph, home), (pd, "Draw"), (pa, away)], key=lambda t: t[0])
        if fav[1] != "Draw" and fav[0] >= 0.55:
            drivers.append(f"Favorito claro según 1X2: **{fav[1]}** ({fav[0]:.3f}).")
        elif fav[1] == "Draw" and fav[0] >= 0.33:
            drivers.append(f"Empate con peso (Draw {fav[0]:.3f}) → ojo con double chance.")
        else:
            drivers.append("1X2 sin súper favorito → goles pueden ser más estables que ganador.")

    # Market signals
    if p_under or p_over:
        if p_under >= 0.62:
            drivers.append(f"Señal Under fuerte: Under2.5 **{p_under:.3f}**.")
        elif p_over >= 0.45 and xt >= 2.6:
            drivers.append(f"Over con argumento: Over2.5 **{p_over:.3f}** + total xG alto.")

    if p_btts_yes or p_btts_no:
        if p_btts_no >= 0.62 and min(xh, xa) <= 0.9:
            drivers.append(f"BTTS NO respaldado: BTTS No **{p_btts_no:.3f}** + ataque flojo esperado.")
        elif p_btts_yes >= 0.50 and xt >= 2.6:
            drivers.append(f"BTTS YES con argumento: BTTS Yes **{p_btts_yes:.3f}** + total xG alto.")

    return drivers


def bet_type_rationale(market: str, match_item: dict, prob: float):
    home = match_item.get("home", "Home")
    away = match_item.get("away", "Away")
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
        if not bits:
            bits.append("perfil controlado")
        return " + ".join(bits) + f" → Under tiene sentido (pick {pr:.3f})."

    if "over" in m:
        bits = []
        if xt >= 2.8:
            bits.append(f"total xG alto ({xt:.2f})")
        if p_over:
            bits.append(f"Over2.5 prob {p_over:.3f}")
        if min(xh, xa) >= 1.1:
            bits.append(f"ambos generan (min xG {min(xh, xa):.2f})")
        if not bits:
            bits.append("perfil abierto")
        return " + ".join(bits) + f" → Over tiene sentido (pick {pr:.3f})."

    if "btts" in m and "yes" in m:
        bits = []
        if xt >= 2.6:
            bits.append(f"total xG alto ({xt:.2f})")
        if min(xh, xa) >= 1.0:
            bits.append(f"ambos llegan (min xG {min(xh, xa):.2f})")
        if p_btts_yes:
            bits.append(f"BTTS Yes prob {p_btts_yes:.3f}")
        if not bits:
            bits.append("ambos con chances")
        return " + ".join(bits) + f" → BTTS Yes coherente (pick {pr:.3f})."

    if "btts" in m and "no" in m:
        bits = []
        if min(xh, xa) <= 0.9:
            bits.append(f"uno con poca producción (min xG {min(xh, xa):.2f})")
        if abs(xa - xh) >= 0.7:
            leader = away if (xa - xh) > 0 else home
            bits.append(f"dominio de {leader} (edge xG)")
        if p_btts_no:
            bits.append(f"BTTS No prob {p_btts_no:.3f}")
        if not bits:
            bits.append("riesgo de cero de un lado")
        return " + ".join(bits) + f" → BTTS No con lógica (pick {pr:.3f})."

    if "home" in m and "win" in m:
        bits = []
        if xh > xa:
            bits.append(f"home xG arriba ({xh:.2f} vs {xa:.2f})")
        if ph:
            bits.append(f"Home prob {ph:.3f}")
        if not bits:
            bits.append("ventaja local en modelo")
        return " + ".join(bits) + f" → Home Win coherente (pick {pr:.3f})."

    if "away" in m and "win" in m:
        bits = []
        if xa > xh:
            bits.append(f"away xG arriba ({xa:.2f} vs {xh:.2f})")
        if pa:
            bits.append(f"Away prob {pa:.3f}")
        if not bits:
            bits.append("ventaja visitante en modelo")
        return " + ".join(bits) + f" → Away Win coherente (pick {pr:.3f})."

    return f"Señal combinada de xG ({xh:.2f}/{xa:.2f}, total {xt:.2f}) + prob {pr:.3f}."


# =========================
# Picks helpers
# =========================
def best_candidate_for_match(match_item):
    candidates = match_item.get("candidates") or []
    if not candidates:
        return None
    return max(candidates, key=lambda c: (c.get("prob", 0), -_safe_float(c.get("fair"), 999)))


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
                "home": p["home"],
                "away": p["away"],
                "utcDate": p.get("utcDate", ""),
                "xg_total": p.get("xg_total", 0),
                "market": c.get("market", ""),
                "prob": c.get("prob", 0),
                "fair": c.get("fair", None)
            })
    ranking.sort(key=lambda x: _safe_float(x["prob"]), reverse=True)
    return ranking


# =========================
# HTML shell
# =========================
def page_shell(title, inner_html):
    return f"""
    <html>
    <head>
        <meta charset="utf-8" />
        <title>{title}</title>
        <style>
            body {{ background:#0b1220; color:#e5e7eb; font-family: Arial; padding:24px; }}
            .topbar {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:18px; }}
            .brand {{ font-weight:900; font-size:20px; letter-spacing:0.4px; display:flex; align-items:center; gap:10px; }}
            .brand img {{ width:26px; height:26px; }}
            .links a {{ color:#60a5fa; text-decoration:none; margin-left:12px; font-size:14px; }}

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
            .blur {{
                filter: blur(6px);
                user-select:none;
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
                <a href="/" >Dashboard</a>
                <a href="/picks" >Picks</a>
                <a href="/matches" >Matches</a>
                <a href="/premium">Premium</a>
                <a href="/api/picks" target="_blank">JSON Picks</a>
                <a href="/api/matches" target="_blank">JSON Matches</a>
            </div>
        </div>

        {inner_html}
    </body>
    </html>
    """


def render_cards(items, title_text, show_probs=True, premium_lock=False, show_candidates=True):
    if not items:
        return f'<div class="section-title">{title_text} (0)</div><div class="muted">No hay data. Corré team_strength.py</div>'

    html = f'<div class="section-title">{title_text} ({len(items)})</div>'
    html += '<div class="grid">'

    visible = items
    locked = []
    if premium_lock:
        visible = items[:FREE_PICKS_LIMIT]
        locked = items[FREE_PICKS_LIMIT:]

    for it in visible:
        badge = '<span class="badge">PICK</span>' if (it.get("candidates") and len(it["candidates"]) > 0) else ""
        home = it.get("home", "")
        away = it.get("away", "")

        home_crest = crest_img(home, 22)
        away_crest = crest_img(away, 22)

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

        drv = model_drivers(it)
        if drv:
            bullets = "".join([f"<li>{d}</li>" for d in drv[:4]])
            html += f"""
            <div class="drivers">
                <div class="drivers-title">Model Drivers</div>
                <ul>{bullets}</ul>
            </div>
            """

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

    if premium_lock and locked:
        # Mostrar tarjetas bloqueadas "limpias" (sin data filtrable)
        for it in locked[:6]:  # muestra 6 bloqueadas (podés subir/bajar)
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
# JSON endpoints
# =========================
@app.get("/api/picks")
def picks_json():
    if not os.path.exists(PICKS_FILE):
        return {"error": "No picks file found. Run team_strength.py first."}
    return read_json(PICKS_FILE)


@app.get("/api/matches")
def matches_json():
    if not os.path.exists(MATCHES_FILE):
        return {"error": "No matches file found. Run team_strength.py first."}
    return read_json(MATCHES_FILE)


# =========================
# Pages
# =========================
@app.get("/", response_class=HTMLResponse)
def dashboard():
    load_team_crests()

    picks = read_json(PICKS_FILE)
    matches = read_json(MATCHES_FILE)

    top_pick, top_candidate = best_pick_overall(picks)
    ranking = ranked_candidates(picks)

    if top_pick and top_candidate:
        prob_pct = round(_safe_float(top_candidate.get("prob")) * 100, 1)
        label, cls = confidence(top_candidate.get("prob"))

        home = top_pick.get("home", "")
        away = top_pick.get("away", "")
        home_crest = crest_img(home, 26)
        away_crest = crest_img(away, 26)

        rationale = bet_type_rationale(top_candidate.get("market", ""), top_pick, top_candidate.get("prob"))
        drv = model_drivers(top_pick)
        bullets = "".join([f"<li>{d}</li>" for d in drv[:3]]) if drv else ""

        hero = f"""
        <div class="hero">
            <div class="hero-title">Top pick del día</div>
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

            <div class="muted" style="margin-top:10px;">Picks: <b>{len(picks)}</b> • Matches: <b>{len(matches)}</b> • Free: <b>{FREE_PICKS_LIMIT}</b></div>
        </div>
        """
    else:
        hero = """
        <div class="hero">
            <div class="hero-title">Top pick del día</div>
            <div class="muted">No hay picks todavía. Corré team_strength.py</div>
        </div>
        """

    inner = hero

    inner += '<div class="section-title">🏆 Ranking de confianza (Top 5)</div>'
    if not ranking:
        inner += '<div class="muted">No hay ranking todavía.</div>'
    else:
        inner += '<div class="grid">'
        for i, r in enumerate(ranking[:5], start=1):
            prob_pct = round(_safe_float(r.get("prob")) * 100, 1)
            label, cls = confidence(r.get("prob"))
            home_crest = crest_img(r.get("home", ""), 20)
            away_crest = crest_img(r.get("away", ""), 20)

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
        picks,
        "🔥 Picks detectados (Drivers + Rationale + FREE + 🔒)",
        show_probs=True,
        premium_lock=False,
        show_candidates=False
    )

    inner += '<div class="divider"></div>'

    inner += render_cards(
        matches,
        "📅 Próximos partidos (transparente, sin recomendaciones)",
        show_probs=True,
        premium_lock=False,
        show_candidates=False
    )

    return page_shell("AFTR Dashboard", inner)


@app.get("/picks", response_class=HTMLResponse)
def picks_page():
    load_team_crests()
    picks = read_json(PICKS_FILE)
    inner = render_cards(
        picks,
        "🔥 Picks detectados (Drivers + Rationale + FREE + 🔒)",
        show_probs=True,
        premium_lock=True,
        show_candidates=True
    )
    return page_shell("AFTR Picks", inner)


@app.get("/matches", response_class=HTMLResponse)
def matches_page():
    load_team_crests()
    matches = read_json(MATCHES_FILE)
    inner = render_cards(
        matches,
        "📅 Próximos partidos (transparente, sin recomendaciones)",
        show_probs=True,
        premium_lock=False,
        show_candidates=False
    )
    return page_shell("AFTR Matches", inner)


@app.get("/premium", response_class=HTMLResponse)
def premium_page():
    # TU TELEGRAM
    username = "AFTRPICK"  # sin @

    msg = """Hola! Quiero activar AFTR Premium.
Vengo desde la app y quiero pagar el plan mensual.
Pasame el link de pago y cómo obtengo el acceso."""

    contact_link = f"https://t.me/{username}?text={requests.utils.quote(msg)}"

    inner = f"""
    <div class="hero">
        <div class="hero-title">AFTR Premium</div>
        <div class="hero-match">Tomá decisiones con ventaja matemática.</div>
        <div class="muted">
            Accedé a todos los picks del modelo con probabilidades reales, fair odds, drivers y explicación.
            Sin humo. Solo data.
        </div>
    </div>

    <div class="section-title">🔓 Qué desbloqueás</div>
    <div class="card">
        <div>✅ Todos los picks diarios (no solo los primeros {FREE_PICKS_LIMIT})</div>
        <div>✅ Ranking completo de confianza</div>
        <div>✅ Model Drivers</div>
        <div>✅ Bet type rationale</div>
        <div>✅ Actualizaciones del modelo</div>
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
    return page_shell("AFTR Premium", inner)

   





