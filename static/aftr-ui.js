/**
 * aftr-ui.js — Visual enhancements
 *  1. AFTR Score gauge animation
 *  2. Skeleton loader on league navigation
 *  3. Pick card stagger fade-in
 */
(function () {
  "use strict";

  /* ─────────────────────────────────────────────────────────────
     1. AFTR GAUGE — animate stroke-dashoffset on every .aftr-gauge-arc
     Server renders the arc with dashoffset = C (fully hidden).
     JS reads data-gauge-to and transitions to the final value.
  ───────────────────────────────────────────────────────────── */
  function animateGauges() {
    var arcs = document.querySelectorAll(".aftr-gauge-arc");
    arcs.forEach(function (arc, idx) {
      var to = parseFloat(arc.getAttribute("data-gauge-to"));
      if (isNaN(to)) return;
      /* Stagger each card slightly so they don't all fire at once */
      var delay = 80 + idx * 40;
      setTimeout(function () {
        arc.style.transition =
          "stroke-dashoffset 0.85s cubic-bezier(0.34, 1.15, 0.64, 1)";
        arc.style.strokeDashoffset = to;
      }, delay);
    });
  }

  /* ─────────────────────────────────────────────────────────────
     2. SKELETON LOADER
     When the user clicks a league link (navigates), overlay the
     picks section with shimmer skeleton cards until the new page
     loads (browser unloads current page).
  ───────────────────────────────────────────────────────────── */
  var SKELETON_CARD = [
    '<div class="skel-card">',
    '  <div class="skel-line skel-line--sm" style="width:55%"></div>',
    '  <div class="skel-teams">',
    '    <div class="skel-team"><div class="skel-dot"></div><div class="skel-line" style="width:70%"></div></div>',
    '    <div class="skel-vs"></div>',
    '    <div class="skel-team"><div class="skel-dot"></div><div class="skel-line" style="width:65%"></div></div>',
    '  </div>',
    '  <div class="skel-line skel-line--md" style="width:45%"></div>',
    '  <div class="skel-gauge"></div>',
    '  <div class="skel-badges">',
    '    <div class="skel-badge"></div><div class="skel-badge"></div>',
    '  </div>',
    '</div>',
  ].join("\n");

  function buildSkeleton(n) {
    var out = '<div class="skel-grid">';
    for (var i = 0; i < n; i++) out += SKELETON_CARD;
    out += "</div>";
    return out;
  }

  function showSkeleton() {
    /* Try to find the main picks container */
    var container =
      document.querySelector(".picks-grid") ||
      document.querySelector(".picks-section") ||
      document.querySelector(".day-group") ||
      document.querySelector("main");
    if (!container) return;

    var wrap = document.querySelector(".skel-overlay");
    if (wrap) return; /* already showing */

    wrap = document.createElement("div");
    wrap.className = "skel-overlay";
    wrap.innerHTML = buildSkeleton(6);
    /* Position over the container */
    container.style.position = "relative";
    container.appendChild(wrap);
  }

  /* Intercept clicks on league nav links */
  document.addEventListener(
    "click",
    function (e) {
      var a = e.target.closest(
        'a[href*="?league="], a[href*="/?"], .league-item[href], .league-card[href], a.league-item'
      );
      if (!a) return;
      /* Don't show skeleton for same-page JS-only interactions */
      var href = a.getAttribute("href") || "";
      if (!href || href.startsWith("#") || href.startsWith("javascript"))
        return;
      showSkeleton();
    },
    true
  );

  /* ─────────────────────────────────────────────────────────────
     3. PICK CARD STAGGER FADE-IN
     Add .card-stagger-N classes to flip cards so CSS can stagger
     their entrance animation.
  ───────────────────────────────────────────────────────────── */
  function staggerCards() {
    var cards = document.querySelectorAll(".aftr-pick-card");
    cards.forEach(function (card, i) {
      card.classList.add("card-stagger");
      card.style.animationDelay = Math.min(i * 55, 550) + "ms";
    });
  }

  /* ─────────────────────────────────────────────────────────────
     4. COMBOS CAROUSEL
     Simple single-card horizontal slide with dots + mouse/touch drag.
  ───────────────────────────────────────────────────────────── */
  function initComboCarousel() {
    var root  = document.querySelector("[data-combos-carousel]");
    if (!root) return;
    var track = root.querySelector(".combos-car__track");
    if (!track) return;
    var cards = Array.from(track.children);
    var n = cards.length;
    if (n <= 1) return;

    var btnPrev     = root.querySelector(".combos-car__btn--prev");
    var btnNext     = root.querySelector(".combos-car__btn--next");
    var dotsWrap    = root.querySelector(".combos-car__dots");
    var idx         = 0;
    var autoTimer   = null;
    var paused      = false;
    var suppressClk = false;

    /* Build dots */
    var dots = [];
    if (dotsWrap) {
      for (var i = 0; i < n; i++) {
        var dot = document.createElement("button");
        dot.className = "combos-car__dot" + (i === 0 ? " active" : "");
        dot.setAttribute("aria-label", "Combo " + (i + 1));
        (function (ii) {
          dot.addEventListener("click", function () { goTo(ii); });
        })(i);
        dotsWrap.appendChild(dot);
        dots.push(dot);
      }
    }

    function updateDots() {
      dots.forEach(function (d, i) { d.classList.toggle("active", i === idx); });
    }

    function goTo(i, immediate) {
      idx = ((i % n) + n) % n;
      track.style.transition = immediate ? "none" : "transform 0.45s cubic-bezier(0.22, 1, 0.36, 1)";
      track.style.transform  = "translateX(-" + (idx * 100) + "%)";
      updateDots();
      scheduleAuto();
    }

    if (btnPrev) btnPrev.addEventListener("click", function () { goTo(idx - 1); });
    if (btnNext) btnNext.addEventListener("click", function () { goTo(idx + 1); });

    /* Auto-advance */
    function clearAuto() { if (autoTimer) { clearInterval(autoTimer); autoTimer = null; } }
    function scheduleAuto() {
      clearAuto();
      autoTimer = setInterval(function () { if (!paused) goTo(idx + 1); }, 5000);
    }
    root.addEventListener("mouseenter", function () { paused = true; });
    root.addEventListener("mouseleave", function () { paused = false; });

    /* Touch swipe */
    var tx0 = 0;
    track.addEventListener("touchstart", function (e) {
      tx0 = e.touches[0].clientX; paused = true; clearAuto();
    }, { passive: true });
    track.addEventListener("touchend", function (e) {
      var dx = e.changedTouches[0].clientX - tx0;
      if (Math.abs(dx) > 40) goTo(idx + (dx < 0 ? 1 : -1));
      else goTo(idx);
      setTimeout(function () { paused = false; scheduleAuto(); }, 1500);
    }, { passive: true });

    /* Mouse drag */
    var mouseDown = false, mouseX0 = 0, mouseMoved = 0;
    track.addEventListener("pointerdown", function (e) {
      if (e.pointerType !== "mouse") return;
      mouseDown = true; mouseX0 = e.clientX; mouseMoved = 0; suppressClk = false;
      track.setPointerCapture(e.pointerId);
      track.style.transition = "none";
      clearAuto(); paused = true;
    });
    track.addEventListener("pointermove", function (e) {
      if (!mouseDown || e.pointerType !== "mouse") return;
      var dx = e.clientX - mouseX0;
      mouseMoved = Math.max(mouseMoved, Math.abs(dx));
      track.style.transform = "translateX(" + (-(idx * 100) + dx / track.offsetWidth * 100) + "%)";
    });
    function onDragEnd(e) {
      if (!mouseDown || e.pointerType !== "mouse") return;
      mouseDown = false;
      try { track.releasePointerCapture(e.pointerId); } catch (_) {}
      var dx = e.clientX - mouseX0;
      if (mouseMoved > 40) { suppressClk = true; goTo(idx + (dx < 0 ? 1 : -1)); setTimeout(function () { suppressClk = false; }, 200); }
      else goTo(idx);
      paused = false;
    }
    track.addEventListener("pointerup",     onDragEnd);
    track.addEventListener("pointercancel", onDragEnd);
    track.addEventListener("click", function (e) {
      if (suppressClk) { e.preventDefault(); e.stopPropagation(); }
    }, true);

    /* Boot */
    goTo(0, true);
    scheduleAuto();
  }

  /* ─────────────────────────────────────────────────────────────
     Boot on DOMContentLoaded
  ───────────────────────────────────────────────────────────── */
  function boot() {
    animateGauges();
    staggerCards();
    initComboCarousel();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();


// ─────────────────────────────────────────────
// Win Celebration
// ─────────────────────────────────────────────
(function () {
  var STORAGE_KEY = "aftr_celebrated_wins";
  var cel = null;

  function getCelebrated() {
    try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]"); }
    catch (e) { return []; }
  }

  function saveCelebrated(ids) {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(ids.slice(-300))); }
    catch (e) {}
  }

  function buildConfetti(container) {
    var colors = ["#4ade80", "#22c55e", "#a7f3d0", "#fbbf24", "#facc15", "#fff"];
    for (var i = 0; i < 36; i++) {
      var d = document.createElement("div");
      d.className = "win-confetti-dot";
      var size = 5 + Math.random() * 7;
      d.style.cssText = [
        "left:" + (2 + Math.random() * 96) + "%;",
        "background:" + colors[i % colors.length] + ";",
        "width:" + size + "px;height:" + size + "px;",
        "border-radius:" + (Math.random() > 0.5 ? "50%" : "2px") + ";",
        "animation-delay:" + (Math.random() * 0.7) + "s;",
        "animation-duration:" + (1.1 + Math.random() * 1) + "s;"
      ].join("");
      container.appendChild(d);
    }
  }

  function closeOverlay() {
    if (!cel) return;
    cel.classList.add("win-cel-exit");
    var removed = cel;
    setTimeout(function () {
      if (removed && removed.parentNode) removed.parentNode.removeChild(removed);
      if (cel === removed) cel = null;
      document.body.style.overflow = "";
    }, 380);
  }

  function showCelebration(wins) {
    if (!wins.length || cel) return;

    var overlay = document.createElement("div");
    overlay.className = "win-cel-overlay";

    var confWrap = document.createElement("div");
    confWrap.className = "win-cel-confetti";
    buildConfetti(confWrap);
    overlay.appendChild(confWrap);

    var count = wins.length;
    var title = count === 1 ? "¡Ganaste!" : "¡" + count + " victorias!";
    var subtitle = count === 1 ? "Tu pick dio resultado" : "Tus picks dieron resultado";

    var picksHtml = wins.slice(0, 3).map(function (w) {
      var market = w.market || "—";
      var home = w.home_team || w.home || "";
      var away = w.away_team || w.away || "";
      var score = (w.score_home != null && w.score_away != null)
        ? (w.score_home + " - " + w.score_away) : "";
      var matchPart = (home && away)
        ? '<span class="win-cel-teams">' + home + " vs " + away + "</span>"
        : "";
      var scorePart = score
        ? ' <span class="win-cel-score">' + score + "</span>"
        : "";
      return '<div class="win-cel-pick">'
        + '<span class="win-cel-check">✓</span>'
        + '<div><strong>' + market + "</strong>"
        + (matchPart ? " · " + matchPart : "")
        + scorePart + "</div></div>";
    }).join("");

    var firstWin = wins[0] || {};
    var shareBtn = '<button class="win-cel-share aftr-share-trigger"'
      + ' data-home="' + (firstWin.home_team || firstWin.home || "") + '"'
      + ' data-away="' + (firstWin.away_team || firstWin.away || "") + '"'
      + ' data-market="' + (firstWin.market || "") + '"'
      + ' data-aftr-score="' + (firstWin.aftr_score || "") + '"'
      + ' data-tier="' + (firstWin.tier || "") + '"'
      + ' data-score-home="' + (firstWin.score_home != null ? firstWin.score_home : "") + '"'
      + ' data-score-away="' + (firstWin.score_away != null ? firstWin.score_away : "") + '"'
      + '>&#8679; Compartir</button>';

    var box = document.createElement("div");
    box.className = "win-cel-box";
    box.innerHTML = [
      '<div class="win-cel-emoji">🎉</div>',
      '<div class="win-cel-title">' + title + "</div>",
      '<div class="win-cel-subtitle">' + subtitle + "</div>",
      '<div class="win-cel-picks">' + picksHtml + "</div>",
      '<div class="win-cel-foot">',
        shareBtn,
        '<button class="win-cel-btn" id="win-cel-close">¡Genial!</button>',
      '</div>'
    ].join("");

    overlay.appendChild(box);
    document.body.appendChild(overlay);
    cel = overlay;
    document.body.style.overflow = "hidden";

    var timer = setTimeout(closeOverlay, 9000);
    overlay.addEventListener("click", function (e) {
      if (e.target === overlay || e.target.id === "win-cel-close") {
        clearTimeout(timer);
        closeOverlay();
      }
    });
  }

  function checkWins() {
    fetch("/user/history", { credentials: "include" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data || !data.ok || !Array.isArray(data.history)) return;
        var celebrated = getCelebrated();
        var seen = {};
        celebrated.forEach(function (id) { seen[id] = true; });

        var newWins = data.history.filter(function (item) {
          var r = (item.result || "").toUpperCase();
          var id = item.pick_id || "";
          return r === "WIN" && id && !seen[id];
        });

        if (!newWins.length) return;

        var allIds = celebrated.concat(newWins.map(function (w) { return w.pick_id; }));
        saveCelebrated(allIds);
        showCelebration(newWins);
      })
      .catch(function () {});
  }

  function bootCelebration() {
    fetch("/user/me", { credentials: "include" })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (d && d.ok && d.user) checkWins(); })
      .catch(function () {});
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootCelebration);
  } else {
    setTimeout(bootCelebration, 400);
  }
})();


