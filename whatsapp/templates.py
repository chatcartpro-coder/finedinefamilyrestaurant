"""
WhatsApp message template management via the Graph API - lets the admin
dashboard create/list/delete the pre-approved templates needed to message a
customer *outside* the 24-hour session window (e.g. "your order is ready"
sent well after their last message), unlike the free-form replies
whatsapp/client.py sends inside an active conversation.

Scoped to the WhatsApp Business Account (WABA), not a phone number - see
https://developers.facebook.com/docs/whatsapp/business-management-api/message-templates.
Every created template starts in Meta's own review queue (PENDING) and is
only usable once Meta approves it (APPROVED) - that review is separate from,
and unrelated to, Meta's App Review process for this app itself.
"""
import requests

from config import config


class WhatsAppTemplateError(Exception):
    pass


def _base_url(waba_id: str = None):
    return f"https://graph.facebook.com/{config.WHATSAPP_API_VERSION}/{waba_id or config.WHATSAPP_BUSINESS_ACCOUNT_ID}/message_templates"


def _headers(access_token: str = None):
    return {
        "Authorization": f"Bearer {access_token or config.WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def list_templates(waba_id: str = None, access_token: str = None) -> list:
    resp = requests.get(_base_url(waba_id), headers=_headers(access_token), params={"limit": 100}, timeout=15)
    if resp.status_code >= 300:
        raise WhatsAppTemplateError(f"Failed to list templates {resp.status_code}: {resp.text}")
    return resp.json().get("data", [])


def create_template(name: str, category: str, body_text: str, language: str = "en_US",
                     footer_text: str = None, waba_id: str = None, access_token: str = None) -> dict:
    """Creates a text-only template (BODY, optional FOOTER - no header media
    or buttons, kept minimal). `name` must be lowercase_with_underscores per
    Meta's naming rule. `category` is one of MARKETING, UTILITY,
    AUTHENTICATION. Submitted templates start PENDING in Meta's review
    queue - see list_templates()/get_template_status() to check progress."""
    components = [{"type": "BODY", "text": body_text}]
    if footer_text:
        components.append({"type": "FOOTER", "text": footer_text})

    payload = {
        "name": name,
        "language": language,
        "category": category,
        "components": components,
    }
    resp = requests.post(_base_url(waba_id), headers=_headers(access_token), json=payload, timeout=15)
    if resp.status_code >= 300:
        raise WhatsAppTemplateError(f"Failed to create template {resp.status_code}: {resp.text}")
    return resp.json()


def delete_template(name: str, waba_id: str = None, access_token: str = None) -> dict:
    resp = requests.delete(
        _base_url(waba_id), headers=_headers(access_token), params={"name": name}, timeout=15,
    )
    if resp.status_code >= 300:
        raise WhatsAppTemplateError(f"Failed to delete template {resp.status_code}: {resp.text}")
    return resp.json()


def send_template_message(to: str, name: str, language: str = "en_US", body_params: list = None,
                           access_token: str = None, phone_number_id: str = None) -> dict:
    """Sends an APPROVED template to a customer - the only way to message
    someone outside the 24h session window. body_params: ordered list of
    strings filling the template's {{1}}, {{2}}, ... placeholders, if any."""
    from whatsapp.client import _base_url as _messages_url, _headers as _msg_headers, WhatsAppError

    components = []
    if body_params:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": str(p)} for p in body_params],
        })

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": name,
            "language": {"code": language},
            **({"components": components} if components else {}),
        },
    }
    resp = requests.post(_messages_url(phone_number_id), headers=_msg_headers(access_token), json=payload, timeout=15)
    if resp.status_code >= 300:
        raise WhatsAppError(f"WhatsApp template send failed {resp.status_code}: {resp.text}")
    return resp.json()
