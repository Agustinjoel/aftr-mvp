/**
 * aftr-premium.js
 * Shared premium modal — works on any AFTR page.
 * Injects #premium-modal if not already present, provides global
 * openPremium() / closePremium() / activatePremium(provider).
 */
(function () {
  'use strict';

  var MODAL_ID = 'premium-modal';

  function injectModal() {
    if (document.getElementById(MODAL_ID)) return; // already present (home page)

    var el = document.createElement('div');
    el.id = MODAL_ID;
    el.className = 'modal-backdrop';
    el.style.display = 'none';
    el.innerHTML = [
      '<div class="modal modal--wide">',
        '<div class="modal-head">',
          '<div class="modal-title">AFTR &mdash; Planes</div>',
          '<button class="modal-x" onclick="closePremium()">&#x2715;</button>',
        '</div>',
        '<div class="modal-body">',
          '<div class="plan-compare">',
            '<div class="plan-col plan-col--free">',
              '<div class="plan-col-name">Gratis</div>',
              '<div class="plan-col-price-line"><span class="plan-price-num">$0</span></div>',
              '<ul class="plan-col-list">',
                '<li class="plan-item plan-item--yes">Picks diarios con AFTR Score</li>',
                '<li class="plan-item plan-item--yes">Notificaciones antes del partido</li>',
                '<li class="plan-item plan-item--yes">Tracker personal de apuestas</li>',
                '<li class="plan-item plan-item--yes">Favoritos e historial propio</li>',
                '<li class="plan-item plan-item--no">Todos los picks del d&iacute;a</li>',
                '<li class="plan-item plan-item--no">Ligas adicionales</li>',
                '<li class="plan-item plan-item--no">Combos inteligentes de valor</li>',
              '</ul>',
              '<a href="/?auth=register" class="plan-col-btn plan-col-btn--free" onclick="closePremium();">Crear cuenta</a>',
            '</div>',
            '<div class="plan-col plan-col--premium">',
              '<div class="plan-col-badge">Recomendado</div>',
              '<div class="plan-col-name">&#11088; Premium</div>',
              '<div class="plan-col-price-line"><span class="plan-price-num">$9.99</span><span class="plan-price-sub">/mes USD</span></div>',
              '<ul class="plan-col-list">',
                '<li class="plan-item plan-item--yes">Todo lo del plan gratis</li>',
                '<li class="plan-item plan-item--yes">Todos los picks del d&iacute;a</li>',
                '<li class="plan-item plan-item--yes">Todas las ligas disponibles</li>',
                '<li class="plan-item plan-item--yes">Combos inteligentes de valor</li>',
                '<li class="plan-item plan-item--yes">Picks de alto AFTR Score</li>',
                '<li class="plan-item plan-item--yes">Edge y an&aacute;lisis completo</li>',
              '</ul>',
              '<div id="premium-modal-cta" class="checkout-btns">',
                '<button class="pill modal-cta modal-cta--mp" onclick="activatePremium(\'mp\')">Pagar con Mercado Pago</button>',
              '</div>',
            '</div>',
          '</div>',
        '</div>',
      '</div>'
    ].join('');

    document.body.appendChild(el);

    // Close on backdrop click
    el.addEventListener('click', function (e) {
      if (e.target === el) closePremium();
    });

    // Check if already premium and swap CTA
    fetch('/user/me', { credentials: 'include' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        var role = (data.role || '').toLowerCase();
        var status = (data.subscription_status || '').toLowerCase();
        var isActive = role === 'premium_user' && status === 'active';
        var isOnTrial = role === 'premium_user' && status === 'trial';
        if (isActive) {
          var cta = document.getElementById('premium-modal-cta');
          if (cta) cta.innerHTML = '<div class="premium-badge">&#11088; Premium activo</div>';
        } else if (isOnTrial) {
          var cta = document.getElementById('premium-modal-cta');
          if (cta) cta.innerHTML = [
            '<p style="font-size:.8rem;color:#FFD700;margin:0 0 10px;">Tenés un trial activo. Activá ahora para no perder acceso.</p>',
            '<button class="pill modal-cta modal-cta--mp" onclick="activatePremium(\'mp\')">Activar Premium con Mercado Pago</button>'
          ].join('');
        }
      })
      .catch(function () {});
  }

  window.openPremium = function () {
    injectModal();
    var m = document.getElementById(MODAL_ID);
    if (m) { m.style.display = 'flex'; }
    document.body.style.overflow = 'hidden';
  };

  window.closePremium = function () {
    var m = document.getElementById(MODAL_ID);
    if (m) { m.style.display = 'none'; }
    document.body.style.overflow = '';
  };

  window.activatePremium = function (provider) {
    var base = window.location.origin || (window.location.protocol + '//' + window.location.host);
    var url = provider === 'mp'
      ? base + '/billing/mp-checkout'
      : base + '/billing/create-checkout-session';
    fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: '{}'
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
      .then(function (result) {
        if (result.ok && result.data && result.data.url) {
          window.location.href = result.data.url;
        } else if (result.data && result.data.error === 'need_login') {
          closePremium();
          if (typeof openLoginModal === 'function') openLoginModal();
          else window.location.href = '/?auth=login';
        } else {
          alert('No se pudo iniciar el pago: ' + ((result.data && result.data.error) || 'error desconocido'));
        }
      })
      .catch(function () {
        alert('Error de conexión con el servidor de pagos.');
      });
  };

  // Init on DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', injectModal);
  } else {
    injectModal();
  }
})();
