"""
Client for sending messages via the Meta WhatsApp Cloud API.
Docs: https://developers.facebook.com/docs/whatsapp/cloud-api
"""
import requests

from config import config


class WhatsAppError(Exception):
    pass


def _base_url(phone_number_id: str = None):
    return f"https://graph.facebook.com/{config.WHATSAPP_API_VERSION}/{phone_number_id or config.WHATSAPP_PHONE_NUMBER_ID}/messages"


def _headers(access_token: str = None):
    return {
        "Authorization": f"Bearer {access_token or config.WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def send_text_message(to: str, body: str, access_token: str = None, phone_number_id: str = None) -> dict:
    """to: recipient phone number in international format, no leading +, e.g. '9715XXXXXXXX'.
    access_token/phone_number_id: optional overrides for a Coexistence-connected
    number (see whatsapp/coexistence.py) - defaults to config.* (the .env-configured
    number) when not given."""
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body, "preview_url": False},
    }
    resp = requests.post(_base_url(phone_number_id), headers=_headers(access_token), json=payload, timeout=15)
    if resp.status_code >= 300:
        raise WhatsAppError(f"WhatsApp send failed {resp.status_code}: {resp.text}")
    return resp.json()


def mark_as_read(message_id: str, access_token: str = None, phone_number_id: str = None) -> dict:
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    }
    resp = requests.post(_base_url(phone_number_id), headers=_headers(access_token), json=payload, timeout=15)
    if resp.status_code >= 300:
        raise WhatsAppError(f"WhatsApp mark-as-read failed {resp.status_code}: {resp.text}")
    return resp.json()


def get_media_url(media_id: str, access_token: str = None) -> str:
    """Step 1 of downloading inbound media: resolve a media_id to a short-lived download URL."""
    resp = requests.get(
        f"https://graph.facebook.com/{config.WHATSAPP_API_VERSION}/{media_id}",
        headers=_headers(access_token),
        timeout=15,
    )
    if resp.status_code >= 300:
        raise WhatsAppError(f"WhatsApp get_media_url failed {resp.status_code}: {resp.text}")
    return resp.json()["url"]


def download_media(media_id: str, access_token: str = None) -> bytes:
    """Step 2: download the actual media bytes. Requires the same auth header
    as the API itself, not just a plain GET."""
    url = get_media_url(media_id, access_token)
    resp = requests.get(url, headers=_headers(access_token), timeout=30)
    if resp.status_code >= 300:
        raise WhatsAppError(f"WhatsApp media download failed {resp.status_code}: {resp.text}")
    return resp.content
