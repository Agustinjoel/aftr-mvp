/**
 * Home league strip: scroll active .league-item into view (centered).
 */
(function () {
  "use strict";

  function centerActive() {
    var root = document.getElementById("homeLeagueCarousel");
    if (!root) return;
    var track = root.querySelector(".league-track");
    var el = root.querySelector(".league-item.is-active");
    if (!track || !el) return;
    function run(behavior) {
      try {
        el.scrollIntoView({ inline: "center", block: "nearest", behavior: behavior || "smooth" });
      } catch (e) {
        var left = el.offsetLeft - track.clientWidth / 2 + el.offsetWidth / 2;
        track.scrollLeft = Math.max(0, left);
      }
    }
    run("auto");
    window.requestAnimationFrame(function () {
      setTimeout(function () {
        run("smooth");
      }, 50);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", centerActive);
  } else {
    centerActive();
  }
  window.addEventListener("load", function () {
    centerActive();
  });
})();
