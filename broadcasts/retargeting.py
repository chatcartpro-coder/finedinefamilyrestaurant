"""
Customer retargeting broadcast - messages customers who've opted in to
marketing but haven't placed a confirmed order in a while, using an
admin-configured, Meta-approved WhatsApp template (see whatsapp/templates.py
and the /admin/retargeting dashboard page).

Triggered on a schedule by a Render cron job calling
POST /broadcast/run-retargeting (see broadcasts/routes.py) - not by a
standalone script reading the database directly, since Render cron jobs
cannot attach a disk (confirmed via Render's own docs: disks are
unavailable to cron services, and never shared between two services even
if both had one). All real work therefore happens inside the already-running
web service, which already holds the DB connection and WhatsApp credentials.
"""
import logging
from datetime import datetime, timezone

from storage import store
from whatsapp.templates import WhatsAppTemplateError, send_template_message

logger = logging.getLogger("finedine-agent")

CAMPAIGN = "retargeting"


def run_retargeting_campaign() -> dict:
    """Returns a summary dict (sent/failed/dry_run counts, segment_size).
    Safe to call more than once in the same period - get_inactive_opted_in_
    customers' cooldown window already prevents double-messaging the same
    customer, so an accidental double-trigger just re-queries a (likely now
    empty or smaller) segment rather than re-sending to the same people."""
    settings = store.get_retargeting_campaign_settings()
    if not settings or not settings["enabled"] or not settings["template_name"]:
        return {"skipped": True, "reason": "not configured or disabled"}

    run_id = datetime.now(timezone.utc).isoformat()
    customers = store.get_inactive_opted_in_customers(
        settings["days_inactive"], settings["cooldown_days"], campaign=CAMPAIGN,
    )

    sent = failed = dry_run_count = 0
    for c in customers:
        # Per-recipient try/except - one bad number must never abort the
        # whole batch (same isolation pattern as main.py's
        # _process_message_status, applied here for the same reason).
        try:
            if settings["dry_run"]:
                store.log_broadcast(
                    run_id, CAMPAIGN, c["phone"], settings["template_name"], settings["language"], "dry_run",
                )
                dry_run_count += 1
                continue
            send_template_message(
                to=c["phone"], name=settings["template_name"], language=settings["language"],
                body_params=[c["name"] or "there"],
            )
            store.log_broadcast(
                run_id, CAMPAIGN, c["phone"], settings["template_name"], settings["language"], "sent",
            )
            sent += 1
        except (WhatsAppTemplateError, Exception) as e:
            logger.exception("Retargeting broadcast failed for %s (non-fatal)", c["phone"])
            store.log_broadcast(
                run_id, CAMPAIGN, c["phone"], settings["template_name"], settings["language"],
                "failed", error=str(e),
            )
            failed += 1

    return {
        "run_id": run_id, "sent": sent, "failed": failed, "dry_run": dry_run_count,
        "segment_size": len(customers),
    }
