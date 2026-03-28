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
     Boot on DOMContentLoaded
  ───────────────────────────────────────────────────────────── */
  function boot() {
    animateGauges();
    staggerCards();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
