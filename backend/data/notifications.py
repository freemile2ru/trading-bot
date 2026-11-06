"""Notifications: email over SMTP and SMS via Twilio. Used by the strong-signal alarm (api/routes.py), the watchdog
(ops/watchdog.py) and the trade alerts (trading/trade_alerts.py: every open and close on every book).

Email (2026-11-06, back after being removed as unreachable): SMTP_HOST, SMTP_PORT (465 = TLS from the start, anything
else = STARTTLS), SMTP_USER, SMTP_PASSWORD, and NOTIFY_EMAIL for the recipient (the sender is SMTP_USER unless
SMTP_FROM says otherwise). Both senders block: call them from a thread.
"""

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

_log = logging.getLogger(__name__)


def email_configured() -> bool:
    return all(os.getenv(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "NOTIFY_EMAIL"))


def sms_configured() -> bool:
    return all(os.getenv(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "NOTIFY_PHONE"))


def send_email(subject: str, body: str) -> bool:
    if not email_configured():
        _log.warning("Email not configured — set SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, NOTIFY_EMAIL in .env")
        return False
    host, port = os.environ["SMTP_HOST"], int(os.getenv("SMTP_PORT", "587"))
    user, password, to = os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"], os.environ["NOTIFY_EMAIL"]
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject[:200], os.getenv("SMTP_FROM") or user, to
    msg.set_content(body)
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context()) as s:
                s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(user, password)
                s.send_message(msg)
        _log.info("Email sent to %s: %s", to, subject)
        return True
    except Exception as exc:
        _log.error("Email send failed: %s", exc)
        return False


def send_sms(body: str) -> bool:
    if not sms_configured():
        _log.warning("SMS not configured — set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, NOTIFY_PHONE in .env")
        return False
    try:
        from twilio.rest import Client  # optional dependency
        client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
        client.messages.create(body=body[:1600], from_=os.environ["TWILIO_FROM_NUMBER"], to=os.environ["NOTIFY_PHONE"])
        _log.info("SMS sent to %s", os.environ["NOTIFY_PHONE"])
        return True
    except ImportError:
        _log.error("Twilio not installed — run: pip install twilio")
        return False
    except Exception as exc:
        _log.error("SMS send failed: %s", exc)
        return False
