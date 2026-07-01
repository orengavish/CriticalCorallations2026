"""
Galgo email utility — sends via Gmail SMTP using an app password.

Credentials: C:/Projects/Galgo2026/secrets.ini  (gitignored)
  [gmail]
  user = gavish.oren@gmail.com
  app_password = xxxx xxxx xxxx xxxx

Usage from Python:
    from send_email import send
    send("Subject here", "Plain text body")
    send("Subject", "<h2>HTML body</h2>", html=True)

Usage from command line:
    python send_email.py "Subject" "Body text"
"""

import configparser
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

_SECRETS = Path(__file__).parent / "secrets.ini"
_SMTP_HOST = "smtp.gmail.com"
_SMTP_PORT = 587


def _load_creds():
    if not _SECRETS.exists():
        raise FileNotFoundError(
            f"secrets.ini not found at {_SECRETS}\n"
            "Create it with:\n"
            "  [gmail]\n"
            "  user = gavish.oren@gmail.com\n"
            "  app_password = xxxx xxxx xxxx xxxx"
        )
    cfg = configparser.ConfigParser()
    cfg.read(_SECRETS)
    return cfg["gmail"]["user"], cfg["gmail"]["app_password"].replace(" ", "")


def send(subject: str, body: str, *, to: str | None = None, html: bool = False) -> None:
    user, password = _load_creds()
    recipient = to or user  # default: send to self

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = recipient

    if html:
        msg.attach(MIMEText(body, "html", "utf-8"))
    else:
        msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(user, password)
        smtp.sendmail(user, recipient, msg.as_string())

    print(f"[send_email] Sent '{subject}' to {recipient}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python send_email.py <subject> <body>")
        sys.exit(1)
    send(sys.argv[1], sys.argv[2])
