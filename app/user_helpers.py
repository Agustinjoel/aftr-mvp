"""
User access helpers for AFTR. Roles: guest, free_user, premium_user, admin.
Subscription states: inactive, active, expired, trial.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import Request


def is_admin(user: dict | None, request: Request | None = None) -> bool:
    """
    True if user has admin role or request is from localhost (dev override).
    Guest (user is None) is not admin unless localhost.
    """
    if user is not None and (user.get("role") or "").strip().lower() == "admin":
        return True
    if request is not None:
        client_ip = getattr(getattr(request, "client", None), "host", "")
        if client_ip in ("127.0.0.1", "::1"):
            return True
    return False


def is_premium_active(user: dict | None) -> bool:
    """
    True if user is premium_user and subscription is active or trial (and not expired).
    Guest and free_user return False. expired or inactive return False.
    """
    if user is None:
        return False
    role = (user.get("role") or "").strip().lower()
    if role != "premium_user":
        return False
    status = (user.get("subscription_status") or "").strip().lower()
    if status == "active":
        return True
    if status == "trial":
        sub_end = user.get("subscription_end")
        if not sub_end:
            return True  # no expiry set → allow
        try:
            end_dt = datetime.fromisoformat(str(sub_end).replace("Z", "+00:00"))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            return end_dt > datetime.now(timezone.utc)
        except Exception:
            return True
    return False


def trial_days_remaining(user: dict | None) -> int | None:
    """Returns days left in trial, or None if not on trial."""
    if user is None:
        return None
    status = (user.get("subscription_status") or "").strip().lower()
    if status != "trial":
        return None
    sub_end = user.get("subscription_end")
    if not sub_end:
        return None
    try:
        end_dt = datetime.fromisoformat(str(sub_end).replace("Z", "+00:00"))
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        delta = end_dt - datetime.now(timezone.utc)
        return max(0, delta.days + 1)
    except Exception:
        return None


def can_see_all_picks(user: dict | None, request: Request | None = None) -> bool:
    """
    True if user can see all picks (no free 3-pick limit).
    Rules: admin always; premium_user only if subscription active/trial; else False.
    """
    return is_admin(user, request) or is_premium_active(user)
