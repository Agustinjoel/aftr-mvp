/**
 * aftr-share.js — Share card generator (Canvas-based, no external deps)
 * Usage: window.AFTRShare.open({ home, away, market, aftr_score, tier, score_home, score_away })
 */
(function () {
  "use strict";

  /* ── Helpers ──────────────────────────────────────────────── */
  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + w - r, y);
    ctx.arcTo(x + w, y,     x + w, y + r,     r);
    ctx.lineTo(x + w, y + h - r);
    ctx.arcTo(x + w, y + h, x + w - r, y + h, r);
    ctx.lineTo(x + r, y + h);
    ctx.arcTo(x,     y + h, x,     y + h - r, r);
    ctx.lineTo(x,     y + r);
    ctx.arcTo(x,     y,     x + r, y,         r);
    ctx.closePath();
  }

  function truncate(ctx, text, maxW) {
    if (!text) return "";
    if (ctx.measureText(text).width <= maxW) return text;
    while (text.length > 0 && ctx.measureText(text + "…").width > maxW) {
      text = text.slice(0, -1);
    }
    return text + "…";
  }

  /* ── Canvas drawing ───────────────────────────────────────── */
  function drawShareCard(opts) {
    var W = 1080, H = 540;
    var canvas = document.createElement("canvas");
    canvas.width = W;
    canvas.height = H;
    var ctx = canvas.getContext("2d");

    // Background
    var bg = ctx.createLinearGradient(0, 0, W, H);
    bg.addColorStop(0, "#070a10");
    bg.addColorStop(0.55, "#0b1525");
    bg.addColorStop(1, "#08101e");
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, W, H);

    // Ambient orbs
    function radial(cx, cy, r, col) {
      var g = ctx.createRadialGradient(cx, cy, 0, cx, cy, r);
      g.addColorStop(0, col);
      g.addColorStop(1, "transparent");
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, W, H);
    }
    radial(180, 130, 380, "rgba(88,28,135,0.28)");
    radial(920, 420, 300, "rgba(30,64,175,0.22)");
    radial(W / 2, H, 320, "rgba(56,189,248,0.08)");

    // Border
    ctx.save();
    roundRect(ctx, 8, 8, W - 16, H - 16, 22);
    ctx.strokeStyle = "rgba(56,189,248,0.22)";
    ctx.lineWidth = 2;
    ctx.stroke();
    ctx.restore();

    // ── AFTR brand (top-left) ──
    ctx.fillStyle = "#eaf2ff";
    ctx.font = "900 62px system-ui, -apple-system, sans-serif";
    ctx.fillText("AFTR", 60, 96);

    ctx.fillStyle = "rgba(56,189,248,0.75)";
    ctx.font = "500 19px system-ui";
    ctx.fillText("Picks con ventaja estadística", 62, 126);

    // ── WIN badge (top-right) ──
    var bW = 116, bH = 46, bX = W - 60 - bW, bY = 38;
    ctx.save();
    ctx.fillStyle = "rgba(34,197,94,0.18)";
    roundRect(ctx, bX, bY, bW, bH, 12);
    ctx.fill();
    ctx.strokeStyle = "rgba(34,197,94,0.45)";
    ctx.lineWidth = 1.5;
    roundRect(ctx, bX, bY, bW, bH, 12);
    ctx.stroke();
    ctx.fillStyle = "#22c55e";
    ctx.font = "800 22px system-ui";
    ctx.textAlign = "center";
    ctx.fillText("WIN ✓", bX + bW / 2, bY + 30);
    ctx.restore();

    // ── Match name ──
    var matchText = (opts.home && opts.away)
      ? (opts.home + " vs " + opts.away) : "";
    ctx.fillStyle = "#eaf2ff";
    ctx.font = "700 38px system-ui";
    ctx.textAlign = "left";
    ctx.fillText(truncate(ctx, matchText, W - 130), 60, 228);

    // ── Market ──
    ctx.fillStyle = "rgba(234,242,255,0.6)";
    ctx.font = "500 24px system-ui";
    ctx.fillText(opts.market || "—", 60, 276);

    // ── Score ──
    if (opts.score_home != null && opts.score_away != null &&
        opts.score_home !== "" && opts.score_away !== "") {
      ctx.fillStyle = "#22c55e";
      ctx.font = "800 56px system-ui";
      ctx.textAlign = "center";
      ctx.fillText(opts.score_home + " - " + opts.score_away, W / 2, 380);
      ctx.textAlign = "left";
    }

    // ── AFTR score chip ──
    if (opts.aftr_score) {
      ctx.save();
      ctx.fillStyle = "rgba(56,189,248,0.12)";
      roundRect(ctx, 60, 318, 136, 34, 8);
      ctx.fill();
      ctx.fillStyle = "#38bdf8";
      ctx.font = "700 16px system-ui";
      ctx.textAlign = "left";
      ctx.fillText("AFTR " + opts.aftr_score, 77, 341);
      ctx.restore();
    }

    // ── Tier chip ──
    if (opts.tier && opts.tier.toLowerCase() !== "pass" && opts.tier !== "—") {
      var tierColors = { elite: "#FFD700", strong: "#00C853", risky: "#FF9800" };
      var tCol = tierColors[opts.tier.toLowerCase()] || "#9E9E9E";
      var tierX = 60 + (opts.aftr_score ? 148 : 0);
      ctx.save();
      ctx.fillStyle = "rgba(255,255,255,0.06)";
      roundRect(ctx, tierX, 318, 96, 34, 8);
      ctx.fill();
      ctx.fillStyle = tCol;
      ctx.font = "700 15px system-ui";
      ctx.textAlign = "left";
      ctx.fillText(opts.tier.toUpperCase(), tierX + 12, 341);
      ctx.restore();
    }

    // ── Divider ──
    ctx.strokeStyle = "rgba(255,255,255,0.07)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(60, H - 60);
    ctx.lineTo(W - 60, H - 60);
    ctx.stroke();

    // ── Footer tagline ──
    ctx.fillStyle = "rgba(234,242,255,0.32)";
    ctx.font = "400 17px system-ui";
    ctx.textAlign = "left";
    ctx.fillText("aftr.app · Apostá con ventaja real", 60, H - 30);

    return canvas;
  }

  /* ── Modal ────────────────────────────────────────────────── */
  function openShareModal(opts) {
    var existing = document.getElementById("aftr-share-modal");
    if (existing) existing.remove();

    var canvas = drawShareCard(opts);
    var home = opts.home || opts.home_team || "";
    var fname = "aftr-win-" + (home || "pick").replace(/\s+/g, "-").toLowerCase() + ".png";

    var modal = document.createElement("div");
    modal.id = "aftr-share-modal";
    modal.className = "aftr-share-overlay";
    modal.innerHTML = [
      '<div class="aftr-share-box">',
        '<div class="aftr-share-head">',
          '<span class="aftr-share-title">Compartir resultado</span>',
          '<button class="aftr-share-x" id="aftr-share-x">✕</button>',
        '</div>',
        '<div class="aftr-share-preview" id="aftr-share-canvas-wrap"></div>',
        '<div class="aftr-share-actions">',
          '<button class="aftr-share-btn aftr-share-btn--dl" id="aftr-share-dl">',
            '&#8681; Descargar',
          '</button>',
          '<button class="aftr-share-btn aftr-share-btn--native" id="aftr-share-native">',
            '&#8679; Compartir',
          '</button>',
        '</div>',
      '</div>'
    ].join("");

    document.body.appendChild(modal);

    canvas.style.cssText = "width:100%;height:auto;border-radius:10px;display:block;";
    document.getElementById("aftr-share-canvas-wrap").appendChild(canvas);

    document.getElementById("aftr-share-dl").addEventListener("click", function () {
      canvas.toBlob(function (blob) {
        var a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = fname;
        a.click();
        setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
      }, "image/png");
    });

    document.getElementById("aftr-share-native").addEventListener("click", function () {
      canvas.toBlob(function (blob) {
        var file = new File([blob], fname, { type: "image/png" });
        var shareData = {
          title: "AFTR — WIN",
          text: "Gané este pick en AFTR · " + (opts.market || "") + " · aftr.app",
        };
        if (
          typeof navigator.share === "function" &&
          typeof navigator.canShare === "function" &&
          navigator.canShare({ files: [file] })
        ) {
          navigator.share(Object.assign(shareData, { files: [file] })).catch(function () {});
        } else if (typeof navigator.share === "function") {
          navigator.share(shareData).catch(function () {});
        } else {
          // Fallback: download
          var a = document.createElement("a");
          a.href = URL.createObjectURL(blob);
          a.download = fname;
          a.click();
          setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
        }
      }, "image/png");
    });

    function closeModal() { modal.remove(); }
    document.getElementById("aftr-share-x").addEventListener("click", closeModal);
    modal.addEventListener("click", function (e) {
      if (e.target === modal) closeModal();
    });
  }

  /* ── Public API + delegated click ────────────────────────── */
  window.AFTRShare = { open: openShareModal };

  document.addEventListener("click", function (e) {
    var btn = e.target.closest(".aftr-share-trigger");
    if (!btn) return;
    e.preventDefault();
    openShareModal({
      home:        btn.dataset.home      || "",
      away:        btn.dataset.away      || "",
      market:      btn.dataset.market    || "",
      aftr_score:  btn.dataset.aftrScore || null,
      tier:        btn.dataset.tier      || "",
      score_home:  btn.dataset.scoreHome != null ? btn.dataset.scoreHome : null,
      score_away:  btn.dataset.scoreAway != null ? btn.dataset.scoreAway : null,
    });
  });
})();
