(function () {
  'use strict';

  var BASE = '';
  var currentTab = 'activas';
  var betType = 'simple';
  var allBets = [];

  // ── Market labels ──────────────────────────────────────────────────────────
  var MARKET_LABELS = {
    '1':         'Local gana',
    'X':         'Empate',
    '2':         'Visitante gana',
    '1X':        'Gana/Empata local',
    'X2':        'Gana/Empata visitante',
    '12':        'Ambos ganan (no empate)',
    'over_1.5':  'Más de 1.5 goles',
    'over_2.5':  'Más de 2.5 goles',
    'over_3.5':  'Más de 3.5 goles',
    'under_1.5': 'Menos de 1.5 goles',
    'under_2.5': 'Menos de 2.5 goles',
    'btts_yes':  'Ambos marcan (Sí)',
    'btts_no':   'Ambos marcan (No)',
    'dnb_1':     'Draw No Bet local',
    'dnb_2':     'Draw No Bet visitante',
  };

  // ── Status config ──────────────────────────────────────────────────────────
  var BET_STATUS = {
    PENDING:  { label: 'Pendiente',   cls: 'status--pending'  },
    IN_PLAY:  { label: 'En juego',    cls: 'status--inplay'   },
    WON:      { label: 'Ganada ✓',    cls: 'status--won'      },
    LOST:     { label: 'Perdida',     cls: 'status--lost'     },
    VOID:     { label: 'Anulada',     cls: 'status--void'     },
  };

  var LEG_STATUS = {
    PENDING: { icon: '⏳', cls: 'leg-status--pending' },
    WON:     { icon: '✅', cls: 'leg-status--won'     },
    LOST:    { icon: '❌', cls: 'leg-status--lost'    },
    VOID:    { icon: '⚪', cls: 'leg-status--void'    },
    PUSHED:  { icon: '↩️', cls: 'leg-status--pushed'  },
  };

  // ── Helpers ────────────────────────────────────────────────────────────────
  function el(id) { return document.getElementById(id); }

  function fmt(n) {
    if (n == null || isNaN(n)) return '—';
    return Number(n).toLocaleString('es-AR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function fmtOdds(n) {
    if (!n) return '—';
    return Number(n).toFixed(2);
  }

  function fetchJSON(url, opts) {
    return fetch(url, opts || {}).then(function (r) { return r.json(); });
  }

  function betStatusLive(bet) {
    // A bet with all-pending legs but at least one won is IN_PLAY
    return bet.status;
  }

  // ── Summary ────────────────────────────────────────────────────────────────
  function renderSummary(bets) {
    var settled = bets.filter(function (b) { return b.status === 'WON' || b.status === 'LOST'; });
    if (!settled.length) { var s = el('tracker-summary'); if (s) s.style.display = 'none'; return; }

    var totalStaked = 0, totalReturn = 0;
    settled.forEach(function (b) {
      totalStaked += b.stake || 0;
      if (b.status === 'WON') totalReturn += b.potential_payout || 0;
    });
    var pnl = totalReturn - totalStaked;
    var roi = totalStaked > 0 ? (pnl / totalStaked * 100) : 0;

    var s = el('tracker-summary');
    if (!s) return;
    s.style.display = '';
    el('ts-apostado').textContent = 'ARS ' + fmt(totalStaked);
    var pnlEl = el('ts-pnl');
    pnlEl.textContent = (pnl >= 0 ? '+' : '') + 'ARS ' + fmt(Math.abs(pnl));
    pnlEl.className = 'tracker-summary-num ' + (pnl >= 0 ? 'pnl--pos' : 'pnl--neg');
    el('ts-roi').textContent = (roi >= 0 ? '+' : '') + roi.toFixed(1) + '%';
  }

  // ── Bet card ───────────────────────────────────────────────────────────────
  function statusCopyForBet(bet) {
    var legs = bet.legs || [];
    var won   = legs.filter(function (l) { return l.status === 'WON'; }).length;
    var lost  = legs.filter(function (l) { return l.status === 'LOST'; }).length;
    var pend  = legs.filter(function (l) { return l.status === 'PENDING'; }).length;
    var total = legs.length;

    if (bet.status === 'WON')  return '<span class="bet-copy bet-copy--won">cobrada 🎉</span>';
    if (bet.status === 'LOST') return '<span class="bet-copy bet-copy--lost">ya muri\u00f3 ✗</span>';
    if (won === total - 1 && pend === 1) return '<span class="bet-copy bet-copy--close">una m\u00e1s y cobr\u00e1s 👊</span>';
    if (lost === 0 && won > 0) return '<span class="bet-copy bet-copy--alive">segu\u00eds adentro 🟢</span>';
    return '';
  }

  function legActionsHtml(leg, betSettled) {
    // Siempre mostrar botones de corrección (para corregir liquidaciones automáticas erróneas)
    if (leg.status === 'PENDING') {
      return '<div class="leg-actions">' +
        '<button class="leg-btn leg-btn--won"  data-leg="' + leg.id + '" data-status="WON">✓</button>' +
        '<button class="leg-btn leg-btn--lost" data-leg="' + leg.id + '" data-status="LOST">✗</button>' +
        '<button class="leg-btn leg-btn--void" data-leg="' + leg.id + '" data-status="VOID">⊘</button>' +
        '</div>';
    }
    // Leg ya liquidada: mostrar solo botón de corrección discreto
    return '<div class="leg-actions leg-actions--settled">' +
      '<button class="leg-btn leg-btn--edit" data-leg="' + leg.id + '" data-status="PENDING" title="Corregir">✎</button>' +
      '</div>';
  }

  function renderBetCard(bet) {
    var legs = bet.legs || [];
    var settled = bet.status === 'WON' || bet.status === 'LOST';
    var stCfg = BET_STATUS[bet.status] || BET_STATUS['PENDING'];
    var isCombinada = bet.bet_type === 'combinada';
    var copy = isCombinada ? statusCopyForBet(bet) : '';

    var legsHtml = legs.map(function (leg) {
      var lsCfg = LEG_STATUS[leg.status] || LEG_STATUS['PENDING'];
      var mLabel = MARKET_LABELS[leg.market] || leg.market;
      return '<div class="bet-leg ' + lsCfg.cls + '">' +
        '<span class="leg-icon">' + lsCfg.icon + '</span>' +
        '<div class="leg-info">' +
          '<span class="leg-match">' + leg.home_team + ' vs ' + leg.away_team + '</span>' +
          '<span class="leg-market">' + mLabel + ' &middot; @' + fmtOdds(leg.odds) + '</span>' +
        '</div>' +
        legActionsHtml(leg, settled) +
      '</div>';
    }).join('');

    var wonLegs  = legs.filter(function (l) { return l.status === 'WON'; }).length;
    var totalLegs = legs.length;
    var progressHtml = isCombinada
      ? '<div class="bet-progress"><span class="bet-progress-num">' + wonLegs + '/' + totalLegs + '</span> <span class="muted">acertadas</span></div>'
      : '';

    var deleteBtn = settled
      ? '<button class="bet-delete-btn" data-bet="' + bet.id + '" title="Eliminar">🗑</button>'
      : '<button class="bet-delete-btn" data-bet="' + bet.id + '" title="Eliminar">🗑</button>';

    return '<div class="bet-card bet-card--' + bet.status.toLowerCase() + '" id="bet-' + bet.id + '">' +
      '<div class="bet-card-header">' +
        '<div class="bet-card-header-left">' +
          '<span class="bet-type-label">' + (isCombinada ? 'COMBINADA \u00d7' + totalLegs : 'SIMPLE') + '</span>' +
          '<span class="bet-status-badge ' + stCfg.cls + '">' + stCfg.label + '</span>' +
        '</div>' +
        '<div class="bet-card-header-right">' +
          copy +
          deleteBtn +
        '</div>' +
      '</div>' +
      progressHtml +
      '<div class="bet-legs">' + legsHtml + '</div>' +
      '<div class="bet-card-footer">' +
        '<div class="bet-footer-item"><span class="muted">Apostado</span><strong>' + fmt(bet.stake) + '</strong></div>' +
        '<div class="bet-footer-item"><span class="muted">Cuota</span><strong>@' + fmtOdds(bet.total_odds) + '</strong></div>' +
        '<div class="bet-footer-item"><span class="muted">Potencial</span><strong class="payout-val">' + fmt(bet.potential_payout) + '</strong></div>' +
      '</div>' +
      (bet.note ? '<div class="bet-note muted">' + bet.note + '</div>' : '') +
    '</div>';
  }

  // ── Render list ────────────────────────────────────────────────────────────
  function renderList() {
    var list = el('tracker-list');
    var empty = el('tracker-empty');
    if (!list) return;

    var filtered = allBets.filter(function (b) {
      if (currentTab === 'activas') return b.status === 'PENDING' || b.status === 'IN_PLAY';
      return b.status === 'WON' || b.status === 'LOST' || b.status === 'VOID';
    });

    // Remove old cards, keep empty msg
    Array.from(list.querySelectorAll('.bet-card')).forEach(function (n) { n.remove(); });

    if (!filtered.length) {
      if (empty) empty.style.display = '';
      return;
    }
    if (empty) empty.style.display = 'none';
    filtered.forEach(function (bet) {
      var div = document.createElement('div');
      div.innerHTML = renderBetCard(bet);
      list.appendChild(div.firstChild);
    });
  }

  // ── Load bets ──────────────────────────────────────────────────────────────
  function loadBets() {
    fetchJSON(BASE + '/tracker/bets').then(function (d) {
      if (!d.ok) return;
      allBets = d.bets || [];
      renderSummary(allBets);
      renderList();
    }).catch(function () {});
  }

  // ── Tabs ───────────────────────────────────────────────────────────────────
  document.querySelectorAll('.tracker-tab').forEach(function (btn) {
    btn.addEventListener('click', function () {
      currentTab = btn.getAttribute('data-tab');
      document.querySelectorAll('.tracker-tab').forEach(function (b) {
        b.classList.toggle('tracker-tab--active', b === btn);
      });
      renderList();
    });
  });

  // ── Leg resolve ───────────────────────────────────────────────────────────
  document.addEventListener('click', function (e) {
    var btn = e.target.closest && e.target.closest('.leg-btn');
    if (btn) {
      var legId = btn.getAttribute('data-leg');
      var status = btn.getAttribute('data-status');
      btn.disabled = true;
      fetchJSON(BASE + '/tracker/legs/' + legId, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: status }),
      }).then(function (d) {
        if (d.ok) loadBets();
        else btn.disabled = false;
      }).catch(function () { btn.disabled = false; });
    }

    var delBtn = e.target.closest && e.target.closest('.bet-delete-btn');
    if (delBtn) {
      var betId = delBtn.getAttribute('data-bet');
      if (!confirm('¿Eliminar esta apuesta?')) return;
      fetchJSON(BASE + '/tracker/bets/' + betId, { method: 'DELETE' })
        .then(function (d) { if (d.ok) loadBets(); })
        .catch(function () {});
    }
  });

  // ── Modal / form ───────────────────────────────────────────────────────────
  function openModal() {
    el('tracker-modal').style.display = '';
    el('tracker-overlay').style.display = '';
    resetForm();
  }
  function closeModal() {
    el('tracker-modal').style.display = 'none';
    el('tracker-overlay').style.display = 'none';
  }

  el('tracker-fab').addEventListener('click', openModal);
  el('tracker-modal-close').addEventListener('click', closeModal);
  el('tracker-overlay').addEventListener('click', closeModal);

  // ── Bet type toggle ───────────────────────────────────────────────────────
  document.querySelectorAll('.tracker-type-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      betType = btn.getAttribute('data-type');
      document.querySelectorAll('.tracker-type-btn').forEach(function (b) {
        b.classList.toggle('tracker-type-btn--active', b === btn);
      });
      el('tracker-add-leg').style.display = betType === 'combinada' ? '' : 'none';
      syncLegs();
    });
  });

  // ── Leg builder ───────────────────────────────────────────────────────────
  var legCount = 0;

  function marketOptions() {
    return Object.keys(MARKET_LABELS).map(function (k) {
      return '<option value="' + k + '">' + MARKET_LABELS[k] + '</option>';
    }).join('');
  }

  function buildLegHtml(idx) {
    var removeable = idx > 0;
    var n = idx + 1;
    return '<div class="leg-form" id="leg-form-' + idx + '">' +
      '<div class="leg-form-header">' +
        '<span class="leg-form-num">Selecci\u00f3n ' + n + '</span>' +
        (removeable ? '<button type="button" class="leg-remove-btn" data-idx="' + idx + '">&times; Quitar</button>' : '') +
      '</div>' +
      '<div class="leg-form-row">' +
        '<input type="text" class="tracker-input leg-home" placeholder="Local" autocomplete="off">' +
        '<input type="text" class="tracker-input leg-away" placeholder="Visitante" autocomplete="off">' +
      '</div>' +
      '<div class="leg-form-row">' +
        '<select class="tracker-select leg-market">' + marketOptions() + '</select>' +
      '</div>' +
      '<div class="leg-form-row">' +
        '<div class="leg-odds-wrap">' +
          '<label class="leg-odds-label">Cuota decimal</label>' +
          '<input type="number" class="tracker-input leg-odds" placeholder="Ej: 1.85" min="1" step="0.01">' +
        '</div>' +
        '<div class="leg-kickoff-wrap">' +
          '<label class="leg-odds-label">Fecha y hora del partido</label>' +
          '<input type="datetime-local" class="tracker-input leg-kickoff">' +
        '</div>' +
      '</div>' +
    '</div>';
  }

  function syncLegs() {
    var container = el('legs-container');
    if (betType === 'simple') {
      // Only show 1 leg
      if (!container.querySelector('.leg-form')) {
        legCount = 0;
        container.innerHTML = buildLegHtml(0);
        legCount = 1;
      } else {
        // Remove extra legs
        var forms = container.querySelectorAll('.leg-form');
        for (var i = 1; i < forms.length; i++) forms[i].remove();
      }
    }
    updatePayoutPreview();
  }

  function addLeg() {
    var container = el('legs-container');
    var div = document.createElement('div');
    div.innerHTML = buildLegHtml(legCount);
    container.appendChild(div.firstChild);
    legCount++;
    updatePayoutPreview();
  }

  function resetForm() {
    legCount = 0;
    betType = 'simple';
    document.querySelectorAll('.tracker-type-btn').forEach(function (b) {
      b.classList.toggle('tracker-type-btn--active', b.getAttribute('data-type') === 'simple');
    });
    el('tracker-add-leg').style.display = 'none';
    el('legs-container').innerHTML = buildLegHtml(0);
    legCount = 1;
    el('t-stake').value = '';
    el('t-note').value = '';
    el('tracker-payout-preview').style.display = 'none';
    el('tracker-form-error').style.display = 'none';
  }

  el('tracker-add-leg').addEventListener('click', addLeg);

  // Remove leg
  el('legs-container').addEventListener('click', function (e) {
    var btn = e.target.closest && e.target.closest('.leg-remove-btn');
    if (btn) {
      var formEl = btn.closest('.leg-form');
      if (formEl) { formEl.remove(); updatePayoutPreview(); }
    }
  });

  // Live payout preview
  function updatePayoutPreview() {
    var stake = parseFloat(el('t-stake') && el('t-stake').value) || 0;
    var forms = el('legs-container').querySelectorAll('.leg-form');
    var totalOdds = 1.0;
    var valid = true;
    forms.forEach(function (form) {
      var odds = parseFloat(form.querySelector('.leg-odds') && form.querySelector('.leg-odds').value);
      if (!odds || odds < 1) { valid = false; return; }
      totalOdds *= odds;
    });
    var preview = el('tracker-payout-preview');
    var previewNum = el('tracker-payout-num');
    if (stake > 0 && valid && totalOdds > 1) {
      preview.style.display = '';
      previewNum.textContent = 'ARS ' + fmt(stake * totalOdds) + ' (@' + totalOdds.toFixed(2) + ')';
    } else {
      preview.style.display = 'none';
    }
  }

  el('t-stake').addEventListener('input', updatePayoutPreview);
  el('legs-container').addEventListener('input', updatePayoutPreview);

  // ── Submit ────────────────────────────────────────────────────────────────
  el('tracker-submit').addEventListener('click', function () {
    var errEl = el('tracker-form-error');
    errEl.style.display = 'none';

    var stake = parseFloat(el('t-stake').value);
    if (!stake || stake <= 0) { errEl.textContent = 'Ingres\u00e1 el monto apostado.'; errEl.style.display = ''; return; }

    var forms = el('legs-container').querySelectorAll('.leg-form');
    var legs = [];
    var hasError = false;
    forms.forEach(function (form) {
      if (hasError) return;
      var home = (form.querySelector('.leg-home').value || '').trim();
      var away = (form.querySelector('.leg-away').value || '').trim();
      var market = form.querySelector('.leg-market').value;
      var odds = parseFloat(form.querySelector('.leg-odds').value);
      if (!home || !away) { errEl.textContent = 'Complet\u00e1 los equipos en todas las selecciones.'; errEl.style.display = ''; hasError = true; return; }
      if (!odds || odds < 1) { errEl.textContent = 'La cuota debe ser mayor a 1.00.'; errEl.style.display = ''; hasError = true; return; }
      var kickoffEl = form.querySelector('.leg-kickoff');
      var kickoff = kickoffEl && kickoffEl.value ? kickoffEl.value : null;
      legs.push({ home_team: home, away_team: away, market: market, odds: odds, kickoff_time: kickoff });
    });
    if (hasError) return;
    if (!legs.length) { errEl.textContent = 'Agreg\u00e1 al menos una selecci\u00f3n.'; errEl.style.display = ''; return; }

    var btn = el('tracker-submit');
    btn.disabled = true;
    btn.textContent = 'Guardando...';

    fetchJSON(BASE + '/tracker/bets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        bet_type: betType,
        stake: stake,
        legs: legs,
        note: el('t-note').value.trim(),
      }),
    }).then(function (d) {
      btn.disabled = false;
      btn.textContent = 'Guardar apuesta';
      if (d.ok) {
        closeModal();
        loadBets();
      } else {
        errEl.textContent = 'Error: ' + (d.error || 'desconocido');
        errEl.style.display = '';
      }
    }).catch(function () {
      btn.disabled = false;
      btn.textContent = 'Guardar apuesta';
      errEl.textContent = 'Error de conexi\u00f3n.';
      errEl.style.display = '';
    });
  });

  // ── Market key mapper (pick display name → tracker key) ──────────────────
  var MARKET_KEY_MAP = {
    'home win': '1', 'local gana': '1', '1': '1',
    'draw': 'X', 'empate': 'X', 'x': 'X',
    'away win': '2', 'visitante gana': '2', '2': '2',
    '1x': '1X', 'x2': 'X2', '12': '12',
    'over 1.5': 'over_1.5', 'over_1.5': 'over_1.5',
    'over 2.5': 'over_2.5', 'over_2.5': 'over_2.5',
    'over 3.5': 'over_3.5', 'over_3.5': 'over_3.5',
    'under 1.5': 'under_1.5', 'under_1.5': 'under_1.5',
    'under 2.5': 'under_2.5', 'under_2.5': 'under_2.5',
    'btts yes': 'btts_yes', 'btts_yes': 'btts_yes',
    'btts no': 'btts_no', 'btts_no': 'btts_no',
    'dnb home': 'dnb_1', 'dnb_1': 'dnb_1',
    'dnb away': 'dnb_2', 'dnb_2': 'dnb_2',
  };

  function resolveMarketKey(raw) {
    var normalized = (raw || '').toLowerCase().trim();
    return MARKET_KEY_MAP[normalized] || normalized || 'over_1.5';
  }

  function utcIsoToLocalDatetimeInput(utcIso) {
    if (!utcIso) return '';
    try {
      var d = new Date(utcIso);
      if (isNaN(d.getTime())) return '';
      var pad = function (n) { return String(n).padStart(2, '0'); };
      return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) +
             'T' + pad(d.getHours()) + ':' + pad(d.getMinutes());
    } catch (e) { return ''; }
  }

  function prefillLeg(legIdx, home, away, marketKey, localDatetime) {
    var form = el('legs-container').querySelectorAll('.leg-form')[legIdx];
    if (!form) return;
    form.querySelector('.leg-home').value = home || '';
    form.querySelector('.leg-away').value = away || '';
    var sel = form.querySelector('.leg-market');
    if (sel) sel.value = marketKey;
    var kickoffEl = form.querySelector('.leg-kickoff');
    if (kickoffEl && localDatetime) kickoffEl.value = localDatetime;
  }

  // ── Check localStorage for pick prefill ──────────────────────────────────
  function checkPrefill() {
    var raw = localStorage.getItem('aftr_tracker_prefill');
    if (!raw) return;
    try {
      var data = JSON.parse(raw);
      var picks = Array.isArray(data) ? data : [data];
      if (!picks.length) return;

      resetForm(); // empieza con 1 leg en modo simple

      if (picks.length > 1) {
        // Cambiar a modo combinada — el atributo es data-type, no data-bet-type
        var combBtn = document.querySelector('[data-type="combinada"]');
        if (combBtn) combBtn.click();
        // Agregar las legs que faltan (empieza con 1, necesita picks.length)
        while (legCount < picks.length) addLeg();
      }

      picks.forEach(function(pick, idx) {
        prefillLeg(idx, pick.home, pick.away, resolveMarketKey(pick.market), utcIsoToLocalDatetimeInput(pick.utcDate));
      });

      el('tracker-modal').style.display = '';
      el('tracker-overlay').style.display = '';
      localStorage.removeItem('aftr_tracker_prefill');
    } catch (e) {
      localStorage.removeItem('aftr_tracker_prefill');
    }
  }

  // ── Init ──────────────────────────────────────────────────────────────────
  resetForm();
  loadBets();
  checkPrefill();

  // Exponer checkPrefill para que addPickToTracker lo llame desde cualquier página
  window._aftrCheckPrefill = checkPrefill;

})();

// ── Global: called from pick cards on any page ────────────────────────────
window.addPickToTracker = function (btn) {
  var home    = btn.getAttribute('data-home') || '';
  var away    = btn.getAttribute('data-away') || '';
  var market  = btn.getAttribute('data-market') || '';
  var utcDate = btn.getAttribute('data-utcdate') || '';
  var newPick = { home: home, away: away, market: market, utcDate: utcDate };

  // Acumular picks en lugar de pisar — para combinadas
  var existing = [];
  var raw = localStorage.getItem('aftr_tracker_prefill');
  if (raw) {
    try {
      var parsed = JSON.parse(raw);
      existing = Array.isArray(parsed) ? parsed : [parsed];
    } catch (e) { existing = []; }
  }
  existing.push(newPick);
  localStorage.setItem('aftr_tracker_prefill', JSON.stringify(existing));

  if (window.location.pathname === '/tracker') {
    // Ya estamos en el tracker — rellenar el form directamente
    if (typeof window._aftrCheckPrefill === 'function') window._aftrCheckPrefill();
  } else {
    window.location.href = '/tracker';
  }
};
