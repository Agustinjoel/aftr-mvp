/**
 * Home league carousel — drum/wheel 3D.
 * Items are placed on a cylinder: each at rotateY(i*step) translateZ(radius).
 * Rotating the track container brings any item to the front.
 * Front item: full size, glowing. Others: fade + shrink into depth.
 */
(function () {
  "use strict";

  var AUTO_MS      = 3800;
  var SWIPE_THRESH = 36;
  var CLICK_MAX_MOVE = 14;
  var EASE         = "transform 0.58s cubic-bezier(0.22, 0.82, 0.24, 1)";
  var TOUCH_PAUSE_MS = 1800;

  /* Card fixed dimensions — must match CSS */
  var CARD_W  = 96;   /* px  (width of .league-carousel__card) */
  var CARD_H  = 124;  /* px  (height of .league-carousel__card) */
  var ARC_GAP = 18;   /* extra arc spacing between items    */

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

    /* ── Drum geometry ────────────────────────────────────────────── */
    var angleStep = 360 / n;
    /* Radius: enough that adjacent items don't overlap on the arc */
    var arcPerItem = CARD_W + ARC_GAP;
    var radius = Math.max(180, Math.round((n * arcPerItem) / (2 * Math.PI)));

    /* ── Give viewport explicit size so absolutely-placed items show ─ */
    var vpH = CARD_H + 44;  /* card height + breathing room */
    viewport.style.cssText +=
      ";position:relative;height:" + vpH + "px;" +
      "overflow:hidden;perspective:920px;perspective-origin:50% 42%;" +
      "touch-action:pan-y;";

    /* ── Convert each item into a drum position ───────────────────── */
    /* Track becomes a 0×0 pivot at the centre of the viewport */
    track.style.cssText +=
      ";position:absolute;top:50%;left:50%;" +
      "width:0;height:0;margin:0;padding:0;" +
      "transform-style:preserve-3d;will-change:transform;";

    items.forEach(function (el, i) {
      var angle = angleStep * i;
      el.style.cssText +=
        ";position:absolute;" +
        "top:0;left:0;" +
        /* Centre the card on the pivot before the drum rotation */
        "margin:" + (-CARD_H / 2) + "px 0 0 " + (-CARD_W / 2) + "px;" +
        "width:" + CARD_W + "px;" +
        "transform-style:preserve-3d;" +
        "will-change:transform,opacity;" +
        "transform:rotateY(" + angle + "deg) translateZ(" + radius + "px);";
    });

    /* Mark ready so CSS distance classes apply */
    root.classList.add("league-carousel--ready");

    /* ── State ────────────────────────────────────────────────────── */
    var dragging        = false;
    var dragStartX      = 0;
    var dragMoved       = 0;
    var suppressClick   = false;
    var activePointerId = null;
    var pointerCaptured = false;
    var hoverPaused     = false;
    var touchPauseTimer = null;
    var autoTimer       = null;
    var CAPTURE_AFTER   = 10;

    /* ── Circular distance (-n/2 … n/2) ──────────────────────────── */
    function circDist(i) {
      var d = ((i - idx) % n + n) % n;
      if (d > n / 2) d -= n;
      return Math.round(d);
    }

    /* ── Update CSS distance classes (glow, opacity via CSS) ─────── */
    function setDistanceClasses() {
      items.forEach(function (el, i) {
        var d = circDist(i);
        el.classList.toggle("is-active", d === 0);
        el.classList.toggle("active",    d === 0);
        el.setAttribute("aria-current", d === 0 ? "true" : "false");
        el.classList.remove(
          "league-carousel__item--d0",  "league-carousel__item--dn1", "league-carousel__item--dp1",
          "league-carousel__item--dn2", "league-carousel__item--dp2", "league-carousel__item--far"
        );
        if (d  ===  0) el.classList.add("league-carousel__item--d0");
        else if (d === -1) el.classList.add("league-carousel__item--dn1");
        else if (d ===  1) el.classList.add("league-carousel__item--dp1");
        else if (d === -2) el.classList.add("league-carousel__item--dn2");
        else if (d ===  2) el.classList.add("league-carousel__item--dp2");
        else               el.classList.add("league-carousel__item--far");
      });
    }

    /* ── Spin the drum ────────────────────────────────────────────── */
    /* extraAngle: live drag offset in degrees */
    function applyTrack(immediate, extraAngle) {
      var angle = -(angleStep * idx) + (extraAngle || 0);
      track.style.transition =
        (immediate || reducedMotion()) ? "none" : EASE;
      track.style.transform = "rotateY(" + angle + "deg)";
      if (!dragging) setDistanceClasses();
    }

    function goTo(i, immediate) {
      /* Wrap around — always go the shorter way */
      idx = ((i % n) + n) % n;
      dragging = false;
      applyTrack(immediate);
      setDistanceClasses();
      scheduleAuto();
    }

    /* ── Auto-advance ─────────────────────────────────────────────── */
    function clearAuto() {
      if (autoTimer) { clearInterval(autoTimer); autoTimer = null; }
    }
    function scheduleAuto() {
      clearAuto();
      if (reducedMotion() || n <= 1) return;
      autoTimer = window.setInterval(function () {
        if (hoverPaused || dragging) return;
        goTo(idx + 1, false);
      }, AUTO_MS);
    }

    /* ── Hover pause ──────────────────────────────────────────────── */
    root.addEventListener("mouseenter", function () { hoverPaused = true;  });
    root.addEventListener("mouseleave", function () { hoverPaused = false; });

    function touchPause() {
      hoverPaused = true;
      if (touchPauseTimer) clearTimeout(touchPauseTimer);
      touchPauseTimer = setTimeout(function () {
        hoverPaused = false; touchPauseTimer = null;
      }, TOUCH_PAUSE_MS);
    }
    viewport.addEventListener("touchstart", function () { touchPause(); }, { passive: true });

    /* ── Drag → rotation ──────────────────────────────────────────── */
    /* How many degrees one pixel of horizontal drag rotates the drum */
    var DEG_PER_PX = angleStep / arcPerItem;

    viewport.addEventListener("pointerdown", function (e) {
      if (e.pointerType === "mouse" && e.button !== 0) return;
      dragging        = true;
      dragStartX      = e.clientX;
      dragMoved       = 0;
      suppressClick   = false;
      activePointerId = e.pointerId;
      pointerCaptured = false;
      clearAuto();
      track.style.transition = "none";
    }, { passive: true });

    viewport.addEventListener("pointermove", function (e) {
      if (!dragging) return;
      var dx = e.clientX - dragStartX;
      dragMoved = Math.max(dragMoved, Math.abs(dx));
      if (!pointerCaptured && activePointerId === e.pointerId && dragMoved > CAPTURE_AFTER) {
        pointerCaptured = true;
        try { viewport.setPointerCapture(e.pointerId); } catch (_) {}
      }
      applyTrack(true, -dx * DEG_PER_PX);
    });

    function onPointerEnd(e) {
      if (!dragging) return;
      var dx = e.clientX - dragStartX;
      var dm = dragMoved;
      dragging  = false;
      dragMoved = 0;
      try {
        if (pointerCaptured && activePointerId === e.pointerId)
          viewport.releasePointerCapture(e.pointerId);
      } catch (_) {}
      pointerCaptured = false;
      activePointerId = null;

      if (dm > SWIPE_THRESH || dm > CLICK_MAX_MOVE) {
        suppressClick = true;
        /* Snap to the nearest item in the direction of drag */
        var steps = Math.round(dx / arcPerItem);
        goTo(idx - steps, false);
        setTimeout(function () { suppressClick = false; }, 120);
      } else {
        goTo(idx, false);
      }
    }

    viewport.addEventListener("pointerup",     onPointerEnd);
    viewport.addEventListener("pointercancel", function (e) {
      if (!dragging) return;
      dragging = false; dragMoved = 0;
      try { if (pointerCaptured) viewport.releasePointerCapture(e.pointerId); } catch (_) {}
      pointerCaptured = false; activePointerId = null;
      goTo(idx, false);
    });

    /* Suppress navigation click after a drag gesture */
    root.addEventListener("click", function (e) {
      var a = e.target.closest("a.league-carousel__item");
      if (!a || !root.contains(a)) return;
      if (suppressClick) { e.preventDefault(); e.stopImmediatePropagation(); }
    }, true);

    window.addEventListener("resize", function () { applyTrack(true); });

    /* ── Boot ─────────────────────────────────────────────────────── */
    setDistanceClasses();
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        applyTrack(true);
        scheduleAuto();
      });
    });
  }

  document.querySelectorAll(".league-carousel.league-carousel--3d")
    .forEach(function (root) { init(root); });
})();
