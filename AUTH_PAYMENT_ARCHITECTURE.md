# AFTR V3 — Auth & payment architecture

## 1. Current setup (audit answers)

### Database
- **Engine:** **SQLite** (no PostgreSQL).
- **Definition:** `app/db.py` — `get_conn()` returns a `sqlite3` connection; path from `config.settings.DB_PATH` (env: `AFTR_DB_PATH` or `DB_PATH`, default: `base_dir/aftr.db`).
- **Init:** `init_db()` in `app/db.py` creates tables; called from `app/main.py` at startup.

### User “model”
- **No ORM.** User data is the **`users` table** schema in `app/db.py` and the **dict** returned by `app.auth.get_user_by_id(user_id)`.
- **Tables:** `users` (id, email, username, password_hash, role, subscription_*, stripe_*, created_at, updated_at), `subscriptions` (user_id, plan, expires_at, created_at), `password_reset_tokens`, `leads`.

### Auth/session
- **Session:** Signed cookie `aftr_session` (itsdangerous, salt `aftr-session`), payload `{"uid": user_id}`. No JWT.
- **Resolution:** `app.auth.get_user_id(request)` reads cookie and returns `user_id` or `None`; `get_user_by_id(uid)` loads from DB and enriches with `get_active_plan(uid)` for display.

### Routes (before this pass)

| Route | Method | Purpose |
|-------|--------|--------|
| `/auth/register` | POST | JSON body: create user, set session, return JSON |
| `/auth/signup` | POST | Lead capture (email only), no password |
| `/auth/login` | POST | Form: email, password → redirect + cookie |
| `/auth/logout` | GET | Clear cookie, redirect |
| `/auth/forgot-password` | POST | JSON: send reset email |
| `/auth/reset-password` | GET | Redirect to reset form (token in query) |
| `/auth/reset-password` | POST | JSON: token + new password |
| `/premium/manual-activate` | GET | Dev: set PREMIUM for current user (requires login) |
| `/billing/create-checkout-session` | POST | Stripe Checkout URL (requires login) |
| `/billing/success` | GET | Redirect after checkout |
| `/webhooks/stripe` | POST | Stripe webhook (checkout.session.completed only before) |

### Premium determination
- **Source of truth:** `app.models.get_active_plan(user_id)` — reads `subscriptions` table, returns `"FREE"` if no row or `expires_at` in the past, else `plan` (e.g. `"PREMIUM"`, `"PRO"`).
- **UI helpers:** `app.user_helpers.is_premium_active(user)` (role `premium_user` and status `active`/`trial`); `can_see_all_picks(user, request)` = admin or `is_premium_active(user)`.
- **Persistence:** Premium survives restarts because it is stored in SQLite (`subscriptions` + `users.role` / `users.subscription_status` / `subscription_end`). No in-memory-only flags.

---

## 2. What was implemented or completed (this pass)

- **GET /auth/me** — Returns current user (id, email, username, role, subscription_status, plan). 401 if not logged in. Uses existing `get_user_id` + `get_user_by_id` + `get_active_plan`.
- **POST /auth/login/json** — JSON body `{ "email", "password" }`; sets `aftr_session` cookie and returns same shape as `/auth/me` so API/Android can login and persist session.
- **Stripe webhook** — Extended to:
  - Use Stripe subscription `current_period_end` for `expires_at` when available (checkout.session.completed).
  - Handle **customer.subscription.updated** (sync active/trialing → PREMIUM with correct expiry; other statuses → revoke).
  - Handle **customer.subscription.deleted** (revoke premium).  
  Helpers: `_apply_premium_to_user`, `_revoke_premium_for_user`, `_uid_from_subscription_id`.

No new tables; no change to password hashing (bcrypt via passlib), existing register/login/logout, or premium gating logic.

---

## 3. Files modified

| File | Change |
|------|--------|
| `app/auth.py` | Added `GET /auth/me`; added `POST /auth/login/json` (Body, set cookie, return user + plan). |
| `app/payments.py` | Webhook: use subscription period end; handle `customer.subscription.updated` and `customer.subscription.deleted`; extracted `_apply_premium_to_user`, `_revoke_premium_for_user`, `_uid_from_subscription_id`. |

No changes to: `app/db.py`, `app/models.py`, `app/user_helpers.py`, `app/main.py`, `app/ui.py` (beyond what was already there for auth/premium).

---

## 4. Routes after this pass

