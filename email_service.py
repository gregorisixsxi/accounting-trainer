"""Transactional e-mail (verification, password reset) through the Resend HTTP API.

  RESEND_API_KEY  your Resend API key (re_...). Without it nothing is sent and the app keeps running.
  MAIL_FROM       sender address on your verified Resend domain. Defaults to 'Accounting Trainer <auth@accountingtrainer.app>'.
  EMAIL_DEV_LOG   1 = print the e-mails (with their links) to the console instead of sending (local development).
"""
import html
import json
import os
import urllib.error
import urllib.request

APP_NAME = "Accounting Trainer"
RESEND_URL = "https://api.resend.com/emails"
DEFAULT_FROM = f"{APP_NAME} <auth@accountingtrainer.app>"   # the verified domain; MAIL_FROM overrides it
TIMEOUT_SECONDS = 15


def config() -> dict:
    return {
        "api_key": os.getenv("RESEND_API_KEY", "").strip(),
        "from": os.getenv("MAIL_FROM", "").strip() or DEFAULT_FROM,
    }


def is_configured() -> bool:
    return bool(config()["api_key"])


def dev_log_enabled() -> bool:
    """Local development: print the e-mail (with its link) to the console instead of sending it."""
    return os.getenv("EMAIL_DEV_LOG", "").lower() in ("1", "true", "yes")


def send_email(to: str, subject: str, text: str, html_body: str) -> bool:
    """Sends one message. Returns True on success; never raises, so a mail problem cannot crash a request."""
    cfg = config()
    if not cfg["api_key"]:
        if dev_log_enabled():
            print(f"[email:dev] to={to} subject={subject!r}\n{text}\n")
            return True
        print(f"[email] RESEND_API_KEY is not set, the message to {to} was NOT sent")
        return False

    payload = {"from": cfg["from"], "to": [to], "subject": subject, "html": html_body, "text": text}
    request = urllib.request.Request(
        RESEND_URL,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Authorization": "Bearer " + cfg["api_key"],
            "Content-Type": "application/json",
            "User-Agent": "accounting-trainer/1.0",     # Resend rejects requests without a User-Agent
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return 200 <= response.status < 300
    except urllib.error.HTTPError as e:
        # e.g. 403 "You can only send testing emails to your own email address" or an unverified domain
        print(f"[email] Resend rejected the message to {to}: HTTP {e.code} {e.read()[:300]!r}")
    except Exception as e:
        print(f"[email] sending to {to} failed: {e!r}")
    return False


def _layout(title: str, intro: str, link: str, button: str, footer: str) -> str:
    safe_link = html.escape(link, quote=True)
    return f"""<!doctype html>
<html><body style="margin:0;padding:24px;background:#f4f4f5;font-family:Arial,Helvetica,sans-serif;color:#18181b">
  <div style="max-width:480px;margin:0 auto;background:#ffffff;border-radius:14px;padding:32px">
    <p style="margin:0 0 4px;font-size:12px;letter-spacing:.2em;text-transform:uppercase;color:#71717a">{APP_NAME}</p>
    <h1 style="margin:0 0 16px;font-family:'Times New Roman',serif;font-weight:normal;font-size:26px">{html.escape(title)}</h1>
    <p style="margin:0 0 24px;font-size:15px;line-height:1.5">{html.escape(intro)}</p>
    <p style="margin:0 0 24px"><a href="{safe_link}" style="display:inline-block;background:#18181b;color:#ffffff;text-decoration:none;padding:12px 22px;border-radius:10px;font-size:15px">{html.escape(button)}</a></p>
    <p style="margin:0 0 8px;font-size:13px;color:#52525b">Or paste this link into your browser:<br><span style="word-break:break-all">{safe_link}</span></p>
    <p style="margin:16px 0 0;font-size:13px;color:#71717a">{html.escape(footer)}</p>
  </div>
</body></html>"""


def send_verification_email(to: str, link: str) -> bool:
    text = (
        f"Welcome to {APP_NAME}!\n\n"
        f"Confirm your email to activate your account:\n{link}\n\n"
        "This link is valid for 24 hours. If you did not create an account, you can ignore this message."
    )
    body = _layout(
        "Confirm your email",
        "Thanks for signing up. Confirm your email address to activate your account.",
        link,
        "Confirm email",
        "This link is valid for 24 hours. If you did not create an account, ignore this message.",
    )
    return send_email(to, f"Confirm your email for {APP_NAME}", text, body)


def send_password_reset_email(to: str, link: str) -> bool:
    text = (
        f"We received a request to reset your {APP_NAME} password.\n\n"
        f"Choose a new password here:\n{link}\n\n"
        "This link is valid for 1 hour. If you did not ask for this, you can ignore this message: your password stays the same."
    )
    body = _layout(
        "Reset your password",
        "We received a request to reset your password. Click the button to choose a new one.",
        link,
        "Choose a new password",
        "This link is valid for 1 hour. If you did not ask for this, ignore this message: your password stays the same.",
    )
    return send_email(to, f"Reset your {APP_NAME} password", text, body)
