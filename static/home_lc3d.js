/**
 * AFTR home carousel — true 3D coverflow.
 * Click en cualquier item → navega a /?league=CODE.
 * Drag → pan visual, sin navegar.
 */
(function () {
  "use strict";

  var GAP         = 116;
  var TILT        = 24;
  var TILT_MAX    = 62;
  var SCALE0      = 1.10;
  var SCALE_D     = 0.13;
  var SCALE_MIN   = 0.28;
  var OPAC_D      = 0.20;
  var OPAC_MIN    = 0.06;
  var AUTO_MS     = 3400;
  var TRANS_DUR   = "0.50s";
  var DRAG_THRESH = 8;   /* px mínimo para considerar drag vs click */

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

    /* ── Transform calc ─────────────────────────────────────── */
    function calcT(d, dragDx) {
      var dx = dragDx || 0;
      var ad = Math.abs(d - dx / GAP);
      return {
        x  : d * GAP + dx,
        ry : Math.max(-TILT_MAX, Math.min(TILT_MAX, d * TILT - dx * 0.07)),
        s  : Math.max(SCALE_MIN, SCALE0 - ad * SCALE_D),
        o  : Math.max(OPAC_MIN,  1.0   - ad * OPAC_D),
        zi : 30 - Math.round(ad) * 3,
        bl : ad > 1 ? ((ad - 1) * 0.9).toFixed(1) : 0
      };
    }

    function update(animated, dragDx) {
      var dur      = (animated && !prefersReducedMotion()) ? TRANS_DUR : "0s";
      var isDrag   = dragDx != null && dragDx !== 0;
      items.forEach(function (el, i) {
        var t = calcT(i - idx, isDrag ? dragDx : 0);
        el.style.transitionDuration = dur;
        el.style.transform  = "translateX(" + t.x + "px) rotateY(" + t.ry + "deg) scale(" + t.s + ")";
        el.style.opacity    = t.o;
        el.style.zIndex     = t.zi;
        el.style.filter     = t.bl > 0 ? "blur(" + t.bl + "px)" : "";
        var active = (i - idx) === 0 && !isDrag;
        el.classList.toggle("lc3d__item--active", active);
        el.setAttribute("aria-current", active ? "true" : "false");
      });
    }

    /* ── goTo: centra visualmente un item ───────────────────── */
    function goTo(i, animated) {
      idx = ((i % n) + n) % n;
      update(animated !== false);
      scheduleAuto();
    }

    /* ── navigate: va a la URL del item ────────────────────── */
    function navigateTo(el) {
      var href = el.getAttribute("href");
      if (href) window.location.href = href;
    }

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

    /* ── Arrow buttons ──────────────────────────────────────── */
    if (btnP) btnP.addEventListener("click", function () { goTo(idx - 1, true); });
    if (btnN) btnN.addEventListener("click", function () { goTo(idx + 1, true); });

    /* ── Keyboard ───────────────────────────────────────────── */
    root.addEventListener("keydown", function (e) {
      if (e.key === "ArrowLeft")  { e.preventDefault(); goTo(idx - 1, true); }
      if (e.key === "ArrowRight") { e.preventDefault(); goTo(idx + 1, true); }
    });

    /* ── Mouse drag — SIN setPointerCapture para no bloquear clicks ── */
    var mouseDown = false, mouseX0 = 0, totalMoved = 0;

    stage.style.cursor = "grab";

    stage.addEventListener("pointerdown", function (e) {
      if (e.button !== 0) return;                     /* solo botón izquierdo */
      if (e.pointerType === "touch") return;          /* touch lo maneja touchstart */
      mouseDown  = true;
      mouseX0    = e.clientX;
      totalMoved = 0;
      stage.style.cursor = "grabbing";
      clearAuto();
      paused = true;
    });

    stage.addEventListener("pointermove", function (e) {
      if (!mouseDown) return;
      var dx = e.clientX - mouseX0;
      totalMoved = Math.max(totalMoved, Math.abs(dx));
      if (totalMoved > 3) update(false, dx);
    });

    function endDrag(e) {
      if (!mouseDown) return;
      mouseDown = false;
      stage.style.cursor = "grab";
      paused = false;
      var dx = e.clientX - mouseX0;
      if (totalMoved > DRAG_THRESH) {
        /* Fue un drag real: mover al item más cercano */
        var steps = -Math.round(dx / GAP);
        goTo(idx + steps, true);
      } else {
        /* Fue un click: navegar al item bajo el cursor */
        var target = document.elementFromPoint(e.clientX, e.clientY);
        var item   = target && target.closest(".lc3d__item");
        if (item) {
          var itemIdx = items.indexOf(item);
          if (itemIdx !== -1) {
            /* Cualquier item: navegar directo */
            navigateTo(item);
          }
        } else {
          update(true);
          scheduleAuto();
        }
      }
    }

    stage.addEventListener("pointerup",     endDrag);
    stage.addEventListener("pointerleave",  function (e) {
      if (!mouseDown) return;
      mouseDown = false;
      stage.style.cursor = "grab";
      paused = false;
      var dx = e.clientX - mouseX0;
      if (totalMoved > DRAG_THRESH) {
        goTo(idx + (-Math.round(dx / GAP)), true);
      } else {
        update(true);
        scheduleAuto();
      }
    });
    stage.addEventListener("pointercancel", function () {
      mouseDown = false;
      stage.style.cursor = "grab";
      paused = false;
      update(true);
      scheduleAuto();
    });

    /* ── Click en items (para cuando NO se usa drag) ────────── */
    items.forEach(function (el, i) {
      el.addEventListener("click", function (e) {
        /* Si fue precedido por un drag real, cancelar navegación */
        if (totalMoved > DRAG_THRESH) { e.preventDefault(); return; }
        /* Todos los items: dejar que el href navegue naturalmente */
      });
    });

    /* ── Touch swipe ────────────────────────────────────────── */
    var tx0 = 0, tMoved = 0;
    stage.addEventListener("touchstart", function (e) {
      tx0 = e.touches[0].clientX;
      tMoved = 0;
      paused = true;
      clearAuto();
    }, { passive: true });
    stage.addEventListener("touchmove", function (e) {
      tMoved = Math.max(tMoved, Math.abs(e.touches[0].clientX - tx0));
      if (tMoved > 5) update(false, e.touches[0].clientX - tx0);
    }, { passive: true });
    stage.addEventListener("touchend", function (e) {
      var dx = e.changedTouches[0].clientX - tx0;
      if (tMoved > DRAG_THRESH) {
        goTo(idx + (dx < 0 ? 1 : -1), true);
      } else {
        /* Tap: navegar al item tocado */
        var t2    = e.changedTouches[0];
        var el2   = document.elementFromPoint(t2.clientX, t2.clientY);
        var item2 = el2 && el2.closest(".lc3d__item");
        if (item2) {
          var ti = items.indexOf(item2);
          if (ti !== -1) navigateTo(item2);
        } else {
          update(true);
        }
      }
      setTimeout(function () { paused = false; scheduleAuto(); }, 1400);
    }, { passive: true });

    /* ── Boot ───────────────────────────────────────────────── */
    root.classList.add("lc3d--ready");
    update(false);
    scheduleAuto();
  }

  document.querySelectorAll(".lc3d").forEach(function (el) { init(el); });
})();
