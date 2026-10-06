"""
Loads all configuration from environment variables (.env).
No secrets live in code - this file only reads them.
"""
import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # WhatsApp
    WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
    WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
    WHATSAPP_BUSINESS_ACCOUNT_ID = os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID", "")
    WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
    WHATSAPP_API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v20.0")

    # WhatsApp Embedded Signup (Coexistence) - lets a restaurant connect its
    # existing WhatsApp Business app number through the admin dashboard
    # instead of us hand-configuring WHATSAPP_ACCESS_TOKEN/PHONE_NUMBER_ID
    # above. Requires a Meta Tech Provider / Solution Partner app - these
    # three come from that app's Meta Developer Console, not from a WABA.
    # See whatsapp/coexistence.py and templates/whatsapp_connect.html.
    META_APP_ID = os.getenv("META_APP_ID", "")
    META_APP_SECRET = os.getenv("META_APP_SECRET", "")
    META_CONFIG_ID = os.getenv("META_CONFIG_ID", "")  # Embedded Signup configuration ID

    # OpenRouter
    OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
    OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemma-4-26b-a4b-it:free")
    # Used only for image messages - must be a vision/multimodal-capable model.
    # Defaults to the same model; override if OPENROUTER_MODEL doesn't support images.
    OPENROUTER_VISION_MODEL = os.getenv("OPENROUTER_VISION_MODEL") or OPENROUTER_MODEL
    OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

    # Fallback chain for text replies - tried in order after OPENROUTER_MODEL
    # if a call fails (rate limit, provider outage, etc). Spread across
    # different free-tier providers so one provider's outage doesn't take
    # all of them down, with one cheap paid model as the last-resort safety
    # net (OPENROUTER_API_KEY still gates whether any of this runs at all).
    # Override via a comma-separated OPENROUTER_FALLBACK_MODELS env var.
    OPENROUTER_FALLBACK_MODELS = [
        m.strip() for m in os.getenv(
            "OPENROUTER_FALLBACK_MODELS",
            "meta-llama/llama-3.3-70b-instruct:free,"
            "mistralai/mistral-small-3.2-24b-instruct:free,"
            "qwen/qwen-2.5-72b-instruct:free,"
            "google/gemini-2.0-flash-001",
        ).split(",") if m.strip()
    ]

    # Voice transcription (Groq's Whisper endpoint by default - fast, free-tier
    # friendly, OpenAI-compatible. Swap to OpenAI's whisper-1 by changing
    # WHISPER_BASE_URL/WHISPER_API_KEY/WHISPER_MODEL - ai/voice.py doesn't
    # care which provider, it's a plain REST call either way.
    WHISPER_API_KEY = os.getenv("WHISPER_API_KEY", "")
    WHISPER_BASE_URL = os.getenv("WHISPER_BASE_URL", "https://api.groq.com/openai/v1")
    WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-large-v3")

    # Admin session auth
    ADMIN_SESSION_SECRET = os.getenv("ADMIN_SESSION_SECRET", "")
    ADMIN_SESSION_COOKIE = "finedine_admin_session"
    ADMIN_SESSION_MAX_AGE = 60 * 60 * 12  # 12 hours

    # Print agent auth - a shared secret the restaurant's local print-agent
    # script sends as a header on every request; separate from admin session
    # cookies since a print client isn't a logged-in admin browser session.
    # Printer integration is deferred for this restaurant (per scope), but the
    # module is present and ready the moment they get a printer.
    PRINT_AGENT_TOKEN = os.getenv("PRINT_AGENT_TOKEN", "")

    # Billing/invoicing connector - optional, disabled until a vendor is wired
    # into billing/connector.py. Setting BILLING_API_BASE_URL is what turns
    # the best-effort push in main.py's _confirm_order() on; see that module's
    # docstring.
    BILLING_API_BASE_URL = os.getenv("BILLING_API_BASE_URL", "")
    BILLING_API_KEY = os.getenv("BILLING_API_KEY", "")

    # Restaurant info
    STORE_NAME = os.getenv("STORE_NAME", "Fine Dine Family Restaurant")
    STORE_PHONE = os.getenv("STORE_PHONE", "")
    STORE_LAT = os.getenv("STORE_LAT", "")
    STORE_LNG = os.getenv("STORE_LNG", "")
    # IANA timezone name used to compute the current daypart (breakfast/lunch/
    # dinner/etc.) for AI recommendations - defaults to UTC if unset.
    STORE_TIMEZONE = os.getenv("STORE_TIMEZONE", "UTC")

    # Delivery pricing
    DELIVERY_FEE = float(os.getenv("DELIVERY_FEE", "10"))
    FREE_DELIVERY_THRESHOLD = float(os.getenv("FREE_DELIVERY_THRESHOLD", "100"))
    CURRENCY = os.getenv("CURRENCY", "AED")

    PORT = int(os.getenv("PORT", "8000"))

    # Paths
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = os.path.join(BASE_DIR, "data")
    # DB_DATA_DIR defaults to DATA_DIR (local dev: everything lives in ./data,
    # alongside the bundled catalog_seed.xlsx). On Render, set DB_DATA_DIR to
    # the mounted persistent disk's path (see render.yaml's `disk` block) -
    # deliberately NOT the same path as DATA_DIR/catalog_seed.xlsx, since a
    # disk mounted directly over ./data would shadow the git-committed seed
    # file with an empty volume on first boot and the menu would never
    # auto-import (see main.py's _auto_import_catalog_if_empty).
    DB_DATA_DIR = os.getenv("DB_DATA_DIR") or DATA_DIR
    DB_PATH = os.path.join(DB_DATA_DIR, "app.db")
    CATALOG_SEED_PATH = os.path.join(DATA_DIR, "catalog_seed.xlsx")


config = Config()


def apply_store_settings_override():
    """Applies any admin-saved store_settings row (storage/store.py) on top
    of the .env-sourced defaults above, so a Settings-page save takes effect
    immediately without a redeploy/restart. Called once at app startup and
    again right after a save. Deferred import avoids a circular import
    (storage/store.py imports config)."""
    from storage import store

    saved = store.get_store_settings()
    if not saved:
        return
    if saved.get("store_name"):
        config.STORE_NAME = saved["store_name"]
    if saved.get("currency"):
        config.CURRENCY = saved["currency"]
    if saved.get("delivery_fee") is not None:
        config.DELIVERY_FEE = saved["delivery_fee"]
    if saved.get("free_delivery_threshold") is not None:
        config.FREE_DELIVERY_THRESHOLD = saved["free_delivery_threshold"]
    if saved.get("store_phone"):
        config.STORE_PHONE = saved["store_phone"]


def apply_whatsapp_connection_override():
    """Applies an Embedded Signup-connected number (storage/store.py's
    whatsapp_connection row) on top of the .env-configured
    WHATSAPP_ACCESS_TOKEN/WHATSAPP_PHONE_NUMBER_ID/WHATSAPP_BUSINESS_ACCOUNT_ID,
    so a restaurant that connects via the dashboard doesn't need a redeploy.
    Called once at startup and again right after a successful Embedded Signup
    or disconnect. Deferred import avoids a circular import."""
    from storage import store

    conn = store.get_whatsapp_connection()
    if not conn:
        return
    config.WHATSAPP_ACCESS_TOKEN = conn["access_token"]
    config.WHATSAPP_PHONE_NUMBER_ID = conn["phone_number_id"]
    if conn.get("waba_id"):
        config.WHATSAPP_BUSINESS_ACCOUNT_ID = conn["waba_id"]
