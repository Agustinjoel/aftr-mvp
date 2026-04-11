# MEMO — AFTR Pick Engine
**Fecha:** 2026-03-28
**Branch activo:** `main`
**Estado:** Refactor en curso (`services/refresh.py` → módulos especializados)

---

## 1. ¿Qué es este sistema?

**AFTR Pick Engine** es una plataforma web de análisis de picks deportivos (fútbol + NBA).
Genera predicciones estadísticas con modelos propios (Poisson + xG dinámico), enriquece con cuotas de mercado, y las publica en un dashboard web con gating por suscripción.

**Target:** Usuarios que quieren picks con valor real vs. mercado, clasificados por score de confianza (AFTR Score 0–100).

---

## 2. Stack Tecnológico

| Capa | Tecnología |
|------|-----------|
| Backend | Python 3, **FastAPI** + Uvicorn (ASGI) |
| Frontend | HTML/CSS/JS vanilla + **Jinja2** (server-rendered) |
| Mobile | Capacitor/Android + TypeScript *(parcial, no funcional)* |
| Base de datos | **SQLite** (sin ORM, raw SQL) |
| Cache de datos | **JSON files** en `data/cache/` |
| Auth | Cookies firmadas con `itsdangerous` + bcrypt/passlib |
| Billing | **Stripe** (checkout sessions + webhooks) |
| Deploy | **Render.com** (blueprint en `render.yaml`) |
| Config | Variables de entorno + `.env` (python-dotenv) |

---

## 3. Estructura de Directorios

```
engine/
├── app/                    # Aplicación FastAPI
│   ├── main.py             # Entrypoint: registra routers, lifespan, middleware
│   ├── auth.py             # Login, register, sesión, reset password
│   ├── payments.py         # Stripe checkout + webhooks
│   ├── db.py               # Conexión SQLite + init_db()
│   ├── models.py           # get_active_plan() → FREE/PREMIUM/PRO
│   ├── auto_refresh.py     # Spawn de tareas background tiered
│   ├── cli.py              # python -m app.cli refresh
│   ├── ui.py               # Orquestador de páginas HTML
│   ├── ui_home.py          # Página principal
│   ├── ui_dashboard.py     # Dashboard por liga
│   ├── ui_data.py          # Carga y agregación desde cache
│   ├── ui_picks_calc.py    # ROI, ranking, cálculos de picks
│   ├── ui_stats.py         # KPIs: winrate, ROI, net units
│   ├── ui_combos.py        # Renderizado de combos/parlays
│   ├── ui_matches.py       # Display de partidos (live status)
│   ├── ui_card.py          # Componente card de pick
│   ├── ui_team.py          # Crests/logos de equipos
│   ├── ui_helpers.py       # Utilidades comunes de UI
│   ├── ui_account.py       # Página de cuenta
│   ├── timefmt.py          # UTC ↔ Argentina timezone
│   ├── email_utils.py      # SMTP para reset password
│   ├── user_helpers.py     # can_see_all_picks(), is_premium_active()
│   └── routes/
│       ├── matches.py      # GET /api/matches
│       ├── picks.py        # GET /api/picks, /api/combos, /api/stats/summary
│       ├── user.py         # Favoritos y tracking de picks
│       └── live.py         # Actualizaciones live
│
├── core/                   # Lógica matemática pura
│   ├── poisson.py          # Modelo Poisson + corrección Dixon-Coles  ← MODIFICADO
│   ├── model_b.py          # xG dinámico desde forma de equipo         ← MODIFICADO
│   ├── evaluation.py       # WIN/LOSS/PUSH por mercado
│   ├── value.py            # Tier thresholds (SAFE/MEDIUM/SPICY)
│   ├── combos.py           # Construcción de parlays
│   ├── basketball_picks.py # Picks NBA
│   └── basketball_evaluation.py
│
├── services/               # Orquestación del pipeline de datos
│   ├── refresh.py          # Orquestador principal  ← EN REFACTOR (−1563 líneas)
│   ├── refresh_league.py   # Lógica por liga       ← NUEVO (untracked)
│   ├── refresh_teams.py    # Form y stats de equipos ← NUEVO (untracked)
│   ├── refresh_picks.py    # Generación de picks    ← NUEVO (untracked)
│   ├── refresh_results.py  # Aplicar resultados     ← NUEVO (untracked)
│   ├── refresh_odds.py     # Enriquecimiento cuotas ← NUEVO (untracked)
│   ├── refresh_combos.py   # Construcción combos    ← NUEVO (untracked)
│   ├── refresh_utils.py    # Parseo y utilidades    ← NUEVO (untracked)
│   ├── refresh_basketball.py # Pipeline NBA
│   ├── tiered_refresh.py   # Scheduler LIVE/UPCOMING/RESULTS
│   ├── refresh_rate_guard.py # Rate limiting + backoff
│   └── aftr_score.py       # AFTR Score 0–100       ← MODIFICADO
│
├── data/
│   ├── cache.py            # read/write JSON cache
│   └── providers/
│       ├── football_data.py       # Football-Data.org API client
│       ├── api_sports_basketball.py # API-Sports (NBA)
│       ├── odds_football.py       # The Odds API
│       └── team_form.py           # Cálculo de métricas de forma
│
├── config/
│   └── settings.py         # Configuración centralizada desde env vars
│
├── static/                 # Assets frontend
│   ├── style.css           # Estilos principales (112KB)
│   ├── aftr-ui.js          # Interactividad UI
│   ├── league_carousel.js  # Carrusel de ligas
│   ├── home_league_carousel.js
│   └── sw.js               # Service worker (PWA)
│
├── tests/                  # pytest
├── scripts/                # PowerShell + Python scripts de utilidad
├── daily/                  # Fallback legacy (solo lectura)
├── models/                 # enums.py, schemas.py (mínimos)
├── templates/              # Jinja2 (components/ — mínimo por ahora)
├── android/                # Capacitor Android (parcial)
├── requirements.txt
├── render.yaml
└── .env / .env.example
```

