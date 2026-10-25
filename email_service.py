"""Transactional e-mail (verification, password reset) over plain SMTP.

Works with any provider that offers SMTP, e.g.
  Resend: SMTP_HOST=smtp.resend.com  SMTP_PORT=465  SMTP_USER=resend  SMTP_PASSWORD=<api key>
  Brevo:  SMTP_HOST=smtp-relay.brevo.com  SMTP_PORT=587  SMTP_USER=<login>  SMTP_PASSWORD=<smtp key>
Port 465 uses implicit TLS, anything else uses STARTTLS.
"""
import html
import os
import smtplib
import ssl
from email.message import EmailMessage

APP_NAME = "Accounting Trainer"


def config() -> dict:
    return {
        "host": os.getenv("SMTP_HOST", "").strip(),
        "port": int(os.getenv("SMTP_PORT", "587") or 587),
        "user": os.getenv("SMTP_USER", "").strip(),
        "password": os.getenv("SMTP_PASSWORD", ""),
        "from": os.getenv("MAIL_FROM", "").strip(),
    }


def is_configured() -> bool:
    cfg = config()
    return bool(cfg["host"] and cfg["from"])


def dev_log_enabled() -> bool:
    """Local development: print the e-mail (with its link) to the console instead of sending it."""
    return os.getenv("EMAIL_DEV_LOG", "").lower() in ("1", "true", "yes")


def send_email(to: str, subject: str, text: str, html_body: str) -> bool:
    cfg = config()
    if not is_configured():
        if dev_log_enabled():
            print(f"[email:dev] to={to} subject={subject!r}\n{text}\n")
            return True
        print(f"[email] SMTP is not configured, message to {to} was NOT sent")
        return False

    msg = EmailMessage()
    msg["From"] = cfg["from"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    try:
        context = ssl.create_default_context()
        if cfg["port"] == 465:
            server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=15, context=context)
        else:
            server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=15)
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
        with server:
            if cfg["user"]:
                server.login(cfg["user"], cfg["password"])
            server.send_message(msg)
        return True
    except Exception as e:  # never let a mail problem crash a request handler
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
