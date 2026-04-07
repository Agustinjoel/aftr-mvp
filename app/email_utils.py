from __future__ import annotations

import logging
import os
import smtplib
from email.mime.text import MIMEText

import requests

logger = logging.getLogger("aftr.email")

APP_NAME = "AFTR"
APP_URL  = (os.getenv("APP_BASE_URL") or "https://aftrapp.online").rstrip("/")
_from_env = (os.getenv("RESEND_FROM_EMAIL") or "").strip()
FROM_EMAIL = _from_env if _from_env else f"{APP_NAME} <picks@aftrapp.online>"

_RESEND_SEND_URL = "https://api.resend.com/emails"


# ─────────────────────────────────────────────
# Backend: Resend (primario) o SMTP (fallback)
# ─────────────────────────────────────────────

def _send_via_resend(to_email: str, subject: str, html_body: str) -> bool:
    api_key = (os.getenv("RESEND_API_KEY") or "").strip()
    if not api_key:
        return False
    try:
        r = requests.post(
            _RESEND_SEND_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"from": FROM_EMAIL, "to": [to_email], "subject": subject, "html": html_body},
            timeout=10,
        )
        if r.status_code in (200, 201):
            logger.info("resend: email enviado a %s | subject=%s", to_email, subject)
            return True
        logger.warning("resend: status %s al enviar a %s: %s", r.status_code, to_email, r.text[:200])
        return False
    except Exception:
        logger.error("resend: excepción al enviar a %s", to_email, exc_info=True)
        return False


def _send_via_smtp(to_email: str, subject: str, html_body: str) -> bool:
    server   = (os.getenv("SMTP_SERVER") or "").strip()
    port_str = (os.getenv("SMTP_PORT") or "0").strip()
    user     = (os.getenv("SMTP_USER") or "").strip()
    password = (os.getenv("SMTP_PASSWORD") or "").strip()
    from_e   = (os.getenv("EMAIL_FROM") or "").strip()
    try:
        port = int(port_str)
    except ValueError:
        port = 0
    if not server or not port or not from_e:
        return False
    msg = MIMEText(html_body or "", "html", "utf-8")
    msg["Subject"] = subject or ""
    msg["From"]    = from_e
    msg["To"]      = to_email
    try:
        if port == 465:
            with smtplib.SMTP_SSL(server, port) as s:
                if user and password:
                    s.login(user, password)
                s.sendmail(from_e, [to_email], msg.as_string())
        else:
            with smtplib.SMTP(server, port, timeout=10) as s:
                s.ehlo()
                try:
                    s.starttls(); s.ehlo()
                except smtplib.SMTPException:
                    pass
                if user and password:
                    s.login(user, password)
                s.sendmail(from_e, [to_email], msg.as_string())
        logger.info("smtp: email enviado a %s | subject=%s", to_email, subject)
        return True
    except Exception:
        logger.error("smtp: excepción al enviar a %s", to_email, exc_info=True)
        return False


def send_email(to_email: str, subject: str, html_body: str) -> bool:
    """Envía email: intenta Resend primero, luego SMTP como fallback."""
    if _send_via_resend(to_email, subject, html_body):
        return True
    return _send_via_smtp(to_email, subject, html_body)


# ─────────────────────────────────────────────
# Base HTML del email (diseño dark/branded)
# ─────────────────────────────────────────────

