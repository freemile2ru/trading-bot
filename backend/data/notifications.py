"""Email (SMTP/Gmail) and SMS (Twilio) notifications for trade signals."""

import os
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

_log = logging.getLogger(__name__)


def send_email(subject: str, body: str) -> bool:
    return False

    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASSWORD", "")
    to_email = os.getenv("NOTIFY_EMAIL", "")

    if not all([smtp_user, smtp_pass, to_email]):
        _log.warning("Email not configured — set SMTP_USER, SMTP_PASSWORD, NOTIFY_EMAIL in .env")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = to_email
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo()
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, to_email, msg.as_string())
        _log.info("Email sent to %s", to_email)
        return True
    except Exception as exc:
        _log.error("Email send failed: %s", exc)
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
