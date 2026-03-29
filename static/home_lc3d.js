/**
 * AFTR home carousel — true 3D coverflow.
 * Shared perspective on stage (no scroll-snap). JS positions items via transform.
 * Supports: mouse drag, touch swipe, arrow buttons, keyboard, auto-advance.
 */
(function () {
  "use strict";

  var GAP       = 116;
  var TILT      = 24;
  var TILT_MAX  = 62;
  var SCALE0    = 1.10;
  var SCALE_D   = 0.13;
  var SCALE_MIN = 0.28;
  var OPAC_D    = 0.20;
  var OPAC_MIN  = 0.06;
  var AUTO_MS   = 3400;
  var TRANS_DUR = "0.50s";
  var DRAG_THRESH = 8;

  function prefersReducedMotion() {
    try { return window.matchMedia("(prefers-reduced-motion:reduce)").matches; }
    catch (e) { return false; }
  }

  function init(root) {
    var stage = root.querySelector(".lc3d__stage");
    var items = [].slice.call(root.querySelectorAll(".lc3d__item"));
    var btnP  = root.querySelector(".lc3d__prev");
    var btnN  = root.querySelector(".lc3d__next");
    if (!stage || !items.length) return;

    var n   = items.length;
    var idx = 0;
    var activeCode = (root.getAttribute("data-active-code") || "").trim();
    items.forEach(function (el, i) {
      if ((el.getAttribute("data-code") || "") === activeCode) idx = i;
    });

    /* ── Compute transform for item at distance d + live drag ── */
    function calcTransform(d, dragDx) {
      var dx  = dragDx || 0;
      var x   = d * GAP + dx;
      var ry  = Math.max(-TILT_MAX, Math.min(TILT_MAX, d * TILT - dx * 0.07));
      var ad  = Math.abs(d - dx / GAP);
      var s   = Math.max(SCALE_MIN, SCALE0 - ad * SCALE_D);
      var o   = Math.max(OPAC_MIN,  1.0   - ad * OPAC_D);
      var zi  = 30 - Math.round(ad) * 3;
      var bl  = ad > 1 ? ((ad - 1) * 0.9).toFixed(1) : 0;
      return { x: x, ry: ry, s: s, o: o, zi: zi, bl: bl };
    }

    function update(animated, dragDx) {
      var dur = (animated && !prefersReducedMotion()) ? TRANS_DUR : "0s";
      var dragging = dragDx != null && dragDx !== 0;
      items.forEach(function (el, i) {
        var d = i - idx;
        var t = calcTransform(d, dragging ? dragDx : 0);
        el.style.transitionDuration = dur;
        el.style.transform = "translateX(" + t.x + "px) rotateY(" + t.ry + "deg) scale(" + t.s + ")";
        el.style.opacity   = t.o;
        el.style.zIndex    = t.zi;
        el.style.filter    = t.bl > 0 ? "blur(" + t.bl + "px)" : "";
        el.classList.toggle("lc3d__item--active", d === 0 && !dragging);
        el.setAttribute("aria-current", (d === 0 && !dragging) ? "true" : "false");
      });
    }

    /* ── Navigation ─────────────────────────────────────────── */
    var suppressClick = false;

    function goTo(i, animated) {
      idx = ((i % n) + n) % n;
      update(animated !== false);
      scheduleAuto();
    }

    items.forEach(function (el, i) {
      el.addEventListener("click", function (e) {
        if (suppressClick) { e.preventDefault(); return; }
        if (i !== idx) { e.preventDefault(); goTo(i, true); }
      });
    });

    if (btnP) btnP.addEventListener("click", function () { goTo(idx - 1, true); });
    if (btnN) btnN.addEventListener("click", function () { goTo(idx + 1, true); });

    root.addEventListener("keydown", function (e) {
      if (e.key === "ArrowLeft")  { e.preventDefault(); goTo(idx - 1, true); }
      if (e.key === "ArrowRight") { e.preventDefault(); goTo(idx + 1, true); }
    });

    /* ── Auto-advance ───────────────────────────────────────── */
    var autoTimer = null, paused = false;
    function clearAuto() { if (autoTimer) { clearInterval(autoTimer); autoTimer = null; } }
    function scheduleAuto() {
      clearAuto();
      if (n <= 1 || prefersReducedMotion()) return;
      autoTimer = setInterval(function () { if (!paused) goTo(idx + 1, true); }, AUTO_MS);
    }
    root.addEventListener("mouseenter", function () { paused = true; });
    root.addEventListener("mouseleave", function () { paused = false; });

    /* ── Mouse drag (pointer events) ────────────────────────── */
    var mouseDown = false, mouseX0 = 0, mouseMoved = 0;

    stage.style.cursor = "grab";

    stage.addEventListener("pointerdown", function (e) {
      if (e.pointerType !== "mouse") return;
      mouseDown = true;
      mouseX0   = e.clientX;
      mouseMoved = 0;
      suppressClick = false;
      stage.setPointerCapture(e.pointerId);
      stage.style.cursor = "grabbing";
      clearAuto();
      paused = true;
    });

    stage.addEventListener("pointermove", function (e) {
      if (!mouseDown || e.pointerType !== "mouse") return;
      var dx = e.clientX - mouseX0;
      mouseMoved = Math.max(mouseMoved, Math.abs(dx));
      update(false, dx);
    });

    function onMouseEnd(e) {
      if (!mouseDown || e.pointerType !== "mouse") return;
      mouseDown = false;
      stage.style.cursor = "grab";
      try { stage.releasePointerCapture(e.pointerId); } catch (_) {}
      var dx = e.clientX - mouseX0;
      if (mouseMoved > DRAG_THRESH) {
        suppressClick = true;
        goTo(idx + (-Math.round(dx / GAP)), true);
        setTimeout(function () { suppressClick = false; }, 200);
      } else {
        update(true);
        scheduleAuto();
      }
      paused = false;
    }

    stage.addEventListener("pointerup",     onMouseEnd);
    stage.addEventListener("pointercancel", onMouseEnd);

    /* ── Touch swipe ────────────────────────────────────────── */
    var tx0 = 0;
    stage.addEventListener("touchstart", function (e) {
      tx0 = e.touches[0].clientX; paused = true; clearAuto();
    }, { passive: true });
    stage.addEventListener("touchend", function (e) {
      var dx = e.changedTouches[0].clientX - tx0;
      if (Math.abs(dx) > 36) goTo(idx + (dx < 0 ? 1 : -1), true);
      setTimeout(function () { paused = false; scheduleAuto(); }, 1400);
    }, { passive: true });

    /* ── Boot ───────────────────────────────────────────────── */
    root.classList.add("lc3d--ready");
    update(false);
    scheduleAuto();
  }

  document.querySelectorAll(".lc3d").forEach(function (el) { init(el); });
})();