def _email_wrapper(content_html: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="es">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#0b0f14;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0b0f14;padding:40px 16px;">
    <tr><td align="center">
      <table width="100%" style="max-width:480px;background:#141920;border-radius:16px;overflow:hidden;">
        <!-- Header -->
        <tr>
          <td style="background:#111827;padding:24px 32px;text-align:center;border-bottom:1px solid rgba(255,255,255,.08);">
            <span style="font-size:22px;font-weight:700;color:#ffffff;letter-spacing:-0.5px;">{APP_NAME}</span>
            <span style="font-size:22px;font-weight:300;color:#4ade80;"> Picks</span>
          </td>
        </tr>
        <!-- Body -->
        <tr>
          <td style="padding:32px;">
            {content_html}
          </td>
        </tr>
        <!-- Footer -->
        <tr>
          <td style="padding:20px 32px;text-align:center;border-top:1px solid rgba(255,255,255,.06);">
            <p style="margin:0;font-size:12px;color:#4b5563;">
              © 2026 {APP_NAME} · <a href="{APP_URL}" style="color:#4b5563;">aftr.app</a>
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


# ─────────────────────────────────────────────
# Templates de email
# ─────────────────────────────────────────────

def send_welcome_email(to_email: str, username: str) -> bool:
    """Email de bienvenida cuando el usuario se registra."""
    content = f"""
      <h2 style="margin:0 0 8px;color:#ffffff;font-size:20px;">Bienvenido, {username} 👋</h2>
      <p style="margin:0 0 20px;color:#9ca3af;font-size:15px;line-height:1.6;">
        Tu cuenta de <strong style="color:#fff;">AFTR Picks</strong> está lista.
        El motor de análisis ya está procesando los partidos de hoy.
      </p>

      <table width="100%" style="background:#1f2937;border-radius:12px;padding:20px;margin-bottom:24px;">
        <tr>
          <td style="padding:8px 0;border-bottom:1px solid rgba(255,255,255,.06);">
            <span style="color:#4ade80;font-size:14px;">✓</span>
            <span style="color:#d1d5db;font-size:14px;margin-left:8px;">Picks del día con probabilidad y edge</span>
          </td>
        </tr>
        <tr>
          <td style="padding:8px 0;border-bottom:1px solid rgba(255,255,255,.06);">
            <span style="color:#4ade80;font-size:14px;">✓</span>
            <span style="color:#d1d5db;font-size:14px;margin-left:8px;">AFTR Score por cada apuesta</span>
          </td>
        </tr>
        <tr>
          <td style="padding:8px 0;">
            <span style="color:#4ade80;font-size:14px;">✓</span>
            <span style="color:#d1d5db;font-size:14px;margin-left:8px;">Tabla de posiciones y forma de cada equipo</span>
          </td>
        </tr>
      </table>

      <a href="{APP_URL}"
         style="display:block;background:#4ade80;color:#000;text-align:center;padding:14px 24px;border-radius:10px;font-weight:600;font-size:15px;text-decoration:none;">
        Ver los picks de hoy →
      </a>

      <p style="margin:20px 0 0;color:#6b7280;font-size:13px;text-align:center;">
        ¿Querés más picks y datos avanzados? <a href="{APP_URL}/?open=premium" style="color:#4ade80;">Probá Premium</a>
      </p>
    """
    return send_email(to_email, f"Bienvenido a {APP_NAME} Picks 🎯", _email_wrapper(content))


def send_premium_welcome_email(to_email: str, username: str) -> bool:
    """Email de bienvenida cuando el usuario activa Premium."""
    content = f"""
      <div style="text-align:center;margin-bottom:28px;">
        <div style="display:inline-block;background:linear-gradient(135deg,#4ade80,#22c55e);border-radius:50%;width:64px;height:64px;line-height:64px;font-size:32px;">⭐</div>
      </div>

      <h2 style="margin:0 0 8px;color:#ffffff;font-size:22px;text-align:center;">¡Ya sos Premium, {username}!</h2>
      <p style="margin:0 0 28px;color:#9ca3af;font-size:15px;line-height:1.6;text-align:center;">
        Desbloqueaste el análisis completo de AFTR. Así es como sacarle el máximo provecho:
      </p>

      <!-- Bloque 1: Qué es AFTR -->
      <table width="100%" style="background:#1f2937;border-radius:12px;padding:20px;margin-bottom:16px;">
        <tr>
          <td>
            <p style="margin:0 0 10px;color:#ffffff;font-size:14px;font-weight:600;letter-spacing:.3px;">¿QUÉ ES AFTR?</p>
            <p style="margin:0;color:#9ca3af;font-size:14px;line-height:1.6;">
              Un motor de predicción deportiva que analiza forma reciente, estadísticas de liga y cuotas de mercado
              para identificar picks con <strong style="color:#4ade80;">valor real</strong> — no tips al azar.
            </p>
          </td>
        </tr>
      </table>

      <!-- Bloque 2: Cómo usar -->
      <table width="100%" style="background:#1f2937;border-radius:12px;padding:20px;margin-bottom:16px;">
        <tr><td>
          <p style="margin:0 0 14px;color:#ffffff;font-size:14px;font-weight:600;letter-spacing:.3px;">CÓMO USARLO</p>
          <table width="100%">
            <tr>
              <td style="padding:6px 0;border-bottom:1px solid rgba(255,255,255,.05);">
                <span style="color:#4ade80;font-size:13px;font-weight:700;">01</span>
                <span style="color:#d1d5db;font-size:14px;margin-left:10px;">Revisá los picks del día — están ordenados por AFTR Score</span>
              </td>
            </tr>
            <tr>
              <td style="padding:6px 0;border-bottom:1px solid rgba(255,255,255,.05);">
                <span style="color:#4ade80;font-size:13px;font-weight:700;">02</span>
                <span style="color:#d1d5db;font-size:14px;margin-left:10px;">Mirá la probabilidad y el edge — picks con edge &gt; 5% son los más valiosos</span>
              </td>
            </tr>
            <tr>
              <td style="padding:6px 0;border-bottom:1px solid rgba(255,255,255,.05);">
                <span style="color:#4ade80;font-size:13px;font-weight:700;">03</span>
                <span style="color:#d1d5db;font-size:14px;margin-left:10px;">Tocá "Ver partido" para ver la tabla de posiciones y estadísticas del equipo</span>
              </td>
            </tr>
            <tr>
              <td style="padding:6px 0;">
                <span style="color:#4ade80;font-size:13px;font-weight:700;">04</span>
                <span style="color:#d1d5db;font-size:14px;margin-left:10px;">Guardá los picks que te interesan con el botón ★ para seguirlos</span>
              </td>
            </tr>
          </table>
        </td></tr>
      </table>

      <!-- Bloque 3: Lo que desbloqueaste -->
      <table width="100%" style="background:#1f2937;border-radius:12px;padding:20px;margin-bottom:28px;">
        <tr><td>
          <p style="margin:0 0 14px;color:#ffffff;font-size:14px;font-weight:600;letter-spacing:.3px;">TU PLAN PREMIUM INCLUYE</p>
          <table width="100%">
            <tr><td style="padding:5px 0;"><span style="color:#4ade80;">✓</span> <span style="color:#d1d5db;font-size:14px;margin-left:6px;">Picks de todas las ligas (Premier, LaLiga, Serie A, NBA y más)</span></td></tr>
            <tr><td style="padding:5px 0;"><span style="color:#4ade80;">✓</span> <span style="color:#d1d5db;font-size:14px;margin-left:6px;">AFTR Score completo — probabilidad, edge y confianza</span></td></tr>
            <tr><td style="padding:5px 0;"><span style="color:#4ade80;">✓</span> <span style="color:#d1d5db;font-size:14px;margin-left:6px;">Tabla de posiciones y forma de cada equipo en el drawer</span></td></tr>
            <tr><td style="padding:5px 0;"><span style="color:#4ade80;">✓</span> <span style="color:#d1d5db;font-size:14px;margin-left:6px;">Seguimiento de picks guardados con resultado final</span></td></tr>
          </table>
        </td></tr>
      </table>

      <a href="{APP_URL}"
         style="display:block;background:#4ade80;color:#000;text-align:center;padding:14px 24px;border-radius:10px;font-weight:700;font-size:15px;text-decoration:none;margin-bottom:20px;">
        Ver los picks de hoy →
      </a>

      <p style="margin:0;color:#6b7280;font-size:12px;text-align:center;line-height:1.6;">
        Tu suscripción se renueva automáticamente cada mes.<br>
        Podés cancelarla cuando quieras desde tu cuenta.
      </p>
    """
    return send_email(to_email, f"⭐ Bienvenido a {APP_NAME} Premium", _email_wrapper(content))


def send_pick_follow_email(to_email: str, username: str, home: str, away: str, market: str, aftr_score: float | None, tier: str | None, kickoff_str: str | None) -> bool:
    """Email de confirmación cuando el usuario sigue un pick."""
    tier_label = (tier or "").upper()
    tier_badge = ""
    if tier_label in ("ELITE", "STRONG"):
        tier_color = "#22c55e" if tier_label == "STRONG" else "#FFD700"
        tier_badge = f'<span style="background:rgba(255,255,255,.07);color:{tier_color};border-radius:5px;padding:2px 8px;font-size:12px;font-weight:700;margin-left:8px;">{tier_label}</span>'
    score_line = f'<p style="margin:6px 0 0;font-size:13px;color:#9ca3af;">AFTR Score: <strong style="color:#38bdf8;">{int(round(aftr_score))}</strong>{tier_badge}</p>' if aftr_score is not None else ""
    kickoff_line = f'<p style="margin:6px 0 0;font-size:13px;color:#9ca3af;">Inicio: <strong style="color:#fff;">{kickoff_str}</strong></p>' if kickoff_str else ""
    match_text = f"{home} vs {away}" if home and away else (home or away or "Partido")
    content = f"""
      <h2 style="margin:0 0 6px;color:#ffffff;font-size:19px;">Pick guardado ✓</h2>
      <p style="margin:0 0 22px;color:#9ca3af;font-size:14px;">Hola {username}, este es tu pick:</p>

      <table width="100%" style="background:#1f2937;border-radius:12px;padding:18px 20px;margin-bottom:22px;">
        <tr><td>
          <p style="margin:0 0 4px;font-size:16px;font-weight:700;color:#ffffff;">{match_text}</p>
          <p style="margin:0 0 0;font-size:14px;color:#4ade80;font-weight:600;">{market}</p>
          {score_line}
          {kickoff_line}
        </td></tr>
      </table>

      <a href="{APP_URL}"
         style="display:block;background:#38bdf8;color:#000;text-align:center;padding:13px 24px;border-radius:10px;font-weight:700;font-size:14px;text-decoration:none;margin-bottom:18px;">
        Ver todos los picks →
      </a>
      <p style="margin:0;color:#4b5563;font-size:12px;text-align:center;">
        Podés ver el resultado de este pick en tu panel de cuenta.
      </p>
    """
    return send_email(to_email, f"Pick guardado: {match_text} · {market}", _email_wrapper(content))


def send_trial_expiring_email(to_email: str, username: str, days_left: int) -> bool:
    """Email cuando el trial expira en 1 o 2 días."""
    if days_left <= 1:
        urgency_line = "Tu prueba <strong style='color:#f87171;'>vence hoy</strong>."
        cta_color    = "#f87171"
    else:
        urgency_line = f"Tu prueba <strong style='color:#fbbf24;'>vence en {days_left} días</strong>."
        cta_color    = "#4ade80"

    content = f"""
      <div style="text-align:center;margin-bottom:24px;">
        <div style="display:inline-block;background:rgba(251,191,36,.12);border-radius:50%;width:60px;height:60px;line-height:60px;font-size:28px;">⏳</div>
      </div>

      <h2 style="margin:0 0 8px;color:#ffffff;font-size:20px;text-align:center;">
        {urgency_line}
      </h2>
      <p style="margin:0 0 24px;color:#9ca3af;font-size:15px;line-height:1.6;text-align:center;">
        Hola {username}, aprovechaste el análisis de AFTR estos días.<br>
        Para seguir viendo picks con edge real, activá tu plan.
      </p>

      <table width="100%" style="background:#1f2937;border-radius:12px;padding:20px;margin-bottom:24px;">
        <tr><td>
          <p style="margin:0 0 12px;color:#ffffff;font-size:14px;font-weight:600;letter-spacing:.3px;">QUÉ PERDÉS AL EXPIRAR</p>
          <table width="100%">
            <tr><td style="padding:5px 0;border-bottom:1px solid rgba(255,255,255,.05);">
              <span style="color:#f87171;">✗</span>
              <span style="color:#d1d5db;font-size:14px;margin-left:8px;">Picks de todas las ligas (Premier, LaLiga, Serie A…)</span>
            </td></tr>
            <tr><td style="padding:5px 0;border-bottom:1px solid rgba(255,255,255,.05);">
              <span style="color:#f87171;">✗</span>
              <span style="color:#d1d5db;font-size:14px;margin-left:8px;">AFTR Score completo + edge por pick</span>
            </td></tr>
            <tr><td style="padding:5px 0;">
              <span style="color:#f87171;">✗</span>
              <span style="color:#d1d5db;font-size:14px;margin-left:8px;">Picks ELITE y STRONG sin restricciones</span>
            </td></tr>
          </table>
        </td></tr>
      </table>

      <a href="{APP_URL}/?open=premium"
         style="display:block;background:{cta_color};color:#000;text-align:center;padding:14px 24px;border-radius:10px;font-weight:700;font-size:15px;text-decoration:none;margin-bottom:16px;">
        Activar Premium ahora →
      </a>

      <p style="margin:0;color:#6b7280;font-size:12px;text-align:center;line-height:1.6;">
        Si no querés continuar, no hay nada que hacer — tu cuenta pasa a free automáticamente.
      </p>
    """
    subject = (
        f"⚠️ Tu prueba AFTR vence hoy — activá tu plan"
        if days_left <= 1
        else f"⏳ Te quedan {days_left} días de AFTR Premium"
    )
    return send_email(to_email, subject, _email_wrapper(content))


def send_reset_email(to_email: str, reset_link: str) -> bool:
    """Email de recuperación de contraseña."""
    content = f"""
      <h2 style="margin:0 0 8px;color:#ffffff;font-size:20px;">Recuperar contraseña</h2>
      <p style="margin:0 0 24px;color:#9ca3af;font-size:15px;line-height:1.6;">
        Recibimos una solicitud para restablecer la contraseña de tu cuenta.
        Si no fuiste vos, ignorá este email.
      </p>

      <a href="{reset_link}"
         style="display:block;background:#4ade80;color:#000;text-align:center;padding:14px 24px;border-radius:10px;font-weight:600;font-size:15px;text-decoration:none;margin-bottom:20px;">
        Restablecer contraseña →
      </a>

      <p style="margin:0 0 8px;color:#6b7280;font-size:13px;">O copiá este link en tu navegador:</p>
      <p style="margin:0;background:#1f2937;border-radius:8px;padding:12px;font-size:12px;color:#9ca3af;word-break:break-all;">
        {reset_link}
      </p>

      <p style="margin:20px 0 0;color:#6b7280;font-size:12px;">
        Este link expira en 1 hora. Si no solicitaste esto, tu cuenta sigue segura.
      </p>
    """
    return send_email(to_email, f"{APP_NAME} — Restablecer tu contraseña", _email_wrapper(content))
