/**
 * aftr-onboarding.js — First-visit 3-step onboarding modal
 * Shown once per browser (localStorage flag). Zero-cost on repeat visits.
 */
(function () {
  "use strict";

  var KEY = "aftr_onboarded";
  var STEP_COUNT = 3;

  // Don't show if already onboarded or auth modal is open
  if (localStorage.getItem(KEY)) return;
  if (window.location.search.indexOf("auth=") !== -1) return;
  if (window.location.search.indexOf("open=") !== -1) return;

  var steps = [
    {
      icon: "📊",
      title: "Bienvenido a AFTR",
      body: "AFTR analiza miles de datos de fútbol para encontrar picks donde el mercado está equivocado. No es intuición — es matemática.",
      cta: "Siguiente",
    },
    {
      icon: "🎯",
      title: "Cómo funciona el AFTR Score",
      body: "<strong>ELITE &amp; STRONG</strong> — señal de alta convicción, ventaja estadística positiva.<br><strong>RISKY</strong> — hay valor pero con más incertidumbre.<br><br>Cada pick muestra el edge (ventaja en %) y la cuota sugerida.",
      cta: "Siguiente",
    },
    {
      icon: "⭐",
      title: "Seguí y guardá picks",
      body: 'Guardá los picks que te interesan y seguílos para ver si ganan. Tu historial y racha se calculan automáticamente.<br><br><a href="/account#mi-equipo" class="onb-team-link">Elegir mi equipo favorito →</a>',
      cta: "Empezar",
    },
  ];

  var currentStep = 0;
  var overlay, box, iconEl, titleEl, bodyEl, ctaBtn, dotsWrap, skipBtn;

  function dot(i) {
    return '<span class="onb-dot' + (i === currentStep ? " onb-dot--active" : "") + '"></span>';
  }

  function render() {
    var s = steps[currentStep];
    iconEl.textContent = s.icon;
    titleEl.textContent = s.title;
    bodyEl.innerHTML = s.body;
    ctaBtn.textContent = s.cta;
    dotsWrap.innerHTML = steps.map(function (_, i) { return dot(i); }).join("");
    // Animate step in
    box.classList.remove("onb-step-in");
    void box.offsetWidth; // reflow
    box.classList.add("onb-step-in");
  }

  function finish() {
    localStorage.setItem(KEY, "1");
    overlay.style.animation = "winFadeOut .28s ease forwards";
    setTimeout(function () { overlay.remove(); }, 300);
  }

  function next() {
    if (currentStep < STEP_COUNT - 1) {
      currentStep++;
      render();
    } else {
      finish();
    }
  }

  function build() {
    overlay = document.createElement("div");
    overlay.className = "onb-overlay";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");

    box = document.createElement("div");
    box.className = "onb-box onb-step-in";

    iconEl  = document.createElement("div"); iconEl.className  = "onb-icon";
    titleEl = document.createElement("h2");  titleEl.className = "onb-title";
    bodyEl  = document.createElement("p");   bodyEl.className  = "onb-body";

    var footer = document.createElement("div");
    footer.className = "onb-footer";

    dotsWrap = document.createElement("div");
    dotsWrap.className = "onb-dots";

    ctaBtn = document.createElement("button");
    ctaBtn.className = "onb-cta pill";
    ctaBtn.addEventListener("click", next);

    skipBtn = document.createElement("button");
    skipBtn.className = "onb-skip";
    skipBtn.textContent = "Saltar";
    skipBtn.addEventListener("click", finish);

    footer.appendChild(dotsWrap);
    footer.appendChild(ctaBtn);

    box.appendChild(iconEl);
    box.appendChild(titleEl);
    box.appendChild(bodyEl);
    box.appendChild(footer);
    box.appendChild(skipBtn);

    overlay.appendChild(box);
    document.body.appendChild(overlay);

    // Close on backdrop click
    overlay.addEventListener("click", function (e) {
      if (e.target === overlay) finish();
    });

    render();
  }

  // Delay to let the page paint first
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { setTimeout(build, 900); });
  } else {
    setTimeout(build, 900);
  }
})();
