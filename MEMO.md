# MEMO — AFTR Pick Engine
**Fecha:** 2026-04-11
**Branch activo:** `main`
**Estado:** Producción en Render — funcional

---

## 1. ¿Qué es este sistema?

**AFTR** es una plataforma web de análisis de picks deportivos (fútbol + NBA).
Genera predicciones estadísticas con modelos propios (Poisson + xG dinámico), enriquece con cuotas de mercado, y las publica en un dashboard web con gating por suscripción freemium.

**Target:** Usuarios que quieren picks con valor real vs. mercado, clasificados por AFTR Score (0–100).
**Modelo de negocio:** FREE (top 5 picks) → PREMIUM (todos los picks + combos) vía Mercado Pago.

---

## 2. Stack Tecnológico

| Capa | Tecnología |
|------|-----------|
| Backend | Python 3, **FastAPI** + Uvicorn (ASGI) |
| Frontend | HTML/CSS/JS vanilla + **Jinja2** (server-rendered) |
| Mobile | **PWA** (instalable) + **TWA** en Google Play Store ✅ |
| Base de datos | **PostgreSQL** en Render (migrado desde SQLite) |
| Cache de datos | **JSON files** en `/var/data/cache/` (Render disk) |
| Auth | Cookies firmadas con `itsdangerous` + bcrypt/passlib |
| Billing | **Mercado Pago** (checkout + webhooks) ✅ activo |
| Push notifications | **pywebpush** + VAPID keys + Service Worker |
| Deploy | **Render.com** — Web Service + PostgreSQL + Persistent Disk |
| Config | Variables de entorno en Render dashboard |

---

## 3. Estructura de Directorios

```
engine/
├── app/                    # Aplicación FastAPI
│   ├── main.py             # Entrypoint: routers, lifespan, middleware
│   ├── auth.py             # Login, register, sesión, reset password
│   ├── payments.py         # Mercado Pago checkout + webhooks
│   ├── db.py               # Conexión PostgreSQL + init_db()
│   ├── models.py           # get_active_plan() → FREE/PREMIUM/PRO
│   ├── auto_refresh.py     # Spawn de tareas background tiered
│   ├── ui.py               # Orquestador de páginas HTML
│   ├── ui_home.py          # Página principal + carrusel de ligas
│   ├── ui_dashboard.py     # Dashboard por liga
│   ├── ui_account.py       # Cuenta de usuario + streaks + tracker
│   ├── ui_data.py          # Carga y agregación desde cache
│   ├── ui_picks_calc.py    # ROI, ranking, cálculos de picks
│   ├── ui_stats.py         # KPIs: winrate, ROI, net units
│   ├── ui_combos.py        # Renderizado de combos/parlays
│   ├── ui_matches.py       # Display de partidos (live status, minuto)
│   ├── ui_card.py          # Componente card de pick
│   ├── ui_team.py          # Crests/logos de equipos
│   ├── ui_helpers.py       # Utilidades comunes de UI
│   ├── ui_rendimiento.py   # Página de rendimiento histórico
│   ├── timefmt.py          # UTC ↔ Argentina timezone
│   ├── email_utils.py      # Emails con Resend (dominio aftrapp.online verificado)
│   ├── user_helpers.py     # can_see_all_picks(), is_premium_active()
│   └── routes/
│       ├── matches.py      # GET /api/matches
│       ├── picks.py        # GET /api/picks, /api/combos, /api/stats/summary
│       ├── user.py         # Favoritos, tracking, push subscriptions
│       ├── live.py         # Actualizaciones live
│       ├── tracker.py      # Tracker de apuestas (bets + legs)
│       └── premium.py      # Endpoints premium/admin
│
├── core/                   # Lógica matemática pura
│   ├── poisson.py          # Modelo Poisson + corrección Dixon-Coles
│   ├── model_b.py          # xG dinámico desde forma de equipo
│   ├── evaluation.py       # WIN/LOSS/PUSH por mercado
│   ├── value.py            # Tier thresholds (SAFE/MEDIUM/RISKY)
│   ├── ranking.py          # Ranking de picks por AFTR Score
│   ├── combos.py           # Construcción de parlays
│   ├── basketball_picks.py # Picks NBA
│   └── basketball_evaluation.py
│
├── services/               # Orquestación del pipeline de datos
│   ├── tiered_refresh.py   # Scheduler LIVE/UPCOMING/RESULTS (round-robin)
│   ├── refresh_apifootball.py  # Pipeline principal: API-Football → picks
│   ├── refresh_league.py   # Lógica por liga (football-data.org fallback)
│   ├── refresh_teams.py    # Form y stats de equipos
│   ├── refresh_picks.py    # Generación de picks
│   ├── refresh_results.py  # Aplicar resultados
│   ├── refresh_odds.py     # Enriquecimiento cuotas
│   ├── refresh_combos.py   # Construcción combos
│   ├── refresh_utils.py    # Parseo y utilidades (_normalize_match, etc.)
│   ├── refresh_basketball.py # Pipeline NBA
│   ├── refresh_rate_guard.py # Rate limiting + backoff
│   ├── live_events.py      # Detección de eventos live + push (goles, FT, etc.)
│   ├── push_notifications.py # Envío de push via pywebpush
│   ├── auto_settle.py      # Liquidación automática de bets
│   └── aftr_score.py       # AFTR Score 0–100
│
├── data/
│   ├── cache.py            # read/write JSON cache
│   └── providers/
│       ├── api_football.py        # API-Football (api-sports.io) — proveedor principal + odds
│       ├── football_data.py       # Football-Data.org — fallback ligas sin APIF
│       ├── api_sports_basketball.py # API-Sports (NBA)
│       ├── odds_football.py       # Odds: APIF primero, the-odds-api fallback
│       └── team_form.py           # Cálculo de métricas de forma
│
├── config/
│   └── settings.py         # Configuración centralizada desde env vars
│
├── static/                 # Assets frontend
│   ├── style.css           # Estilos principales
│   ├── aftr-ui.js          # Interactividad UI
│   ├── aftr-push.js        # Lógica de suscripción push (cargado en home + account)
│   ├── aftr-tracker.js     # Tracker de apuestas
│   ├── aftr-bankroll.js    # Gestión de bankroll
│   ├── aftr-share.js       # Compartir picks
│   ├── home_lc3d.js        # Carrusel 3D de ligas
│   ├── sw.js               # Service Worker (PWA + push)
│   ├── manifest.webmanifest # PWA manifest (servido también desde raíz para TWA)
│   ├── leagues/            # Logos de ligas (PNG)
│   └── teams/              # Escudos de equipos
│
└── scripts/                # Scripts de utilidad
```

