"""Shared Jinja2 environment for the admin dashboard."""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

from config import config

templates = Jinja2Templates(directory=os.path.join(config.BASE_DIR, "templates"))


def _local_time(value, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Jinja filter: converts a UTC ISO timestamp (as stored everywhere in
    storage/store.py, e.g. datetime.now(timezone.utc).isoformat()) to the
    restaurant's local time (STORE_TIMEZONE) for display - every admin
    dashboard timestamp was previously shown as raw UTC with no conversion,
    confirmed wrong live for a UAE-based restaurant. Falls back to showing
    the raw value if it can't be parsed (e.g. empty/malformed), rather than
    erroring the whole page."""
    if not value:
        return "-"
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            from datetime import timezone as _tz
            dt = dt.replace(tzinfo=_tz.utc)
        return dt.astimezone(ZoneInfo(config.STORE_TIMEZONE)).strftime(fmt)
    except (ValueError, TypeError):
        return str(value)[:16].replace("T", " ")


templates.env.filters["local_time"] = _local_time


def render(request, template_name: str, **context):
    context.setdefault("store_name", config.STORE_NAME)
    context.setdefault("currency", config.CURRENCY)
    context.setdefault("config", config)
    return templates.TemplateResponse(request, template_name, context)