---

## 4. Base de Datos

### SQLite — `aftr.db`

| Tabla | Propósito | Columnas clave |
|-------|-----------|----------------|
| `users` | Cuentas | id, email, username, password_hash, role, stripe_customer_id |
| `subscriptions` | Planes | user_id, plan (FREE/PREMIUM/PRO), expires_at |
| `password_reset_tokens` | Reset flow | token_hash, user_id, expires_at, used_at |
| `user_favorites` | Picks guardados | user_id, pick_id, action, market, aftr_score |
| `user_picks` | Tracking picks | user_id, pick_id, result, market, settled_at |

**No ORM** — raw SQL directo con `sqlite3`. Init en `app/db.py:init_db()`.

### Cache JSON — `data/cache/`

| Archivo | Contenido |
|---------|-----------|
| `daily_matches_{LEAGUE}.json` | Partidos por liga |
| `daily_picks_{LEAGUE}.json` | Picks generados por liga |
| `daily_combos.json` | Combos/parlays globales |
| `picks_history_{LEAGUE}.json` | Historial de picks resueltos |
| `team_form_{TEAM_ID}_30.json` | Forma reciente por equipo |
| `team_names.json` | Mapa ID ↔ nombre de equipo |
| `cache_meta.json` | Estado del refresh (lock, last_updated) |

---

## 5. Modelo de Predicción

### Flujo de generación de picks

```
Football-Data API → matches + team history
        ↓
Model A (xG estático: home=1.45, away=1.15)
    o
Model B (xG dinámico desde últimos 30 días, weighted by recency)   ← default
        ↓
core/poisson.py → distribución Poisson + corrección Dixon-Coles
        ↓
Probabilidades: 1X2, Over/Under 2.5, BTTS
        ↓
The Odds API → cuotas de mercado
        ↓
services/aftr_score.py → AFTR Score (0–100)
```

### AFTR Score — `services/aftr_score.py`

Ponderación:
- `model_score` (prob. del modelo) → **35%**
- `value_score` (edge vs. implied odds) → **35%**
- `form_score` (form_diff equipo) → **15%**
- `xg_score` (xg_diff) → **15%**

Tiers resultantes:
| Score | Tier |
|-------|------|
| ≥ 85 | `elite` |
| ≥ 70 | `strong` |
| ≥ 55 | `risky` |
| < 55 | `pass` |

Guardrails: edge ≤ 0 → tier baja a máximo `risky`. prob < 0.45 → no puede ser `elite`.

### Combos/Parlays — `services/refresh_combos.py`

- Tiers: `SAFE` / `MEDIUM` / `SPICY` con umbrales de confianza distintos
- Prevención de overlap (no duplicar legs)
- Ventana de 3 días para combos `next-3d`

---

## 6. APIs Externas

| API | Base URL | Auth | Uso |
|-----|----------|------|-----|
| **Football-Data.org** | `https://api.football-data.org/v4/` | Header `X-Auth-Token` | Partidos, standings, historial de equipos |
| **API-Sports** | `https://api-sports.io/v1/` | Header `x-rapidapi-key` | Partidos NBA |
| **The Odds API** | `https://api.the-odds-api.com/v4/` | Query param `apiKey` | Cuotas de mercado fútbol |
| **Stripe** | `https://api.stripe.com` | Secret key | Checkout, suscripciones, webhooks |