---

## 4. Base de Datos

### PostgreSQL en Render

| Tabla | Propósito |
|-------|-----------|
| `users` | Cuentas: id, email, username, password_hash, role, subscription_status, subscription_end |
| `push_subscriptions` | Suscripciones push: user_id, endpoint, p256dh, auth |
| `user_picks` | Tracking picks: user_id, pick_id, action (follow/save), result |
| `user_bets` | Tracker bets: user_id, tipo (simple/combinada), estado |
| `bet_legs` | Legs de bets: home_team, away_team, market, kickoff_time, status |
| `password_reset_tokens` | Reset flow |

Raw SQL con psycopg2, sin ORM.

---

## 5. APIs Externas

| API | Base URL | Auth | Uso |
|-----|----------|------|-----|
| **API-Football** (api-sports.io) **Plan Pro** | `https://v3.football.api-sports.io` | `x-apisports-key` | Fixtures, live, standings, **odds** (primario) |
| **Football-Data.org** | `https://api.football-data.org/v4/` | `X-Auth-Token` | Fallback ligas sin APIF ID |
| **API-Sports** | Basketball API | `x-apisports-key` | Partidos NBA |
| **The Odds API** | `https://api.the-odds-api.com/v4/` | `apiKey` | Odds fallback (si ODDS_API_KEY está, si APIF devuelve vacío) |
| **Mercado Pago** | `https://api.mercadopago.com` | Access token | Checkout, webhooks ✅ |
| **Resend** | `https://api.resend.com` | API key | Emails transaccionales — dominio aftrapp.online verificado |

**Nota:** Odds migradas a API-Football Pro como fuente primaria (2026-04-11). The Odds API es fallback.

---

## 6. Ligas Soportadas

