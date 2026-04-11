# MEMO — AFTR Pick Engine
**Fecha:** 2026-04-11
**Branch activo:** `main`
**Estado:** Producción en Render — funcional

---

## 1. ¿Qué es este sistema?

**AFTR** es una plataforma web de análisis de picks deportivos (fútbol + NBA).
Genera predicciones estadísticas con modelos propios (Poisson + xG dinámico), enriquece con cuotas de mercado, y las publica en un dashboard web con gating por suscripción freemium.

**Target:** Usuarios que quieren picks con valor real vs. mercado, clasificados por AFTR Score (0–100).
**Modelo de negocio:** FREE (top 3 picks) → PREMIUM (todos los picks + combos) vía Mercado Pago.

---

## 2. Stack Tecnológico

| Capa | Tecnología |
|------|-----------|
| Backend | Python 3, **FastAPI** + Uvicorn (ASGI) |
| Frontend | HTML/CSS/JS vanilla + **Jinja2** (server-rendered) |
| Mobile | **PWA** (instalable) — TWA para Play Store en progreso |
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
│   ├── email_utils.py      # Emails con Resend (en test mode, pendiente dominio)
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
│   ├── refresh_teams.py    # Form y cache de nombres de equipos
│   ├── refresh_rate_guard.py # Rate limiting + backoff
│   ├── live_events.py      # Detección de eventos live + push (goles, FT, etc.)
│   ├── push_notifications.py # Envío de push via pywebpush
│   ├── auto_settle.py      # Liquidación automática de bets
│   └── aftr_score.py       # AFTR Score 0–100
│
├── data/
│   ├── cache.py            # read/write JSON cache
│   └── providers/
│       ├── api_football.py        # API-Football (api-sports.io) — proveedor principal
│       ├── football_data.py       # Football-Data.org — fallback ligas sin APIF
│       ├── api_sports_basketball.py # API-Sports (NBA)
│       ├── odds_football.py       # The Odds API
│       └── team_form.py           # Cálculo de métricas de forma
│
├── config/
│   └── settings.py         # Configuración centralizada desde env vars
│
├── static/                 # Assets frontend
│   ├── style.css           # Estilos principales
│   ├── aftr-ui.js          # Interactividad UI
│   ├── aftr-push.js        # Lógica de suscripción push
│   ├── aftr-tracker.js     # Tracker de apuestas
│   ├── aftr-bankroll.js    # Gestión de bankroll
│   ├── aftr-share.js       # Compartir picks
│   ├── home_lc3d.js        # Carrusel 3D de ligas
│   ├── sw.js               # Service Worker (PWA + push)
│   ├── manifest.webmanifest # PWA manifest
│   ├── leagues/            # Logos de ligas (PNG)
│   └── teams/              # Escudos de equipos
│
├── scripts/                # Scripts de utilidad
│   ├── list_apif_leagues.py  # Verificar IDs de ligas en API-Football
│   └── test_live_events.py   # Test del sistema de live events
├── tests/                  # pytest
├── models/                 # enums.py
├── requirements.txt
└── render.yaml
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

**Migrado de SQLite a PostgreSQL.** Raw SQL con psycopg2, sin ORM.

### Cache JSON — `/var/data/cache/` (Render Persistent Disk)

| Archivo | Contenido |
|---------|-----------|
| `daily_matches_{LEAGUE}.json` | Partidos por liga (con status live + minuto) |
| `daily_picks_{LEAGUE}.json` | Picks generados por liga |
| `daily_combos.json` | Combos/parlays globales |
| `picks_history_{LEAGUE}.json` | Historial de picks resueltos |
| `live_events_state.json` | Estado de notificaciones live (evita duplicados) |
| `push_notified_cache.json` | Cache de notificaciones ya enviadas (TTL 2h) |
| `team_names.json` | Mapa ID ↔ nombre de equipo |

---

## 5. APIs Externas

| API | Base URL | Auth | Uso |
|-----|----------|------|-----|
| **API-Football** (api-sports.io) | `https://v3.football.api-sports.io` | Header `x-apisports-key` | Proveedor principal: fixtures, live, standings |
| **Football-Data.org** | `https://api.football-data.org/v4/` | Header `X-Auth-Token` | Fallback para ligas sin ID en APIF |
| **API-Sports** | Basketball API | Header `x-apisports-key` | Partidos NBA |
| **The Odds API** | `https://api.the-odds-api.com/v4/` | Query param `apiKey` | Cuotas de mercado |
| **Mercado Pago** | `https://api.mercadopago.com` | Access token | Checkout, webhooks ✅ |
| **Resend** | `https://api.resend.com` | API key | Emails transaccionales (en test mode, sin dominio propio aún) |

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
| EL | Europa League | ⏳ pendiente refresh | ✅ |
| CONF | Conference League | ⏳ | — |
| ELC | Championship | ✅ | ✅ |
| DED | Eredivisie | ✅ | ✅ |
| PPL | Primeira Liga | ✅ | ✅ |
| FAC | FA Cup | ⏳ pendiente refresh | ✅ |
| CREY | Copa del Rey | ⏳ pendiente refresh | ✅ |
| CLI | Copa Libertadores | ✅ | ✅ |
| BSA | Brasileirão | ✅ | ✅ |
| ARG | Liga Argentina | ⏳ pendiente refresh | ✅ |
| MLS | MLS | ⏳ pendiente refresh | ✅ |
| EC | Eurocopa | ✅ (data de 2024) | ✅ |
| WC | World Cup | ✅ (data histórica) | ✅ |
| NBA | NBA | ✅ | ✅ |

---

## 7. Sistema de Push Notifications