**Rate limiting:** Football-Data = 10 req/min (free tier). Manejado por `services/refresh_rate_guard.py` con backoff exponencial hasta 120s (configurable).

---

## 7. Endpoints REST Internos

### Picks & Matches
```
GET  /api/picks?league=PL            → picks de una liga (JSON)
GET  /api/picks/{league}             → alias
GET  /api/combos                     → combos globales Safe/Medium/Spicy
GET  /api/stats/summary?league=PL   → KPIs (ROI, winrate, net units)
GET  /api/matches?league=PL         → partidos por liga
GET  /api/matches/by-day            → agrupados por fecha
```

### Auth
```
POST /auth/register                  → crear cuenta
POST /auth/login                     → form login → cookie sesión
POST /auth/login/json                → JSON login (para Android)
GET  /auth/me                        → usuario actual (401 si no logueado)
GET  /auth/logout                    → limpiar sesión
POST /auth/forgot-password           → enviar email de reset
POST /auth/reset-password            → nueva contraseña con token
```

### Billing
```
POST /billing/create-checkout-session → Stripe checkout
GET  /billing/success                 → redirect post-pago
POST /webhooks/stripe                 → eventos Stripe (firma verificada)
```

### Status
```
GET  /health                          → {"status": "ok"}
GET  /api/status                      → refresh_running, last_update, picks_total
```

### UI (HTML server-rendered)
```
GET  /                                → Home dashboard
GET  /league/{code}                   → Dashboard de liga
GET  /account                         → Cuenta de usuario
GET  /auth/login                      → Form de login
GET  /auth/register                   → Form de registro
```

---

## 8. Auto-Refresh — Tiered Scheduler

Iniciado automáticamente en el lifespan de FastAPI si `AUTO_REFRESH=true`.

| Tier | Intervalo default | Propósito |
|------|-------------------|-----------|
| LIVE | 60 segundos | Partidos en curso |
| UPCOMING | 15 minutos | Próximos partidos y picks |
| RESULTS | 10 minutos | Resultados de partidos finalizados |

Archivo: `app/auto_refresh.py` → `spawn_auto_refresh_tasks()`
Scheduler: `services/tiered_refresh.py`

---

## 9. Autenticación y Billing

- **Sesiones:** Cookie firmada con `itsdangerous` → `{"uid": user_id}`
- **Passwords:** bcrypt/passlib
- **Planes:** FREE → PREMIUM → PRO determinados por tabla `subscriptions`
- **Gating UI:** free users ven top 3 picks; premium ve todos
- **Premium filter:** `aftr_score >= 70 AND edge > 0` → `services/aftr_score.py:filter_premium_picks()`
- **Stripe flow:** checkout → webhook `checkout.session.completed` → activa plan en DB

---

## 10. Ligas Soportadas

| Código | Liga |
|--------|------|
| PL | Premier League (Inglaterra) |
| PD | La Liga (España) |
| SA | Serie A (Italia) |
| BL1 | Bundesliga (Alemania) |
| FL1 | Ligue 1 (Francia) |
| ELC | Championship (Inglaterra) |
| DED | Eredivisie (Holanda) |
| PPL | Primeira Liga (Portugal) |
| CL | Champions League |
| EL | Europa League |
| BSA | Brasileirão Serie A |
| EC | Euros / Copa |
| WC | World Cup |
| CLI | Copa Libertadores |
| NBA | NBA Basketball |

---

## 11. Configuración de Entorno

### Variables clave

| Variable | Default | Propósito |
|----------|---------|-----------|
| `AFTR_SECRET_KEY` | `dev-secret-change-me` | Firma de cookies |
| `AFTR_DB_PATH` | `./aftr.db` | Path SQLite |
| `AFTR_CACHE_DIR` | `data/cache` | Path cache JSON |
| `APP_BASE_URL` | — | URL pública (para emails/Stripe) |
| `FOOTBALL_DATA_API_KEY` | — | Requerida |
| `API_SPORTS_KEY` | — | Requerida para NBA |
| `ODDS_API_KEY` | — | Opcional |
| `STRIPE_SECRET_KEY` | — | Producción |
| `STRIPE_WEBHOOK_SECRET` | — | Producción |
| `AUTO_REFRESH` | `true` | Scheduler background |
| `AFTR_PICKS_MODEL` | `B` | Modelo A (estático) o B (dinámico) |
| `LIVE_REFRESH_SECONDS` | `60` | Intervalo live |
| `UPCOMING_REFRESH_MIN` | `15` | Intervalo upcoming |
| `RESULTS_REFRESH_MIN` | `10` | Intervalo results |
| `COOKIE_SECURE` | auto (HTTPS) | Secure flag en cookies |

