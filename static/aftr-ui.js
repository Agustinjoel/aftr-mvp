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
    var cards = document.querySelectorAll(".flip-card");
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
// Match Detail Drawer
// ─────────────────────────────────────────────
(function() {
  var drawer = document.getElementById('match-drawer');
  if (!drawer) return;

  var body    = document.getElementById('match-drawer-body');
  var overlay = drawer.querySelector('.match-drawer-overlay');
  var closeBtn = drawer.querySelector('.match-drawer-close');

  function open(league, matchId) {
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
    drawer.classList.remove('open');
    drawer.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
  }

  if (overlay) overlay.addEventListener('click', close);
  if (closeBtn) closeBtn.addEventListener('click', close);
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

  function initTabs(container) {
    var tabs   = container.querySelectorAll('.md-tab');
    var panels = container.querySelectorAll('.md-panel');
    tabs.forEach(function(tab) {
      tab.addEventListener('click', function() {
        tabs.forEach(function(t) { t.classList.remove('active'); });
        panels.forEach(function(p) { p.classList.add('md-panel--hidden'); });
        tab.classList.add('active');
        var panel = container.querySelector('[data-panel= + tab.dataset.tab + ]');
        if (panel) panel.classList.remove('md-panel--hidden');
      });
    });
  }
})();
