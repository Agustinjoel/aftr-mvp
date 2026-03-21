/**
 * AFTR league carousel: coverflow-style selection, URL ?league= sync on load.
 * Adds .league-carousel--ready when initialized; without it, CSS shows horizontal scroll fallback.
 */
(function () {
  "use strict";

  var SWIPE_MIN = 44;
  var TRANSITION_MS = 450;

  function clamp(i, n) {
    return Math.max(0, Math.min(n - 1, i));
  }

  function setDistanceClasses(btn, d) {
    btn.classList.remove(
      "league-carousel__item--d0",
      "league-carousel__item--dn1",
      "league-carousel__item--dn2",
      "league-carousel__item--dp1",
      "league-carousel__item--dp2",
      "league-carousel__item--far"
    );
    if (d === 0) btn.classList.add("league-carousel__item--d0");
    else if (d === -1) btn.classList.add("league-carousel__item--dn1");
    else if (d === 1) btn.classList.add("league-carousel__item--dp1");
    else if (d <= -2) btn.classList.add("league-carousel__item--dn2");
    else if (d >= 2) btn.classList.add("league-carousel__item--dp2");
    if (Math.abs(d) > 2) btn.classList.add("league-carousel__item--far");
  }

  function initCarousel(wrap) {
    var el = wrap.querySelector(".league-carousel");
    if (!el) return;
    var track = el.querySelector("[data-track]");
    var items = [].slice.call(el.querySelectorAll(".league-carousel__item"));
    if (!items.length || !track) return;

    var codes = items.map(function (b) {
      return b.getAttribute("data-code") || "";
    });
    var activeCode = el.getAttribute("data-active-code") || "";
    var idx = codes.indexOf(activeCode);
    if (idx < 0) idx = 0;
    idx = clamp(idx, items.length);

    var viewport = el.querySelector(".league-carousel__viewport");
    var prevBtn = el.querySelector(".league-carousel__arrow--prev");
    var nextBtn = el.querySelector(".league-carousel__arrow--next");

    function apply() {
      items.forEach(function (btn, i) {
        var d = i - idx;
        setDistanceClasses(btn, d);
        btn.setAttribute("aria-current", d === 0 ? "true" : "false");
      });
      if (prevBtn) prevBtn.disabled = idx <= 0;
      if (nextBtn) nextBtn.disabled = idx >= items.length - 1;
    }

    function go(delta) {
      idx = clamp(idx + delta, items.length);
      apply();
    }

    function goToIndex(i) {
      idx = clamp(i, items.length);
      apply();
    }

    if (prevBtn) prevBtn.addEventListener("click", function () {
      go(-1);
    });
    if (nextBtn) nextBtn.addEventListener("click", function () {
      go(1);
    });

    items.forEach(function (btn, i) {
      btn.addEventListener("click", function () {
        if (i === idx) {
          var code = btn.getAttribute("data-code") || "";
          if (code) window.location.href = "/?league=" + encodeURIComponent(code);
        } else {
          goToIndex(i);
        }
      });
    });

    var tx = 0;
    var touching = false;
    if (viewport) {
      viewport.addEventListener(
        "touchstart",
        function (e) {
          touching = true;
          tx = e.touches[0].clientX;
        },
        { passive: true }
      );
      viewport.addEventListener(
        "touchend",
        function (e) {
          if (!touching) return;
          touching = false;
          var cx = e.changedTouches[0] ? e.changedTouches[0].clientX : tx;
          var dx = cx - tx;
          if (Math.abs(dx) > SWIPE_MIN) go(dx < 0 ? 1 : -1);
        },
        { passive: true }
      );
    }

    el.addEventListener("keydown", function (e) {
      if (e.key === "ArrowLeft") {
        e.preventDefault();
        go(-1);
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        go(1);
      } else if (e.key === "Enter" || e.key === " ") {
        var cur = items[idx];
        if (cur && e.target === el) {
          e.preventDefault();
          var c = cur.getAttribute("data-code");
          if (c) window.location.href = "/?league=" + encodeURIComponent(c);
        }
      }
    });

    el.tabIndex = 0;
    apply();
    el.classList.add("league-carousel--ready");
    el.style.setProperty("--lc-transition-ms", String(TRANSITION_MS));
  }

  function boot() {
    document.querySelectorAll('[data-component="league-carousel"]').forEach(initCarousel);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
