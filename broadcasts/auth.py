"""
Auth for the broadcast-trigger API surface (/broadcast/*) - a shared-secret
bearer token, deliberately separate from admin/auth.py's cookie-based admin
session scheme. The caller is Render's cron job (finedine-retargeting-
broadcast), not a logged-in admin browser session, so it gets its own
minimal auth instead of reusing (or working around) the cookie/redirect-
oriented admin flow - same reasoning as print_agent/auth.py.
"""
from fastapi import HTTPException, Request

from config import config


def require_broadcast_cron_secret(request: Request):
    if not config.BROADCAST_CRON_SECRET:
        raise HTTPException(
            status_code=503,
            detail="BROADCAST_CRON_SECRET is not configured on the server - set it in .env to enable scheduled broadcasts.",
        )
    if request.headers.get("Authorization") != f"Bearer {config.BROADCAST_CRON_SECRET}":
        raise HTTPException(status_code=401, detail="Invalid or missing broadcast cron secret")
