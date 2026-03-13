from __future__ import annotations

import logging
import os
import smtplib
from email.mime.text import MIMEText
from typing import Optional

logger = logging.getLogger("aftr.email")


def _smtp_config() -> dict:
    return {
        "server": (os.getenv("SMTP_SERVER") or "").strip(),
        "port": int(os.getenv("SMTP_PORT") or "0") or 0,
        "user": (os.getenv("SMTP_USER") or "").strip(),
        "password": (os.getenv("SMTP_PASSWORD") or "").strip(),
        "from_email": (os.getenv("EMAIL_FROM") or "").strip(),
    }


def send_email(to_email: str, subject: str, html_body: str) -> bool:
    """
    Send an HTML email via SMTP.
    Uses SMTP_SERVER, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_FROM.
    Logs errors but never raises, so the app flow does not crash.
    """
    cfg = _smtp_config()
    if not cfg["server"] or not cfg["port"] or not cfg["from_email"]:
        logger.warning("SMTP not configured; skipping email to %s", to_email)
        return False

    msg = MIMEText(html_body or "", "html", "utf-8")
    msg["Subject"] = subject or ""
    msg["From"] = cfg["from_email"]
    msg["To"] = to_email

    try:
        if cfg["port"] == 465:
            smtp_cls = smtplib.SMTP_SSL
            smtp_kwargs = {}
        else:
            smtp_cls = smtplib.SMTP
            smtp_kwargs = {"timeout": 10}

        with smtp_cls(cfg["server"], cfg["port"], **smtp_kwargs) as server:
            server.ehlo()
            if cfg["port"] != 465:
                try:
                    server.starttls()
                    server.ehlo()
                except smtplib.SMTPException:
                    # If STARTTLS fails, continue without TLS (for local/dev)
                    logger.warning("STARTTLS failed; continuing without TLS", exc_info=True)
            if cfg["user"] and cfg["password"]:
                server.login(cfg["user"], cfg["password"])
            server.sendmail(cfg["from_email"], [to_email], msg.as_string())
        logger.info("Sent email to %s with subject %s", to_email, subject)
        return True
    except Exception:
        logger.error("Error sending email to %s", to_email, exc_info=True)
        return False

