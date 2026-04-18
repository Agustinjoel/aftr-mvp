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

      var cur2     = d.currency || 'ARS';
      var cur      = d.current_bankroll;
      var ini      = d.initial_amount;
      var pnl      = d.total_pnl;
      var settled  = d.total_picks_settled;
      var mvmts    = d.movements || [];

      var fmtOpts = { minimumFractionDigits: 2, maximumFractionDigits: 2 };
      var fmt = function (n) { return Number(n).toLocaleString('es-AR', fmtOpts); };
      var curStr  = fmt(cur);
      var pnlStr  = (pnl >= 0 ? '+' : '') + fmt(pnl);
      var pnlCls  = pnl >= 0 ? 'bk-pnl--pos' : 'bk-pnl--neg';
      var roiPct  = ini > 0 ? (pnl / ini * 100) : 0;
      var roiStr  = (roiPct >= 0 ? '+' : '') + roiPct.toFixed(1) + '%';

      // ── Gráfico Daily Profit ──────────────────────────────────────────────
      var chartHtml = '';
      if (mvmts.length > 0) {
        chartHtml = _renderProfitChart(mvmts, ini, cur2, fmt);
      }

      el.innerHTML =
        '<div class="bankroll-display-inner">' +
          '<div class="bk-current">' +
            '<span class="bk-label">Capital actual</span>' +
            '<span class="bk-amount">' + cur2 + ' ' + curStr + '</span>' +
          '</div>' +
          '<div class="bk-row" style="display:flex;gap:16px;flex-wrap:wrap;margin-top:4px">' +
            '<div class="bk-pnl ' + pnlCls + '">' +
              '<span class="bk-label">P&L total</span>' +
              '<span class="bk-pnl-val">' + cur2 + ' ' + pnlStr + '</span>' +
            '</div>' +
            '<div style="color:#94a3b8">' +
              '<span class="bk-label">ROI</span>' +
              '<span class="bk-pnl-val" style="color:' + (roiPct >= 0 ? '#22c55e' : '#ef4444') + '">' + roiStr + '</span>' +
            '</div>' +
            '<div style="color:#94a3b8">' +
              '<span class="bk-label">Apuestas</span>' +
              '<span class="bk-pnl-val">' + (mvmts.length || settled) + '</span>' +
            '</div>' +
          '</div>' +
          chartHtml +
          '<button class="pill bk-edit-btn" type="button" id="bk-edit-btn" style="margin-top:12px">Editar configuraci\u00f3n</button>' +
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

  // ── Helpers de gráfico ────────────────────────────────────────────────────

  function _renderProfitChart(mvmts, initial, currency, fmt) {
    var W = 320, H = 120, PAD = { top: 10, right: 12, bottom: 28, left: 48 };
    var innerW = W - PAD.left - PAD.right;
    var innerH = H - PAD.top - PAD.bottom;

    // Puntos: start + each movement
    var points = [{ balance: initial, label: '' }];
    mvmts.forEach(function (m) {
      points.push({ balance: m.balance, label: m.date || '', type: m.type });
    });

    var balances = points.map(function (p) { return p.balance; });
    var minB = Math.min.apply(null, balances);
    var maxB = Math.max.apply(null, balances);
    var range = maxB - minB || 1;
    // Add 5% padding
    minB -= range * 0.05;
    maxB += range * 0.05;
    range = maxB - minB;

    function xPos(i) { return PAD.left + (i / (points.length - 1 || 1)) * innerW; }
    function yPos(b) { return PAD.top + innerH - ((b - minB) / range) * innerH; }

    // SVG path
    var pathD = points.map(function (p, i) {
      return (i === 0 ? 'M' : 'L') + xPos(i).toFixed(1) + ',' + yPos(p.balance).toFixed(1);
    }).join(' ');

    // Area fill (down to initial_amount line or bottom)
    var baselineY = yPos(initial).toFixed(1);
    var areaD = pathD
      + ' L' + xPos(points.length - 1).toFixed(1) + ',' + baselineY
      + ' L' + xPos(0).toFixed(1) + ',' + baselineY + ' Z';

    var lastBalance = points[points.length - 1].balance;
    var isProfit = lastBalance >= initial;
    var lineColor  = isProfit ? '#22c55e' : '#ef4444';
    var areaColor  = isProfit ? 'rgba(34,197,94,0.12)' : 'rgba(239,68,68,0.10)';

    // Y axis labels (min, initial, max)
    var yLabels = [
      { val: maxB, y: yPos(maxB) },
      { val: initial, y: yPos(initial) },
      { val: minB, y: yPos(minB) },
    ];

    var yLabelsSvg = yLabels.map(function (l) {
      return '<text x="' + (PAD.left - 4) + '" y="' + l.y.toFixed(1)
        + '" fill="#6b7280" font-size="9" text-anchor="end" dominant-baseline="middle">'
        + _shortNum(l.val) + '</text>';
    }).join('');

    // X axis: first and last date
    var xLabelsSvg = '';
    if (points.length > 1 && points[1].label) {
      xLabelsSvg +=
        '<text x="' + xPos(1).toFixed(1) + '" y="' + (H - 4)
        + '" fill="#6b7280" font-size="9" text-anchor="middle">' + points[1].label + '</text>';
    }
    if (points.length > 2 && points[points.length - 1].label) {
      xLabelsSvg +=
        '<text x="' + xPos(points.length - 1).toFixed(1) + '" y="' + (H - 4)
        + '" fill="#6b7280" font-size="9" text-anchor="end">' + points[points.length - 1].label + '</text>';
    }

    // Dots for each movement
    var dotsSvg = points.slice(1).map(function (p, i) {
      var dotColor = p.type === 'WIN' ? '#22c55e' : p.type === 'LOSS' ? '#ef4444' : '#94a3b8';
      var cx = xPos(i + 1).toFixed(1);
      var cy = yPos(p.balance).toFixed(1);
      return '<circle cx="' + cx + '" cy="' + cy + '" r="3" fill="' + dotColor + '" opacity="0.85">'
        + '<title>' + (p.type || '') + ' ' + currency + ' ' + fmt(p.balance) + ' (' + (p.label || '') + ')</title>'
        + '</circle>';
    }).join('');

    // Baseline (initial_amount) dashed line
    var baselineSvg =
      '<line x1="' + PAD.left + '" y1="' + baselineY
      + '" x2="' + (W - PAD.right) + '" y2="' + baselineY
      + '" stroke="#475569" stroke-width="1" stroke-dasharray="4,3"/>';

    var svg =
      '<svg width="' + W + '" height="' + H + '" viewBox="0 0 ' + W + ' ' + H
      + '" style="display:block;max-width:100%;overflow:visible" xmlns="http://www.w3.org/2000/svg">'
      + '<path d="' + areaD + '" fill="' + areaColor + '" />'
      + baselineSvg
      + '<path d="' + pathD + '" fill="none" stroke="' + lineColor + '" stroke-width="2" stroke-linejoin="round"/>'
      + dotsSvg
      + yLabelsSvg
      + xLabelsSvg
      + '</svg>';

    return (
      '<div style="margin-top:14px">'
      + '<div style="font-size:11px;color:#6b7280;margin-bottom:4px">Curva de capital (tracker)</div>'
      + svg
      + '</div>'
    );
  }

  function _shortNum(n) {
    if (Math.abs(n) >= 1000000) return (n / 1000000).toFixed(1) + 'M';
    if (Math.abs(n) >= 1000)    return (n / 1000).toFixed(1) + 'k';
    return Math.round(n).toString();
  }

})();

// ── Función global: sugerencia de stake Kelly para un pick concreto ──────────
window.aftrSuggestStake = function (decimalOdds, aftrProb, onResult) {
  var url = '/user/bankroll?odds=' + encodeURIComponent(decimalOdds)
          + '&aftr_prob=' + encodeURIComponent(aftrProb);
  fetch(url)
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (d.ok && d.suggested_stake != null) {
        onResult({ stake: d.suggested_stake, note: d.suggested_note, currency: d.currency });
      } else {
        onResult(null);
      }
    })
    .catch(function () { onResult(null); });
};
