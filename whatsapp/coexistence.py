"""
Server-side half of Meta's WhatsApp Embedded Signup flow, used to let a
restaurant connect their EXISTING WhatsApp Business app number (Coexistence)
or a fresh Cloud API number through the admin dashboard, instead of us
hand-configuring WHATSAPP_ACCESS_TOKEN/WHATSAPP_PHONE_NUMBER_ID in .env.

Flow (see templates/whatsapp_connect.html for the client-side half):
  1. Admin opens /admin/whatsapp, clicks "Connect WhatsApp".
  2. Meta's JS SDK (FB.login with config_id) opens a popup, the restaurant
     owner logs into their own Meta Business account and either creates a
     new WABA/number or chooses "use my existing WhatsApp Business app
     number" (-> Coexistence). On success the SDK calls back into our page
     with a short-lived authorization `code` plus the chosen phone_number_id
     and waba_id (sent via postMessage, see the connect template).
  3. The browser POSTs that code to /admin/whatsapp/connect (admin_routes.py).
  4. We exchange the short-lived code for a long-lived access token here
     (exchange_code_for_token) and persist it (storage.store.set_whatsapp_connection).
  5. If this was a Coexistence connection, Meta syncs up to 6 months of chat
     history to our webhook within 24h - see main.py's handling of the
     smb_app_state_sync webhook field.

Requires a Meta Tech Provider / Solution Partner app - config.META_APP_ID/
META_APP_SECRET/META_CONFIG_ID come from that app's Developer Console, not
from the restaurant's own WABA.
"""
import requests

from config import config


class CoexistenceError(Exception):
    pass


def exchange_code_for_token(code: str) -> str:
    """Exchanges the short-lived authorization code Embedded Signup's JS SDK
    hands back on success for a long-lived (60-day, auto-extending with
    use) system user access token, per Meta's standard OAuth token exchange."""
    if not (config.META_APP_ID and config.META_APP_SECRET):
        raise CoexistenceError(
            "META_APP_ID/META_APP_SECRET are not configured - Embedded Signup "
            "requires a Meta Tech Provider app. Set these in .env once your "
            "Tech Provider application is approved."
        )
    resp = requests.get(
        f"https://graph.facebook.com/{config.WHATSAPP_API_VERSION}/oauth/access_token",
        params={
            "client_id": config.META_APP_ID,
            "client_secret": config.META_APP_SECRET,
            "code": code,
        },
        timeout=15,
    )
    if resp.status_code >= 300:
        raise CoexistenceError(f"Token exchange failed {resp.status_code}: {resp.text}")
    data = resp.json()
    if "access_token" not in data:
        raise CoexistenceError(f"Token exchange response missing access_token: {data}")
    return data["access_token"]


def register_phone_number(phone_number_id: str, access_token: str, pin: str = "000000"):
    """Required one-time step after connecting a number (Coexistence or new)
    before it can send/receive via the Cloud API - registers it for Cloud
    API messaging. A 6-digit PIN is required by Meta's API; any value works
    the first time a number is registered this way."""
    resp = requests.post(
        f"https://graph.facebook.com/{config.WHATSAPP_API_VERSION}/{phone_number_id}/register",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={"messaging_product": "whatsapp", "pin": pin},
        timeout=15,
    )
    if resp.status_code >= 300:
        raise CoexistenceError(f"Phone number registration failed {resp.status_code}: {resp.text}")
    return resp.json()