### Dev vs Prod

```
# .env local (dev)
AFTR_SECRET_KEY=dev-secret
AUTO_REFRESH=false
AFTR_DEBUG=1
AFTR_LOG_LEVEL=DEBUG
AFTR_CACHE_DIR=./data/cache
AFTR_DB_PATH=./aftr.db

# Render.com (prod)
AFTR_DB_PATH=/var/data/aftr.db
AFTR_CACHE_DIR=/var/data/cache
COOKIE_SECURE=true
AUTO_REFRESH=true
APP_BASE_URL=https://aftr-api.onrender.com
```

---

## 12. Entry Points

| Forma | Comando |
|-------|---------|
| Servidor web | `uvicorn app.main:app --host 127.0.0.1 --port 8000` |
| Refresh manual | `python -m app.cli refresh` |
| Tests | `pytest tests/` |
| Daily (Windows) | `scripts/run_daily.ps1` |
| Deploy | `render.yaml` → build: `pip install -r requirements.txt` |

---

## 13. Estado Actual del Código (2026-03-28)

### Cambios en working tree (no commiteados)

| Archivo | Estado | Nota |
|---------|--------|------|
| `services/refresh.py` | **Modificado** (−1563 líneas) | Refactor: monolito → módulos especializados |
| `services/refresh_combos.py` | **Nuevo** (untracked) | Extraído de refresh.py |
| `services/refresh_league.py` | **Nuevo** (untracked) | Extraído de refresh.py |
| `services/refresh_odds.py` | **Nuevo** (untracked) | Extraído de refresh.py |
| `services/refresh_picks.py` | **Nuevo** (untracked) | Extraído de refresh.py |
| `services/refresh_results.py` | **Nuevo** (untracked) | Extraído de refresh.py |
| `services/refresh_teams.py` | **Nuevo** (untracked) | Extraído de refresh.py |
| `services/refresh_utils.py` | **Nuevo** (untracked) | Extraído de refresh.py |
| `core/model_b.py` | **Modificado** | Ajustes modelo xG dinámico |
| `core/poisson.py` | **Modificado** | Ajustes distribución + Dixon-Coles |
| `services/aftr_score.py` | **Modificado** | Ajustes scoring/guardrails |

> El refactor del pipeline de refresh es el trabajo principal en curso.
> `services/refresh.py` pasó de ser un monolito de ~1600 líneas a ser un orquestador delgado que re-exporta desde los sub-módulos.

### Últimos commits

```
7780e5f fix: replace drum carousel with native scroll-snap
fb0b4ea fix: carousel class names + viewport height for drum 3D mode
e0cf2d1 feat: UX visual improvements — gauge, badges, skeleton, stagger
fd98563 feat: drum/wheel 3D league carousel
fbd3b35 fix: router not defined in ui_account.py on import
03e15af refactor: split app/ui.py (6068 lines) into 11 focused modules
90f014f fix: activate premium on billing success when webhook is missing
1baba4d fix: homepage fallback + persistent snapshot
```

---

## 14. Dependencias Python

```
fastapi            # Web framework
uvicorn            # ASGI server
python-dotenv      # Carga .env
requests           # HTTP client para APIs externas
pytest             # Testing
passlib            # Hashing de contraseñas
bcrypt==4.0.1      # Backend bcrypt (pinneado)
python-multipart   # Parsing de formularios
stripe             # SDK Stripe
itsdangerous       # Firma de cookies de sesión
tzdata             # Datos de timezone
jinja2             # Templates HTML
```

---

## 15. Notas de Arquitectura

1. **Sin ORM** — SQLite directo. Justificado por la simpleza del schema (solo usuarios/auth/billing). Los datos de picks/partidos no pasan por DB.
2. **Cache JSON es la fuente de verdad de picks** — el pipeline de refresh escribe, las rutas web leen. No hay DB de picks.
3. **Model B es el default** — xG dinámico con ventana de 30 días, ponderado por recencia. Model A (estático) disponible con `AFTR_PICKS_MODEL=A`.
4. **Separación dura: refresh ≠ web server** — el refresh es CPU-bound y corre en threads separados o como proceso CLI. El web server solo lee cache.
5. **Frontend 100% server-rendered** — no SPA, no React. Jinja2 genera HTML completo en cada request. JS es mínimo (interactividad, carousel, modales).
6. **PWA** — service worker + manifest para instalación en móvil. Mobile nativo (Capacitor) está en standby.
7. **Premium gating** — se controla en `app/user_helpers.py` y en los renderers UI. El criterio de picks premium es `aftr_score >= 70 AND edge > 0`.