// ─────────────────────────────────────────────
// Match Detail Drawer
// ─────────────────────────────────────────────
(function() {
  var drawer, body;

  function initTabs(container) {
    var tabs   = container.querySelectorAll('.md-tab');
    var panels = container.querySelectorAll('.md-panel');
    tabs.forEach(function(tab) {
      tab.addEventListener('click', function() {
        tabs.forEach(function(t) { t.classList.remove('active'); });
        panels.forEach(function(p) { p.classList.add('md-panel--hidden'); });
        tab.classList.add('active');
        var panel = container.querySelector('[data-panel="' + tab.dataset.tab + '"]');
        if (panel) panel.classList.remove('md-panel--hidden');
      });
    });
  }

  function open(league, matchId) {
    if (!drawer) return;
    drawer.classList.add('open');
    drawer.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
    body.innerHTML = '<div class="md-loading">Cargando datos del partido...</div>';
    fetch('/api/match/' + encodeURIComponent(league) + '/' + encodeURIComponent(matchId) + '/detail')
      .then(function(r) { return r.text(); })
      .then(function(html) {
        body.innerHTML = html;
        initTabs(body);
      })
      .catch(function() {
        body.innerHTML = '<p class="muted" style="padding:20px">No se pudieron cargar los datos.</p>';
      });
  }

  function close() {
    if (!drawer) return;
    drawer.classList.remove('open');
    drawer.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
  }

  function bootDrawer() {
    drawer = document.getElementById('match-drawer');
    if (!drawer) return;
    body = document.getElementById('match-drawer-body');
    var overlay  = drawer.querySelector('.match-drawer-overlay');
    var closeBtn = drawer.querySelector('.match-drawer-close');
    if (overlay)  overlay.addEventListener('click', close);
    if (closeBtn) closeBtn.addEventListener('click', close);
  }

  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') close();
  });

  document.addEventListener('click', function(e) {
    var btn = e.target.closest('.btn-match-detail');
    if (!btn) return;
    var league  = btn.dataset.league;
    var matchId = btn.dataset.matchId;
    if (league && matchId) open(league, matchId);
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootDrawer);
  } else {
    bootDrawer();
  }
})();

// ── addPickToTracker — global, cargado en todas las páginas via aftr-ui.js ──
window.addPickToTracker = function (btn) {
  var home    = btn.getAttribute('data-home') || '';
  var away    = btn.getAttribute('data-away') || '';
  var market  = btn.getAttribute('data-market') || '';
  var utcDate = btn.getAttribute('data-utcdate') || '';
  var newPick = { home: home, away: away, market: market, utcDate: utcDate };

  // Acumular picks para combinadas
  var existing = [];
  try {
    var raw = localStorage.getItem('aftr_tracker_prefill');
    if (raw) {
      var parsed = JSON.parse(raw);
      existing = Array.isArray(parsed) ? parsed : [parsed];
    }
  } catch (e) { existing = []; }
  existing.push(newPick);
  try { localStorage.setItem('aftr_tracker_prefill', JSON.stringify(existing)); } catch (e) {}

  if (window.location.pathname === '/tracker') {
    // Ya estamos en el tracker — _aftrCheckPrefill expuesto por aftr-tracker.js
    if (typeof window._aftrCheckPrefill === 'function') window._aftrCheckPrefill();
  } else {
    window.location.href = '/tracker';
  }
};

