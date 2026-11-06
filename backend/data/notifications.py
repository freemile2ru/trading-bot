"""Notifications: email over SMTP, SMS and WhatsApp via Twilio. Used by the strong-signal alarm (api/routes.py), the
watchdog (ops/watchdog.py) and the trade alerts (trading/trade_alerts.py: every open and close on every book).

WhatsApp (2026-11-06) goes through Twilio's sandbox: the phone must have joined it (a "join <phrase>" text to the
sandbox number from the Twilio console), and WhatsApp accepts a free-form message only within 24 hours of the phone's
last message to the sandbox — outside that window Twilio fails it with error 63016, and only a pre-approved template
is delivered. So a message is sent free-form first; if Twilio reports 63016, it goes again as the sandbox template
named by TWILIO_WHATSAPP_TEMPLATE_SID — any of the pre-approved ones: the template's body is read from Twilio's
Content API to count its {{n}} slots, the alert goes in the last slot and the title and time fill the others
("Your appointment is coming up on {{1}} at {{2}}" reads "…coming up on Oracle prod at CLOSED · SHORT ZEC +$310…").
Any reply to the sandbox reopens 24 hours of plain messages.

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


WHATSAPP_SANDBOX = "+14155238886"                 # Twilio's shared sandbox sender
WINDOW_CLOSED = 63016                             # "outside the allowed window: use a template"


def whatsapp_to() -> str:
    return os.getenv("NOTIFY_WHATSAPP") or os.getenv("NOTIFY_PHONE", "")


def whatsapp_configured() -> bool:
    return bool(os.getenv("TWILIO_ACCOUNT_SID") and os.getenv("TWILIO_AUTH_TOKEN") and whatsapp_to())


def _twilio_client():
    from twilio.rest import Client  # optional dependency
    return Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])


def _final_status(client, sid: str, wait_s: float = 6.0) -> tuple[str, int | None]:
    """WhatsApp delivery errors arrive after the API accepts the message: poll briefly for a settled status."""
    import time
    deadline = time.time() + wait_s
    while True:
        m = client.messages(sid).fetch()
        if m.status in ("delivered", "read", "failed", "undelivered") or time.time() >= deadline:
            return m.status, m.error_code
        time.sleep(0.5)


_slots: dict[str, int] = {}


def template_slots(client, sid: str) -> int:
    """How many {{n}} variables the content template has (read once from the Content API; 4 if it can't be read)."""
    if sid not in _slots:
        try:
            import re
            c = client.content.v1.contents(sid).fetch()
            text = " ".join(str(v) for v in (c.types or {}).values())
            _slots[sid] = max(int(n) for n in re.findall(r"{{\s*(\d+)\s*}}", text)) if "{{" in text else 1
        except Exception as e:
            _log.debug("content template %s unreadable (%s): assuming 4 slots", sid, e)
            _slots[sid] = 4
    return _slots[sid]


def template_variables(slots: int, title: str, when: str, body: str) -> dict[str, str]:
    """The alert in the last slot, the title and time in front of it."""
    if slots <= 1:                                   # "{{1}} is your verification code…": the whole alert, title first
        return {"1": f"{title} · {body}"[:900]}
    front = [title[:60], when or "now", "trade update"][:slots - 1]
    if slots >= 4:
        front = [title[:60], "trade update", when or "now"]
    return {str(i + 1): v for i, v in enumerate(front + [body[:900]])}


def send_whatsapp(body: str, title: str = "Crypto Oracle", when: str = "") -> bool:
    """A WhatsApp message through the sandbox; outside the 24-hour window, the same text inside the template."""
    if not whatsapp_configured():
        _log.warning("WhatsApp not configured — set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and NOTIFY_WHATSAPP (or NOTIFY_PHONE) in .env")
        return False
    sender = "whatsapp:" + (os.getenv("TWILIO_WHATSAPP_FROM") or WHATSAPP_SANDBOX)
    to = "whatsapp:" + whatsapp_to()
    try:
        client = _twilio_client()
        m = client.messages.create(body=body[:1600], from_=sender, to=to)
        status, err = _final_status(client, m.sid)
        if err != WINDOW_CLOSED:
            ok = status not in ("failed", "undelivered")
            (_log.info if ok else _log.error)("WhatsApp %s to %s (status %s%s)", "sent" if ok else "FAILED", to, status, f", error {err}" if err else "")
            return ok
        template = os.getenv("TWILIO_WHATSAPP_TEMPLATE_SID", "")
        if not template:
            _log.error("WhatsApp window closed (24h since the phone last wrote to the sandbox) and no TWILIO_WHATSAPP_TEMPLATE_SID to fall back on")
            return False
        import json
        m = client.messages.create(from_=sender, to=to, content_sid=template,
                                   content_variables=json.dumps(template_variables(template_slots(client, template), title, when, body)))
        status, err = _final_status(client, m.sid)
        ok = status not in ("failed", "undelivered")
        (_log.info if ok else _log.error)("WhatsApp template %s to %s (status %s%s)", "sent" if ok else "FAILED", to, status, f", error {err}" if err else "")
        return ok
    except ImportError:
        _log.error("Twilio not installed — run: pip install twilio")
        return False
    except Exception as exc:
        _log.error("WhatsApp send failed: %s", exc)
        return False


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
