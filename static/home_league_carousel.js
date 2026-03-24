/**
 * Home league carousel: 3D-style, transform-based centering, auto-advance, drag/swipe.
 * No native horizontal scroll (no scrollbar). Active class follows centered item.
 */
(function () {
  "use strict";

  var AUTO_MS = 3600;
  var SWIPE_THRESH = 42;
  var CLICK_MAX_MOVE = 16;
  var TRACK_EASE = "transform 0.45s cubic-bezier(0.22, 0.82, 0.24, 1)";
  var TOUCH_PAUSE_MS = 1800;

  function reducedMotion() {
    try {
      return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    } catch (e) {
      return false;
    }
  }

  function init(root) {
    var viewport = root.querySelector("[data-carousel-viewport]");
    var track = root.querySelector("[data-track]");
    var items = [].slice.call(root.querySelectorAll(".league-item"));
    if (!viewport || !track || !items.length) return;

    var n = items.length;
    var idx = 0;
    var activeCode = (root.getAttribute("data-active-code") || "").trim();
    items.forEach(function (el, i) {
      if ((el.getAttribute("data-code") || "") === activeCode) idx = i;
    });

    var dragging = false;
    var dragOffset = 0;
    var dragStartClientX = 0;
    var dragMoved = 0;
    var frozenIdx = 0;
    var hoverPaused = false;
    var touchPauseTimer = null;
    var autoTimer = null;
    var suppressClick = false;
    var activePointerId = null;
    var pointerCaptured = false;
    var CAPTURE_AFTER_PX = 10;

    function setDistanceClasses() {
      items.forEach(function (el, i) {
        var d = i - idx;
        el.classList.toggle("is-active", d === 0);
        el.classList.toggle("active", d === 0);
        el.setAttribute("aria-current", d === 0 ? "true" : "false");
        el.classList.remove(
          "league-item--dn2",
          "league-item--dn1",
          "league-item--d0",
          "league-item--dp1",
          "league-item--dp2",
          "league-item--far"
        );
        if (d === 0) el.classList.add("league-item--d0");
        else if (d === -1) el.classList.add("league-item--dn1");
        else if (d === 1) el.classList.add("league-item--dp1");
        else if (d <= -2) el.classList.add("league-item--dn2");
        else if (d >= 2) el.classList.add("league-item--dp2");
        if (Math.abs(d) > 2) el.classList.add("league-item--far");
      });
    }

    function translateForIndex(i, extra) {
      extra = extra || 0;
      var el = items[i];
      if (!el) return 0;
      var vw = viewport.clientWidth;
      var logicalCenter = el.offsetLeft + el.offsetWidth / 2;
      return vw / 2 - logicalCenter + extra;
    }

    function applyTrack(immediate, extraOffset) {
      extraOffset = extraOffset || 0;
      var useIdx = dragging ? frozenIdx : idx;
      var tx = translateForIndex(useIdx, extraOffset);
      track.style.transition =
        immediate || reducedMotion() ? "none" : TRACK_EASE;
      track.style.transform = "translate3d(" + tx + "px, 0, 0)";
      if (!dragging) setDistanceClasses();
    }

    function goTo(i, immediate) {
      idx = Math.max(0, Math.min(n - 1, i));
      dragOffset = 0;
      dragging = false;
      applyTrack(immediate, 0);
      setDistanceClasses();
      scheduleAuto();
    }

    function nearestIndexToCenter() {
      var rectV = viewport.getBoundingClientRect();
      var cx = rectV.left + rectV.width / 2;
      var best = idx;
      var bestD = Infinity;
      items.forEach(function (el, i) {
        var r = el.getBoundingClientRect();
        var c = r.left + r.width / 2;
        var d = Math.abs(c - cx);
        if (d < bestD) {
          bestD = d;
          best = i;
        }
      });
      return best;
    }

    function clearAuto() {
      if (autoTimer) {
        clearInterval(autoTimer);
        autoTimer = null;
      }
    }

    function scheduleAuto() {
      clearAuto();
      if (reducedMotion() || n <= 1) return;
      autoTimer = window.setInterval(function () {
        if (hoverPaused || dragging) return;
        goTo((idx + 1) % n, false);
      }, AUTO_MS);
    }

    root.addEventListener("mouseenter", function () {
      hoverPaused = true;
    });
    root.addEventListener("mouseleave", function () {
      hoverPaused = false;
    });

    function touchPause() {
      hoverPaused = true;
      if (touchPauseTimer) clearTimeout(touchPauseTimer);
      touchPauseTimer = setTimeout(function () {
        hoverPaused = false;
        touchPauseTimer = null;
      }, TOUCH_PAUSE_MS);
    }

    viewport.addEventListener(
      "touchstart",
      function () {
        touchPause();
      },
      { passive: true }
    );

    viewport.addEventListener(
      "pointerdown",
      function (e) {
        if (e.pointerType === "mouse" && e.button !== 0) return;
        dragging = true;
        frozenIdx = idx;
        dragStartClientX = e.clientX;
        dragOffset = 0;
        dragMoved = 0;
        suppressClick = false;
        activePointerId = e.pointerId;
        pointerCaptured = false;
        clearAuto();
        track.style.transition = "none";
        /* Do not setPointerCapture on down: it retargets the click away from <a>, breaking navigation. */
      },
      { passive: true }
    );

    viewport.addEventListener("pointermove", function (e) {
      if (!dragging) return;
      dragOffset = e.clientX - dragStartClientX;
      dragMoved = Math.max(dragMoved, Math.abs(dragOffset));
      if (
        !pointerCaptured &&
        activePointerId === e.pointerId &&
        dragMoved > CAPTURE_AFTER_PX
      ) {
        pointerCaptured = true;
        try {
          viewport.setPointerCapture(e.pointerId);
        } catch (err) {}
      }
      applyTrack(true, dragOffset);
    });

    viewport.addEventListener("pointerup", function (e) {
      if (!dragging) return;
      var dm = dragMoved;
      dragging = false;
      dragMoved = 0;
      try {
        if (pointerCaptured && activePointerId === e.pointerId) {
          viewport.releasePointerCapture(e.pointerId);
        }
      } catch (err) {}
      pointerCaptured = false;
      activePointerId = null;

      if (dm > SWIPE_THRESH || dm > CLICK_MAX_MOVE) {
        suppressClick = true;
        idx = nearestIndexToCenter();
        setTimeout(function () {
          suppressClick = false;
        }, 100);
      }

      dragOffset = 0;
      applyTrack(false, 0);
      setDistanceClasses();
      scheduleAuto();
    });

    viewport.addEventListener("pointercancel", function (e) {
      if (!dragging) return;
      dragging = false;
      dragMoved = 0;
      dragOffset = 0;
      try {
        if (pointerCaptured && activePointerId === e.pointerId) {
          viewport.releasePointerCapture(e.pointerId);
        }
      } catch (err) {}
      pointerCaptured = false;
      activePointerId = null;
      applyTrack(false, 0);
      setDistanceClasses();
      scheduleAuto();
    });

    /* Let <a class="league-item" href="/?league=..."> navigate normally; only block after a drag gesture. */
    root.addEventListener(
      "click",
      function (e) {
        var a = e.target.closest("a.league-item, a.league-card");
        if (!a || !root.contains(a)) return;
        if (suppressClick) {
          e.preventDefault();
          e.stopImmediatePropagation();
        }
      },
      true
    );

    window.addEventListener("resize", function () {
      applyTrack(true, 0);
    });

    function boot() {
      setDistanceClasses();
      requestAnimationFrame(function () {
        requestAnimationFrame(function () {
          goTo(idx, true);
          requestAnimationFrame(function () {
            goTo(idx, false);
          });
        });
      });
    }

    boot();
  }

  var roots = document.querySelectorAll(".league-carousel.league-carousel--3d");
  for (var r = 0; r < roots.length; r++) {
    init(roots[r]);
  }
})();
