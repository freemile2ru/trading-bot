"""Trade-signal notifications. SMS via Twilio; email is currently disabled."""

import os
import logging

_log = logging.getLogger(__name__)


def send_email(subject: str, body: str) -> bool:
    """Email alerts are disabled — SMS (Twilio) is the only active channel.

    The SMTP implementation was removed as unreachable code; it's in git history
    (data/notifications.py at the initial commits) if email is wanted again.
    """
    return False


def send_sms(body: str) -> bool:
    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
    from_number = os.getenv("TWILIO_FROM_NUMBER", "")
    to_number = os.getenv("NOTIFY_PHONE", "")

    if not all([account_sid, auth_token, from_number, to_number]):
        _log.warning("SMS not configured — set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, NOTIFY_PHONE in .env")
        return False

    try:
        from twilio.rest import Client  # optional dependency
        client = Client(account_sid, auth_token)
        client.messages.create(body=body[:1600], from_=from_number, to=to_number)
        _log.info("SMS sent to %s", to_number)
        return True
    except ImportError:
        _log.error("Twilio not installed — run: pip install twilio")
        return False
    except Exception as exc:
        _log.error("SMS send failed: %s", exc)
        return False