| Código | Liga | Datos | Logo |
|--------|------|-------|------|
| PL | Premier League | ✅ | ✅ |
| PD | LaLiga | ✅ | ✅ |
| SA | Serie A | ✅ | ✅ |
| BL1 | Bundesliga | ✅ | ✅ |
| FL1 | Ligue 1 | ✅ | ✅ |
| CL | Champions League | ✅ | ✅ |
| EL | Europa League | ✅ logo | pendiente datos |
| ELC | Championship | ✅ | ✅ |
| DED | Eredivisie | ✅ | ✅ |
| PPL | Primeira Liga | ✅ | ✅ |
| FAC | FA Cup | ✅ logo | pendiente datos |
| CREY | Copa del Rey | ✅ logo | pendiente datos |
| CLI | Copa Libertadores | ✅ | ✅ |
| BSA | Brasileirão | ✅ | ✅ |
| ARG | Liga Argentina | ✅ logo | pendiente datos |
| MLS | MLS | ✅ logo | pendiente datos |
| EC | Eurocopa | data 2024 | ✅ |
| WC | World Cup | data histórica | ✅ |
| NBA | NBA | ✅ | ✅ |

**Pendiente:** correr refresh completo con API-Football para EL, ARG, MLS, FAC, CREY.

---

## 7. Sistema de Push Notifications

- **VAPID keys** en Render (VAPID_PRIVATE_KEY, VAPID_PUBLIC_KEY, VAPID_EMAIL)
- **Suscripciones** en tabla `push_subscriptions` (PostgreSQL)
- **Envío** via `pywebpush` con conversión PEM automática
- **`requireInteraction: true`** — quedan en pantalla hasta que el usuario las toca
- **Tag único por partido** — `live-{fix_id}` para reemplazar notificación anterior (lock screen score)
- **aftr-push.js** cargado en home + account (antes solo account)
- **Prompt post-registro** en trial welcome modal si `Notification.permission === 'default'`

### Eventos push
| Evento | Cuándo |
|--------|--------|
| Upcoming pick | 30 min antes del partido (pick seguido) |
| Kickoff | Cuando arranca |
| Gol | Cada gol (reemplaza anterior) |
| Half-time | Fin 1er tiempo |
| 2° tiempo | Inicio 2do tiempo |
| Full-time | Resultado final |

---

## 8. Auto-Refresh — Tiered Scheduler

Iniciado en lifespan de FastAPI si `AUTO_REFRESH=true`.

| Tier | Intervalo | Propósito |
|------|-----------|-----------|
| LIVE | 60 seg | Partidos en curso + live events + minutos |
| UPCOMING | 15 min | Próximos partidos y picks |
| RESULTS | 10 min | Resultados finalizados |

Round-robin: `AUTO_REFRESH_LEAGUES_PER_CYCLE=4` ligas por ciclo.
Ligas con ID en `APIF_LEAGUE_MAP` → `refresh_apifootball.py`
Ligas sin ID → `football_data.py` (fallback)

**Refresh manual:**
```bash
python -c "import logging; logging.basicConfig(level=logging.INFO); from services.tiered_refresh import run_tiered_refresh; run_tiered_refresh()"
```

---

## 9. Modelo de Predicción

```
API-Football → fixtures + team history
        ↓
Model B (xG dinámico) o Model A (xG estático)
        ↓
core/poisson.py → Poisson + Dixon-Coles
        ↓
Probabilidades: 1X2, Over/Under, BTTS
        ↓
API-Football /odds → cuotas de mercado (Plan Pro)
        ↓
services/aftr_score.py → AFTR Score (0–100)
```

### AFTR Score

| Componente | Peso |
|-----------|------|
| model_score | 35% |
| value_score (edge vs. odds) | 35% |
| form_score | 15% |
| xg_score | 15% |

| Score | Tier |
|-------|------|
| ≥ 85 | `elite` |
| ≥ 70 | `strong` |
| ≥ 55 | `risky` |
| < 55 | `pass` |

---

## 10. Autenticación y Billing

- **Sesiones:** Cookie firmada `itsdangerous` → `{"uid": user_id}`
- **Passwords:** bcrypt/passlib
- **Trial:** 7 días Premium automático al crear cuenta
- **Planes:** FREE / PREMIUM determinados por `subscription_status` + `subscription_end`
- **Gating:** free users ven top 5 picks; premium ve todos + combos
- **Mercado Pago:** checkout + webhooks → activa plan en DB ✅
- **LemonSqueezy:** descartado (rechazados)
- **Endpoints eliminados:** `/auth/signup` (dead code), `/auth/lead` (SQLite incompatible)

