"""
JSON API for Render's cron job (finedine-retargeting-broadcast) to trigger
the retargeting campaign. Deliberately mounted outside /admin so it's never
touched by main.py's _admin_auth_redirect exception handler (which
redirects browser requests to /admin/login on a 401 - wrong behavior for a
headless cron caller, which just wants a plain JSON error it can log).

See broadcasts/retargeting.py for why this is an HTTP endpoint rather than
a standalone script reading the database directly: Render cron jobs cannot
attach a persistent disk, so they can't see the SQLite file the web service
uses - only the already-running web service can touch it.
"""
from fastapi import APIRouter, Depends

from broadcasts.auth import require_broadcast_cron_secret
from broadcasts.retargeting import run_retargeting_campaign

router = APIRouter(prefix="/broadcast")


@router.post("/run-retargeting")
def run_retargeting(_=Depends(require_broadcast_cron_secret)):
    return run_retargeting_campaign()
