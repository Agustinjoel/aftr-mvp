/**
 * Home league carousel — CSS scroll-snap based.
 * Native scroll handles all touch/drag physics.
 * JS adds: active classes, auto-advance, click-to-center.
 */
(function () {
  "use strict";

  var AUTO_MS       = 3800;
  var TOUCH_PAUSE_MS = 2200;
  var SETTLE_MS     = 140;   /* ms after scroll stops before we lock idx */
  var CARD_W        = 96;    /* px — must match .league-carousel__card min-width */

  function reducedMotion() {
    try { return window.matchMedia("(prefers-reduced-motion: reduce)").matches; }
    catch (e) { return false; }
  }

  function init(root) {
    var viewport = root.querySelector("[data-carousel-viewport]");
    var track    = root.querySelector("[data-track]");
    var items    = [].slice.call(root.querySelectorAll(".league-carousel__item"));
    if (!viewport || !track || !items.length) return;

    var n = items.length;

    /* ── Active index ─────────────────────────────────────────────── */
    var idx = 0;
    var activeCode = (root.getAttribute("data-active-code") || "").trim();
    items.forEach(function (el, i) {
      if ((el.getAttribute("data-code") || "") === activeCode) idx = i;
    });

    /* ── Padding so first/last items can be centred ───────────────── */
    function applyPadding() {
      var pad = Math.max(8, Math.floor(viewport.offsetWidth / 2 - CARD_W / 2));
      track.style.paddingLeft  = pad + "px";
      track.style.paddingRight = pad + "px";
    }
    applyPadding();

    /* ── Find which item is closest to viewport centre ────────────── */
    function centeredIdx() {
      var vpMid = viewport.scrollLeft + viewport.offsetWidth / 2;
      var best = 0, bestDist = Infinity;
      items.forEach(function (el, i) {
        var d = Math.abs(el.offsetLeft + el.offsetWidth / 2 - vpMid);
        if (d < bestDist) { bestDist = d; best = i; }
      });
      return best;
    }

    /* ── Distance CSS classes ─────────────────────────────────────── */
    var DC = [
      "league-carousel__item--d0",
      "league-carousel__item--dn1", "league-carousel__item--dp1",
      "league-carousel__item--dn2", "league-carousel__item--dp2",
      "league-carousel__item--far"
    ];
    function updateClasses(ci) {
      items.forEach(function (el, i) {
        var d = i - ci;
        el.classList.remove.apply(el.classList, DC);
        el.classList.toggle("is-active", d === 0);
        el.classList.toggle("active",    d === 0);
        el.setAttribute("aria-current", d === 0 ? "true" : "false");
        if      (d ===  0) el.classList.add("league-carousel__item--d0");
        else if (d === -1) el.classList.add("league-carousel__item--dn1");
        else if (d ===  1) el.classList.add("league-carousel__item--dp1");
        else if (d === -2) el.classList.add("league-carousel__item--dn2");
        else if (d ===  2) el.classList.add("league-carousel__item--dp2");
        else               el.classList.add("league-carousel__item--far");
      });
    }

    /* ── Scroll item into centre ──────────────────────────────────── */
    function scrollTo(i, smooth) {
      var el = items[i];
      if (!el) return;
      var target = el.offsetLeft - (viewport.offsetWidth - el.offsetWidth) / 2;
      try {
        viewport.scrollTo({ left: Math.max(0, target), behavior: smooth ? "smooth" : "auto" });
      } catch (_) {
        viewport.scrollLeft = Math.max(0, target);
      }
    }

    /* ── Go to index ──────────────────────────────────────────────── */
    function goTo(i, smooth) {
      idx = ((i % n) + n) % n;
      scrollTo(idx, smooth);
      updateClasses(idx);
      scheduleAuto();
    }

    /* ── Scroll listener — live class updates while scrolling ─────── */
    var settleTimer = null;
    viewport.addEventListener("scroll", function () {
      var ci = centeredIdx();
      updateClasses(ci);
      if (settleTimer) clearTimeout(settleTimer);
      settleTimer = setTimeout(function () {
        idx = centeredIdx();
        settleTimer = null;
      }, SETTLE_MS);
    }, { passive: true });

    /* ── Auto-advance ─────────────────────────────────────────────── */
    var autoTimer   = null;
    var paused      = false;

    function clearAuto() {
      if (autoTimer) { clearInterval(autoTimer); autoTimer = null; }
    }
    function scheduleAuto() {
      clearAuto();
      if (reducedMotion() || n <= 1) return;
      autoTimer = window.setInterval(function () {
        if (paused) return;
        goTo(idx + 1, true);
      }, AUTO_MS);
    }

    /* Pause on hover (desktop) */
    root.addEventListener("mouseenter", function () { paused = true; });
    root.addEventListener("mouseleave", function () { paused = false; });

    /* Pause on touch, resume after idle */
    var touchTimer = null;
    viewport.addEventListener("touchstart", function () {
      paused = true;
      clearAuto();
      if (touchTimer) clearTimeout(touchTimer);
    }, { passive: true });
    viewport.addEventListener("touchend", function () {
      if (touchTimer) clearTimeout(touchTimer);
      touchTimer = setTimeout(function () {
        paused = false;
        idx = centeredIdx();
        scheduleAuto();
      }, TOUCH_PAUSE_MS);
    }, { passive: true });

    /* ── Click: non-centre item → scroll to it, don't navigate ───── */
    root.addEventListener("click", function (e) {
      var a = e.target.closest("a.league-carousel__item");
      if (!a || !root.contains(a)) return;
      var ci = centeredIdx();
      var ti = items.indexOf(a);
      if (ti === -1 || ti === ci) return;   /* already centred → let href fire */
      e.preventDefault();
      e.stopImmediatePropagation();
      goTo(ti, true);
    }, true);

    window.addEventListener("resize", function () {
      applyPadding();
      scrollTo(idx, false);
    });

    /* ── Boot ─────────────────────────────────────────────────────── */
    root.classList.add("league-carousel--ready");
    updateClasses(idx);
    /* Use rAF so layout is complete before we scroll */
    requestAnimationFrame(function () {
      scrollTo(idx, false);
      scheduleAuto();
    });
  }

  document.querySelectorAll(".league-carousel.league-carousel--3d")
    .forEach(function (root) { init(root); });
})();