- **VAPID keys** configuradas en Render (VAPID_PRIVATE_KEY, VAPID_PUBLIC_KEY, VAPID_EMAIL)
- **Suscripciones** guardadas en tabla `push_subscriptions` en PostgreSQL
- **Envío** via `pywebpush` con conversión PEM automática (`_vapid_private_key_pem()`)
- **Service Worker** (`sw.js`) maneja `push` y `notificationclick`
- **`requireInteraction: true`** — notificaciones quedan en pantalla hasta que el usuario las toca

### Eventos que disparan push
| Evento | Cuándo |
|--------|--------|
| Upcoming pick | 30 min antes del partido (pick seguido) |
| Kickoff | Cuando arranca el partido |
| Gol | Cada gol (reemplaza notificación anterior) |
| Half-time | Fin del 1er tiempo |
| 2° tiempo | Inicio del 2do tiempo |
| Full-time | Resultado final |
| Trial expiring | 48h antes de que venza el trial |

**Tag único por partido** — todos los eventos de un mismo partido usan `live-{fix_id}`, reemplazando la notificación anterior. Efecto: score que se actualiza en el lock screen.

---

## 8. Auto-Refresh — Tiered Scheduler

Iniciado automáticamente en el lifespan de FastAPI si `AUTO_REFRESH=true`.

| Tier | Intervalo default | Propósito |
|------|-------------------|-----------|
| LIVE | 60 segundos | Partidos en curso + live events + minutos |
| UPCOMING | 15 minutos | Próximos partidos y picks |
| RESULTS | 10 minutos | Resultados de partidos finalizados |

Round-robin: `AUTO_REFRESH_LEAGUES_PER_CYCLE=4` ligas por ciclo.
Ligas con ID en `APIF_LEAGUE_MAP` → `refresh_apifootball.py`
Ligas sin ID → `football_data.py` (fallback)

---

## 9. Modelo de Predicción

```
API-Football → fixtures + team history
        ↓
Model B (xG dinámico — default) o Model A (xG estático)
        ↓
core/poisson.py → Poisson + Dixon-Coles
        ↓
Probabilidades: 1X2, Over/Under, BTTS
        ↓
The Odds API → cuotas de mercado
        ↓
services/aftr_score.py → AFTR Score (0–100)
```

### AFTR Score

| Componente | Peso |
|-----------|------|
| model_score (prob. modelo) | 35% |
| value_score (edge vs. odds) | 35% |
| form_score (forma equipo) | 15% |
| xg_score (xg_diff) | 15% |

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
- **Planes:** FREE / PREMIUM determinados por `subscription_status` + `subscription_end` en `users`
- **Gating:** free users ven picks limitados; premium ve todos + combos
- **Mercado Pago:** checkout sessions + webhooks → activa plan en DB ✅
- **LemonSqueezy:** descartado (cuenta rechazada)
- **Stripe:** configurado pero no activo (reemplazado por MP)

---

## 11. Variables de Entorno en Render

| Variable | Propósito |
|----------|-----------|
| `DATABASE_URL` | PostgreSQL connection string |
| `AFTR_SECRET_KEY` | Firma de cookies |
| `API_FOOTBALL_KEY` | api-sports.io (32 chars hex) |
| `FOOTBALL_DATA_API_KEY` | Football-Data.org fallback |
| `API_SPORTS_KEY` | NBA |
| `ODDS_API_KEY` | The Odds API |
| `VAPID_PRIVATE_KEY` | Push notifications |
| `VAPID_PUBLIC_KEY` | Push notifications |
| `VAPID_EMAIL` | Push notifications |
| `MP_ACCESS_TOKEN` | Mercado Pago |
| `MP_WEBHOOK_SECRET` | Mercado Pago |
| `RESEND_API_KEY` | Emails |
| `APP_BASE_URL` | URL pública |
| `AUTO_REFRESH` | `true` en prod |

---

## 12. Estado Mobile / PWA

| Canal | Estado |
|-------|--------|
| PWA (web) | ✅ Instalable — manifest + service worker |
| Android TWA (Play Store) | 🔧 En progreso — pendiente bubblewrap + assetlinks.json |
| iOS App Store | ⏳ Requiere Mac + Apple Developer ($99/año) |

**Estrategia Play Store:** TWA (Trusted Web Activity) via Bubblewrap.
La app nativa con Capacitor está en standby — se reemplazó por TWA.

### Checklist TWA
- [ ] `manifest.webmanifest` completo con todos los campos requeridos
- [ ] `/.well-known/assetlinks.json` en el servidor
- [ ] Cuenta Google Play Developer ($25 USD, pago único)
- [ ] Generar APK con `bubblewrap build`
- [ ] Screenshots + descripción para el store listing

---

## 13. Pendientes / Roadmap

### Bloqueantes para launch completo
- [ ] Dominio propio (para Resend + credibilidad)
- [ ] Términos y condiciones / Política de privacidad
- [ ] Email de trial (Resend en test mode, necesita dominio)

### Próximo
- [ ] TWA → Play Store
- [ ] Racha de picks (streak display)
- [ ] Share de picks
- [ ] Bankroll tracking avanzado
- [ ] EL, ARG, MLS, FAC, CREY con datos reales (pendiente refresh API-Football)

---

## 14. Entry Points

| Forma | Comando |
|-------|---------|
| Servidor web | `uvicorn app.main:app --host 0.0.0.0 --port 8000` |
| Refresh manual una liga | `python -c "from services.refresh_apifootball import apif_refresh_league; print(apif_refresh_league('PL'))"` |
| Tests | `pytest tests/` |
| Deploy | Push a `main` → Render auto-deploy |
