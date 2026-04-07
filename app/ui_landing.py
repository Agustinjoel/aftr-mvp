from __future__ import annotations

import html as html_lib
from fastapi import Request
from config.settings import settings
from app.ui_helpers import AUTH_BOOTSTRAP_SCRIPT


def landing_page(request: Request) -> str:
    """Página de marketing para visitantes no logueados."""
    from app.ui_picks_calc import _result_norm, _unit_delta, _pick_stake_units
    from app.ui_data import _load_all_leagues_data

    # ── Stats reales del sistema ───────────────────────────────
    try:
        _, _, all_settled, _, _, _ = _load_all_leagues_data()
        wins   = sum(1 for p in all_settled if _result_norm(p) == "WIN")
        losses = sum(1 for p in all_settled if _result_norm(p) == "LOSS")
        total_wl = wins + losses
        winrate_str = f"{round(wins / total_wl * 100, 1)}%" if total_wl > 0 else "—"
        total_profit = sum(_unit_delta(p) for p in all_settled)
        total_stake  = sum(_pick_stake_units(p) for p in all_settled)
        roi_str = f"{total_profit / total_stake * 100:+.1f}%" if total_stake > 0 else "—"
        picks_count = str(total_wl) if total_wl > 0 else "—"
    except Exception:
        winrate_str = "—"
        roi_str     = "—"
        picks_count = "—"

    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8"/>
  <title>AFTR — Picks con ventaja real</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="stylesheet" href="/static/style.css?v=31">
  <link rel="icon" type="image/png" href="/static/logo_aftr.png">
  <meta name="description" content="AFTR analiza cada partido y detecta picks con edge positivo sobre el mercado. Más de {picks_count} picks analizados.">
  <meta name="theme-color" content="#0d1117">
  <style>
    /* ── Landing-specific styles ── */
    .ld-nav{{
      display:flex; align-items:center; justify-content:space-between;
      padding:16px 24px; max-width:1000px; margin:0 auto;
    }}
    .ld-logo{{ font-size:20px; font-weight:800; color:#fff; text-decoration:none; letter-spacing:-0.5px; }}
    .ld-logo span{{ color:#38bdf8; }}
    .ld-nav-actions{{ display:flex; gap:10px; }}

    .ld-hero{{
      text-align:center; padding:72px 24px 56px;
      max-width:700px; margin:0 auto;
    }}
    .ld-hero-eyebrow{{
      display:inline-block; font-size:11px; font-weight:700; letter-spacing:1.5px;
      text-transform:uppercase; color:#38bdf8;
      background:rgba(56,189,248,.1); border:1px solid rgba(56,189,248,.2);
      border-radius:20px; padding:4px 12px; margin-bottom:20px;
    }}
    .ld-hero h1{{
      font-size:clamp(2rem, 6vw, 3.2rem); font-weight:900; line-height:1.15;
      color:#fff; margin:0 0 16px; letter-spacing:-1px;
    }}
    .ld-hero h1 span{{ color:#38bdf8; }}
    .ld-hero p{{
      font-size:1.1rem; color:rgba(234,242,255,.7); line-height:1.65;
      margin:0 0 36px; max-width:520px; margin-left:auto; margin-right:auto;
    }}
    .ld-hero-ctas{{ display:flex; gap:12px; justify-content:center; flex-wrap:wrap; }}
    .ld-cta-primary{{
      display:inline-block; background:#38bdf8; color:#000;
      font-weight:700; font-size:15px; padding:14px 28px;
      border-radius:12px; text-decoration:none;
      transition:transform .15s, box-shadow .15s;
    }}
    .ld-cta-primary:hover{{ transform:translateY(-2px); box-shadow:0 8px 24px rgba(56,189,248,.35); }}
    .ld-cta-secondary{{
      display:inline-block; color:rgba(234,242,255,.8);
      font-size:14px; padding:14px 20px; text-decoration:none;
      border:1px solid rgba(255,255,255,.15); border-radius:12px;
    }}
    .ld-cta-secondary:hover{{ border-color:rgba(255,255,255,.35); color:#fff; }}

    .ld-stats{{
      display:flex; justify-content:center; gap:0;
      max-width:600px; margin:48px auto;
      background:rgba(255,255,255,.03);
      border:1px solid rgba(255,255,255,.08);
      border-radius:16px; overflow:hidden;
    }}
    .ld-stat{{
      flex:1; text-align:center; padding:20px 16px;
      border-right:1px solid rgba(255,255,255,.08);
    }}
    .ld-stat:last-child{{ border-right:none; }}
    .ld-stat-val{{ font-size:1.6rem; font-weight:900; color:#38bdf8; }}
    .ld-stat-lbl{{ font-size:11px; color:rgba(234,242,255,.5); text-transform:uppercase; letter-spacing:.8px; margin-top:4px; }}

    .ld-section{{ max-width:800px; margin:0 auto; padding:40px 24px; }}
    .ld-section-title{{
      font-size:1.4rem; font-weight:800; color:#fff;
      text-align:center; margin:0 0 32px;
    }}

    .ld-steps{{
      display:flex; gap:16px; flex-wrap:wrap; justify-content:center;
    }}
    .ld-step{{
      flex:1; min-width:200px; max-width:240px;
      background:rgba(255,255,255,.03); border:1px solid rgba(255,255,255,.08);
      border-radius:14px; padding:24px 20px; text-align:center;
    }}
    .ld-step-icon{{ font-size:28px; margin-bottom:12px; }}
    .ld-step-num{{
      font-size:11px; font-weight:800; color:#38bdf8;
      letter-spacing:1px; text-transform:uppercase; margin-bottom:8px;
    }}
    .ld-step-title{{ font-size:15px; font-weight:700; color:#fff; margin-bottom:8px; }}
    .ld-step-desc{{ font-size:13px; color:rgba(234,242,255,.6); line-height:1.5; }}

    .ld-free-vs{{
      display:grid; grid-template-columns:1fr 1fr; gap:16px;
    }}
    .ld-plan{{
      background:rgba(255,255,255,.03); border:1px solid rgba(255,255,255,.08);
      border-radius:14px; padding:24px 20px;
    }}
    .ld-plan--premium{{
      border-color:rgba(56,189,248,.3);
      background:linear-gradient(135deg, rgba(56,189,248,.07), rgba(255,255,255,.02));
    }}
    .ld-plan-name{{ font-size:13px; font-weight:800; color:rgba(234,242,255,.6); letter-spacing:1px; text-transform:uppercase; margin-bottom:4px; }}
    .ld-plan--premium .ld-plan-name{{ color:#38bdf8; }}
    .ld-plan-price{{ font-size:1.5rem; font-weight:900; color:#fff; margin-bottom:16px; }}
    .ld-plan-price span{{ font-size:13px; font-weight:400; color:rgba(234,242,255,.5); }}
    .ld-plan-feature{{ font-size:13px; color:rgba(234,242,255,.7); padding:5px 0; border-bottom:1px solid rgba(255,255,255,.05); }}
    .ld-plan-feature:last-child{{ border-bottom:none; }}
    .ld-plan-feature::before{{ content:"✓ "; color:#22c55e; font-weight:700; }}
    .ld-plan-feature--no{{ color:rgba(234,242,255,.3); }}
    .ld-plan-feature--no::before{{ content:"✗ "; color:rgba(234,242,255,.2); }}

    .ld-final{{
      text-align:center; padding:56px 24px 80px;
      max-width:560px; margin:0 auto;
    }}
    .ld-final h2{{ font-size:1.8rem; font-weight:900; color:#fff; margin:0 0 12px; }}
    .ld-final p{{ color:rgba(234,242,255,.6); font-size:15px; margin:0 0 28px; }}

    .ld-footer{{
      text-align:center; padding:20px 24px;
      border-top:1px solid rgba(255,255,255,.06);
      font-size:12px; color:rgba(234,242,255,.35);
    }}
    .ld-footer a{{ color:rgba(234,242,255,.4); text-decoration:none; margin:0 8px; }}

    @media(max-width:540px){{
      .ld-stat-val{{ font-size:1.3rem; }}
      .ld-free-vs{{ grid-template-columns:1fr; }}
      .ld-steps{{ flex-direction:column; align-items:center; }}
      .ld-step{{ max-width:100%; }}
    }}
  </style>
</head>
<body>

<!-- Nav -->
<nav>
  <div class="ld-nav">
    <a href="/" class="ld-logo">AFTR<span>.</span></a>
    <div class="ld-nav-actions">
      <a href="/?auth=login" class="pill" style="font-size:13px;padding:7px 16px;">Entrar</a>
      <a href="/?auth=register" class="pill" style="font-size:13px;padding:7px 16px;background:#38bdf8;color:#000;font-weight:700;border-color:#38bdf8;">Gratis →</a>
    </div>
  </div>
</nav>

<!-- Hero -->
<section class="ld-hero">
  <div class="ld-hero-eyebrow">Motor de predicción deportiva</div>
  <h1>Apostá con <span>ventaja real</span><br>sobre el mercado</h1>
  <p>AFTR analiza probabilidades, cuotas y estadísticas para detectar picks donde el mercado está equivocado. Sin corazonadas — solo datos.</p>
  <div class="ld-hero-ctas">
    <a href="/?auth=register" class="ld-cta-primary">Crear cuenta gratis →</a>
    <a href="/?auth=login" class="ld-cta-secondary">Ya tengo cuenta</a>
  </div>
</section>

<!-- Stats reales -->
<div class="ld-stats" style="max-width:600px;margin:0 auto 48px;">
  <div class="ld-stat">
    <div class="ld-stat-val">{winrate_str}</div>
    <div class="ld-stat-lbl">Acierto histórico</div>
  </div>
  <div class="ld-stat">
    <div class="ld-stat-val">{roi_str}</div>
    <div class="ld-stat-lbl">ROI histórico</div>
  </div>
  <div class="ld-stat">
    <div class="ld-stat-val">{picks_count}</div>
    <div class="ld-stat-lbl">Picks analizados</div>
  </div>
</div>

<!-- Cómo funciona -->
<section class="ld-section">
  <h2 class="ld-section-title">¿Cómo funciona?</h2>
  <div class="ld-steps">
    <div class="ld-step">
      <div class="ld-step-icon">📊</div>
      <div class="ld-step-num">Paso 1</div>
      <div class="ld-step-title">AFTR analiza</div>
      <div class="ld-step-desc">El motor compara la probabilidad real de cada resultado contra la cuota del bookie.</div>
    </div>
    <div class="ld-step">
      <div class="ld-step-icon">🎯</div>
      <div class="ld-step-num">Paso 2</div>
      <div class="ld-step-title">Elegís un pick</div>
      <div class="ld-step-desc">Ves los picks ordenados por AFTR Score — mayor score, mayor ventaja sobre el mercado.</div>
    </div>
    <div class="ld-step">
      <div class="ld-step-icon">📈</div>
      <div class="ld-step-num">Paso 3</div>
      <div class="ld-step-title">Seguís el resultado</div>
      <div class="ld-step-desc">Guardá la apuesta en el Tracker y recibí una notificación cuando termine el partido.</div>
    </div>
  </div>
</section>

<!-- Free vs Premium -->
<section class="ld-section">
  <h2 class="ld-section-title">Empezá gratis, crecé con Premium</h2>
  <div class="ld-free-vs">
    <div class="ld-plan">
      <div class="ld-plan-name">Free</div>
      <div class="ld-plan-price">$0 <span>/ siempre</span></div>
      <div class="ld-plan-feature">Picks de ligas seleccionadas</div>
      <div class="ld-plan-feature">AFTR Score básico</div>
      <div class="ld-plan-feature">Tracker de apuestas</div>
      <div class="ld-plan-feature--no">Picks ELITE y STRONG</div>
      <div class="ld-plan-feature--no">Todas las ligas</div>
      <div class="ld-plan-feature--no">Edge y análisis completo</div>
    </div>
    <div class="ld-plan ld-plan--premium">
      <div class="ld-plan-name">Premium</div>
      <div class="ld-plan-price">$10.000 <span>ARS / mes</span></div>
      <div class="ld-plan-feature">Todo lo del plan Free</div>
      <div class="ld-plan-feature">Picks ELITE y STRONG</div>
      <div class="ld-plan-feature">Todas las ligas</div>
      <div class="ld-plan-feature">Edge y análisis completo</div>
      <div class="ld-plan-feature">7 días de prueba gratis</div>
    </div>
  </div>
</section>

<!-- CTA final -->
<div class="ld-final">
  <h2>Empezá hoy</h2>
  <p>Registrate gratis y probá Premium 7 días sin cargo. Sin tarjeta requerida.</p>
  <a href="/?auth=register" class="ld-cta-primary" style="font-size:16px;padding:16px 36px;">
    Crear cuenta gratis →
  </a>
</div>

<!-- Footer -->
<div class="ld-footer">
  © 2026 AFTR ·
  <a href="/terminos">Términos</a>
  <a href="/privacidad">Privacidad</a>
  <a href="/?auth=login">Entrar</a>
</div>

{AUTH_BOOTSTRAP_SCRIPT}
</body>
</html>"""
    return html
