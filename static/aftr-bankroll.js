(function () {
  var el   = document.getElementById('bankroll-display');
  var form = document.getElementById('bankroll-form');
  if (!el) return;

  fetch('/user/bankroll')
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (!d.ok) {
        var msg = d.error === 'premium_required'
          ? 'Requiere suscripción premium activa.'
          : 'Error al cargar bankroll (' + (d.error || 'desconocido') + ')';
        el.innerHTML = '<p class="muted">' + msg + '</p>';
        return;
      }

      var cur2    = d.currency || 'ARS';
      var cur     = d.current_bankroll;
      var ini     = d.initial_amount;
      var pnl     = d.total_pnl;
      var settled = d.total_picks_settled;

      var fmtOpts = { minimumFractionDigits: 2, maximumFractionDigits: 2 };
      var curStr  = cur.toLocaleString('es-AR', fmtOpts);
      var pnlStr  = (pnl >= 0 ? '+' : '') + pnl.toLocaleString('es-AR', fmtOpts);
      var pnlCls  = pnl >= 0 ? 'bk-pnl--pos' : 'bk-pnl--neg';

      el.innerHTML =
        '<div class="bankroll-display-inner">' +
          '<div class="bk-current">' +
            '<span class="bk-label">Capital actual</span>' +
            '<span class="bk-amount">' + cur2 + ' ' + curStr + '</span>' +
          '</div>' +
          '<div class="bk-pnl ' + pnlCls + '">' +
            '<span class="bk-label">P&L total</span>' +
            '<span class="bk-pnl-val">' + cur2 + ' ' + pnlStr + ' (' + settled + ' picks)</span>' +
          '</div>' +
          '<button class="pill bk-edit-btn" type="button" id="bk-edit-btn">Editar configuraci\u00f3n</button>' +
        '</div>';

      if (form) {
        var bi = document.getElementById('br-initial');
        var bs = document.getElementById('br-stake');
        var bc = document.getElementById('br-currency');
        if (bi) bi.value = ini;
        if (bs) bs.value = d.stake_per_unit || 1000;
        if (bc) bc.value = cur2;
        var editBtn = document.getElementById('bk-edit-btn');
        if (editBtn) editBtn.addEventListener('click', function () {
          form.style.display = form.style.display === 'none' ? 'block' : 'none';
        });
      }
    })
    .catch(function () {
      if (el) el.innerHTML = '<p class="muted">Sin datos.</p>';
    });

  if (form) {
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var ini = parseFloat(document.getElementById('br-initial').value);
      var stk = parseFloat(document.getElementById('br-stake').value);
      var cur = document.getElementById('br-currency').value;
      if (!ini || !stk) return;
      fetch('/user/bankroll', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ initial_amount: ini, stake_per_unit: stk, currency: cur })
      })
        .then(function (r) { return r.json(); })
        .then(function (d) { if (d.ok) location.reload(); })
        .catch(function () {});
    });
  }
})();
