from __future__ import annotations

import html as html_lib
from fastapi import Request
from fastapi.responses import HTMLResponse

from app.auth import get_user_id, get_user_by_id
from app.ui_helpers import AUTH_BOOTSTRAP_SCRIPT


def tracker_page(request: Request) -> HTMLResponse:
    uid = get_user_id(request)
    if not uid:
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/?auth=login")

    user = get_user_by_id(uid) or {}
    username = html_lib.escape(str(user.get("username") or user.get("email") or ""))

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>AFTR Tracker</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="stylesheet" href="/static/style.css?v=32">
  <link rel="icon" type="image/png" href="/static/logo_aftr.png">
  <link rel="manifest" href="/static/manifest.json">
  <link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
  <meta name="theme-color" content="#0d1117">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="apple-mobile-web-app-title" content="AFTR">
</head>
<body class="tracker-body">

  <header class="tracker-header">
    <a href="/" class="tracker-header-logo">
      <img src="/static/logo_aftr.png" alt="AFTR" class="tracker-header-logo-img">
      <span>Tracker</span>
    </a>
    <div class="tracker-header-right">
      <span class="tracker-header-user muted">{username}</span>
      <a href="/account" class="pill tracker-header-pill">Mi cuenta</a>
    </div>
  </header>

  <main class="tracker-main">

    <!-- Resumen P&L -->
    <div id="tracker-summary" class="tracker-summary" style="display:none">
      <div class="tracker-summary-item">
        <span class="tracker-summary-num" id="ts-apostado">—</span>
        <span class="tracker-summary-label">apostado</span>
      </div>
      <div class="tracker-summary-item tracker-summary-item--pnl">
        <span class="tracker-summary-num" id="ts-pnl">—</span>
        <span class="tracker-summary-label">resultado</span>
      </div>
      <div class="tracker-summary-item">
        <span class="tracker-summary-num" id="ts-roi">—</span>
        <span class="tracker-summary-label">ROI</span>
      </div>
    </div>

    <!-- Tabs -->
    <div class="tracker-tabs">
      <button class="tracker-tab tracker-tab--active" data-tab="activas">En juego / Pendientes</button>
      <button class="tracker-tab" data-tab="historial">Historial</button>
    </div>

    <!-- Lista de apuestas -->
    <div id="tracker-list" class="tracker-list">
      <p class="muted tracker-empty" id="tracker-empty" style="display:none">No hay apuestas en esta sección.</p>
    </div>

    <!-- Botón nueva apuesta -->
    <button class="tracker-fab" id="tracker-fab" title="Nueva apuesta">+</button>

  </main>

  <!-- Modal nueva apuesta -->
  <div id="tracker-modal" class="tracker-modal" style="display:none" role="dialog" aria-modal="true">
    <div class="tracker-modal-inner">
      <div class="tracker-modal-header">
        <h2 class="tracker-modal-title">Nueva apuesta</h2>
        <button class="tracker-modal-close" id="tracker-modal-close" aria-label="Cerrar">&times;</button>
      </div>

      <div class="tracker-form-row">
        <label class="tracker-form-label">Tipo</label>
        <div class="tracker-type-toggle">
          <button class="tracker-type-btn tracker-type-btn--active" data-type="simple">Simple</button>
          <button class="tracker-type-btn" data-type="combinada">Combinada</button>
        </div>
      </div>

      <!-- Piernas -->
      <div id="legs-container"></div>

      <button class="tracker-add-leg-btn" id="tracker-add-leg" style="display:none">+ Agregar selección</button>

      <div class="tracker-form-row tracker-form-row--stake">
        <label class="tracker-form-label" for="t-stake">Stake apostado</label>
        <input type="number" id="t-stake" class="tracker-input" placeholder="2000" min="1" step="any">
      </div>

      <div class="tracker-payout-preview" id="tracker-payout-preview" style="display:none">
        <span class="tracker-payout-label">Ganancia potencial</span>
        <span class="tracker-payout-num" id="tracker-payout-num">—</span>
      </div>

      <div class="tracker-form-row">
        <label class="tracker-form-label" for="t-note">Nota (opcional)</label>
        <input type="text" id="t-note" class="tracker-input" placeholder="Ej: cuota encontrada en Codere">
      </div>

      <button class="pill tracker-submit-btn" id="tracker-submit">Guardar apuesta</button>
      <p class="tracker-form-error muted" id="tracker-form-error" style="display:none"></p>
    </div>
  </div>
  <div id="tracker-overlay" class="tracker-overlay" style="display:none"></div>

  <script src="/static/aftr-tracker.js" defer></script>
  {AUTH_BOOTSTRAP_SCRIPT}
</body>
</html>"""
    return HTMLResponse(html)