| Route | Method | Purpose |
|-------|--------|--------|
| `/auth/register` | POST | Create user (JSON), set session, return JSON |
| `/auth/login` | POST | Form login, redirect + cookie |
| **`/auth/login/json`** | **POST** | **JSON login, Set-Cookie + JSON user (API/Android)** |
| `/auth/logout` | GET | Clear session, redirect |
| **`/auth/me`** | **GET** | **Current user + plan; 401 if not logged in** |
| `/auth/forgot-password` | POST | Request reset email |
| `/auth/reset-password` | GET | Reset form (token in query) |
| `/auth/reset-password` | POST | Submit new password (JSON) |
| `/auth/signup` | POST | Lead capture (email only) |
| `/premium/manual-activate` | GET | Manual PREMIUM for current user (dev) |
| `/billing/create-checkout-session` | POST | Create Stripe Checkout (requires login) |
| `/billing/success` | GET | Post-checkout redirect |
| `/webhooks/stripe` | POST | Checkout + subscription.updated + subscription.deleted |

---

## 5. Premium gating (where it lives)

- **Place:** `app/ui.py` in the **league dashboard** view: `can_see_all_picks_val = can_see_all_picks(user, request)`.
  - **Free user in free league:** top 3 picks + “Unlock Premium” card.
  - **Free user in premium league:** locked grid + “Esta liga es Premium”.
  - **Premium (or admin):** full picks grid.
- **Definition of “premium”:** `app/user_helpers.can_see_all_picks()` → `is_admin()` or `is_premium_active(user)`; `is_premium_active` uses `user.role == "premium_user"` and `user.subscription_status in ("active", "trial")`. User dict is enriched by `get_user_by_id()` with `get_active_plan()` so role/status reflect the `subscriptions` table.

---

## 6. Environment variables required

| Variable | Used by | Purpose |
|----------|---------|--------|
| `AFTR_SECRET_KEY` | auth (cookie signing) | Secret for `aftr_session` cookie; must be set in production. |
| `AFTR_DB_PATH` or `DB_PATH` | app/db, config | SQLite file path (e.g. on Render persistent disk). |
| `STRIPE_SECRET_KEY` | payments | Stripe API key. |
| `STRIPE_PUBLISHABLE_KEY` | Frontend (Stripe.js / checkout) | Shown in client. |
| `STRIPE_PRICE_ID` | payments | Price ID for subscription checkout. |
| `STRIPE_WEBHOOK_SECRET` | payments | Webhook signature verification; **required** for subscription.updated/deleted and safe checkout handling. |
| `APP_BASE_URL` | payments, auth (reset link) | Public base URL (e.g. `https://aftr-api.onrender.com`) for redirects and emails. |
| `SMTP_*` / `EMAIL_FROM` | auth (forgot-password) | Optional; for password reset emails. |

No keys or passwords are hardcoded; all come from env (or config loaded from env).

---

## 7. What is still missing for end-to-end premium

1. **Stripe Dashboard**
   - Webhook endpoint: `https://<your-host>/webhooks/stripe` with events: `checkout.session.completed`, `customer.subscription.updated`, `customer.subscription.deleted`.
   - Copy the signing secret into `STRIPE_WEBHOOK_SECRET`.

2. **Persistent DB on Render**
   - Set `AFTR_DB_PATH` to a path on a **persistent disk** so users and subscriptions survive restarts/redeploys.

3. **API-level gating (optional)**
   - `/api/picks`, `/api/combos`, `/api/stats/summary` currently return full data with no auth. If the Android app calls these directly (not the HTML dashboard), you may want to add a dependency that checks `get_user_id` + `can_see_all_picks` and returns 403 or limited data for free users. Today, gating is only in the **server-rendered dashboard** (ui.py).

4. **CORS / Android**
   - If the app calls the API from a different origin, ensure CORS allows your app origin and that credentials (cookies) are sent (e.g. `credentials: 'include'`). No CORS changes were made in this pass.

5. **Logout for API**
   - `GET /auth/logout` returns a redirect. For a pure API client you may want a response that only clears the cookie (e.g. 204) without redirect; optional follow-up.

---

## 8. Quick reference: auth flow

- **Register:** `POST /auth/register` with `{ "email", "username", "password", "confirm_password" }` → 200 + `Set-Cookie: aftr_session` + `{ "ok": true, "username": "..." }`.
- **Login (browser):** `POST /auth/login` form `email`, `password` → 302 to `/?msg=login_ok` + `Set-Cookie: aftr_session`.
- **Login (API/Android):** `POST /auth/login/json` with `{ "email", "password" }` → 200 + `Set-Cookie: aftr_session` + `{ "ok": true, "user": { "id", "email", "username", "role", "subscription_status", "plan" } }`.
- **Current user:** `GET /auth/me` with cookie → 200 + `{ "ok": true, "user": { ... } }` or 401.
- **Checkout:** Frontend calls `POST /billing/create-checkout-session` (with cookie) → redirect user to Stripe; after success Stripe redirects to `/billing/success`; backend receives `checkout.session.completed` (and later `customer.subscription.updated` / `customer.subscription.deleted`) and updates `subscriptions` + `users`. Next request, `get_active_plan(uid)` and `can_see_all_picks` reflect premium.