---

## 11. Variables de Entorno en Render

| Variable | Propósito |
|----------|-----------|
| `DATABASE_URL` | PostgreSQL connection string |
| `AFTR_SECRET_KEY` | Firma de cookies |
| `API_FOOTBALL_KEY` | api-sports.io Plan Pro |
| `FOOTBALL_DATA_API_KEY` | Football-Data.org fallback |
| `API_SPORTS_KEY` | NBA |
| `ODDS_API_KEY` | The Odds API (fallback, puede estar vacía) |
| `VAPID_PRIVATE_KEY` | Push notifications |
| `VAPID_PUBLIC_KEY` | Push notifications |
| `VAPID_EMAIL` | Push notifications |
| `MP_ACCESS_TOKEN` | Mercado Pago |
| `MP_WEBHOOK_SECRET` | Mercado Pago |
| `RESEND_API_KEY` | Emails |
| `RESEND_FROM` | noreply@aftrapp.online (configurar si no está) |
| `APP_BASE_URL` | URL pública |
| `AUTO_REFRESH` | `true` en prod |
| `TWA_SHA256_FINGERPRINT` | Android TWA — fingerprint del keystore |

---

## 12. Estado Mobile / PWA / TWA

| Canal | Estado |
|-------|--------|
| PWA (web) | ✅ Instalable — manifest + service worker |
| Android TWA (Play Store) | ✅ APK y AAB construidos — pendiente publicar |
| iOS App Store | ⏳ Requiere Mac + Apple Developer ($99/año) |

**TWA completado:**
- Dominio: `aftrapp.online`
- Package: `online.aftrapp.app`
- Keystore SHA256: `E6:28:46:CF:20:E4:FD:F6:9A:99:C9:7D:BC:21:B6:08:9A:5B:12:5B:36:DB:D3:30:31:C0:3E:CD:78:5C:7C:7D`
- `TWA_SHA256_FINGERPRINT` en Render ✅
- `/.well-known/assetlinks.json` sirviendo ✅
- `manifest.webmanifest` en raíz ✅

**Pendiente Play Store:** Cuenta Google Play Developer ($25 USD), screenshots, descripción, política de privacidad.

---

## 13. Fixes UX Recientes (2026-04-11)

- **Live match format:** `2H 90'` → `🔴 90'` (eliminado prefijo confuso de mitad)
- **Stale-live guard:** reducido de 3h30 a 2h15 (partidos pegados como live se resetean antes)
- **Push en home:** `aftr-push.js` ahora se carga en home (antes solo `/account`)
- **Push prompt post-registro:** aparece en trial welcome modal
- **Login modal:** agregado "¿No tenés cuenta? Crear una acá"
- **Odds → API-Football:** eliminado spam de `401 Unauthorized`; odds via Plan Pro de APIF

---

## 14. Pendientes / Roadmap

### Para Play Store
- [ ] Cuenta Google Play Developer ($25 USD)
- [ ] Screenshots de la app (mínimo 2 landscape)
- [ ] Descripción + política de privacidad URL

### Features
- [ ] Email de expiración de trial (Resend, RESEND_FROM configurado)
- [ ] Racha de picks (streak display)
- [ ] Share de picks social
- [ ] "Mis picks guardados" página `/mi-coleccion`
- [ ] Validación real-time en signup (email format, password strength)
- [ ] Datos reales para EL, ARG, MLS, FAC, CREY (refresh pendiente)

---

## 15. Entry Points

| Forma | Comando |
|-------|---------|
| Servidor web | `uvicorn app.main:app --host 0.0.0.0 --port 8000` |
| Refresh manual (con logs) | `python -c "import logging; logging.basicConfig(level=logging.INFO); from services.tiered_refresh import run_tiered_refresh; run_tiered_refresh()"` |
| Refresh una liga | `python -c "from services.refresh_apifootball import refresh_league_apif; import asyncio; asyncio.run(refresh_league_apif('PL'))"` |
| Tests | `pytest tests/` |
| Deploy | Push a `main` → Render auto-deploy |
