"""Sending mail over SMTP and reading 'WATCH #id' requests over IMAP."""

from __future__ import annotations

import email
import email.utils
import imaplib
import logging
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.message import EmailMessage
from urllib.parse import quote

from .config import WATCH_TAG, Settings

log = logging.getLogger(__name__)

REQUEST_RE = re.compile(r"\b(UN)?WATCH\s+#([\w-]+)(?:\s+·\s+(.+?)\s+@\s)?", re.I)


@dataclass
class WatchRequest:
    message_id: str
    sender: str
    event_id: str
    unwatch: bool
    artist: str = ""


def watch_mailto(settings: Settings, event_id: str, label: str, unwatch: bool = False) -> str:
    """label must look like "Artist @ Venue · Thu 23 Oct" (the artist is parsed back out)."""
    verb = "UNWATCH" if unwatch else "WATCH"
    subject = f"{WATCH_TAG} {verb} #{event_id} · {label}"
    body = (
        "Just hit send. The next Concert Digest run picks this up.\n"
        "Don't edit the subject line."
    )
    return f"mailto:{settings.watch_address}?subject={quote(subject)}&body={quote(body)}"


def send(settings: Settings, subject: str, html: str, text: str) -> None:
    if not (settings.smtp_user and settings.smtp_password and settings.mail_to):
        raise RuntimeError("Set SMTP_USER, SMTP_PASSWORD and MAIL_TO to send email (or use --dry-run).")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email.utils.formataddr(("Concert Digest", settings.mail_from))
    msg["To"] = settings.mail_to
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain="concert-digest.local")
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    ctx = ssl.create_default_context()
    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=ctx) as s:
            s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as s:
            s.starttls(context=ctx)
            s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
    log.info("sent %r to %s", subject, settings.mail_to)


def fetch_watch_requests(settings: Settings, already_seen: set[str], days: int = 30) -> list[WatchRequest]:
    """Find 'WATCH #id' mails sent by an allowed address in the last `days` days."""
    if not (settings.smtp_user and settings.smtp_password):
        log.warning("No mail credentials; skipping watch requests.")
        return []
    since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
    out: list[WatchRequest] = []
    with imaplib.IMAP4_SSL(settings.imap_host) as imap:
        imap.login(settings.smtp_user, settings.smtp_password)
        # Mail you send to yourself lands in All Mail on Gmail; fall back to INBOX elsewhere.
        for box in ('"[Gmail]/All Mail"', "INBOX"):
            if imap.select(box, readonly=True)[0] == "OK":
                break
        status, data = imap.search(None, "SINCE", since, "SUBJECT", f'"{WATCH_TAG}"', "SUBJECT", '"WATCH #"')
        if status != "OK":
            return []
        for num in data[0].split():
            status, parts = imap.fetch(num, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM MESSAGE-ID)])")
            if status != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            headers = email.message_from_bytes(parts[0][1])
            msg_id = headers.get("Message-ID", f"imap-{num.decode()}").strip()
            if msg_id in already_seen:
                continue
            subject = str(make_header(decode_header(headers.get("Subject", ""))))
            sender = email.utils.parseaddr(headers.get("From", ""))[1].lower()
            m = REQUEST_RE.search(subject)
            if not m:
                continue
            if settings.watch_allowed_from and sender not in settings.watch_allowed_from:
                log.warning("ignoring watch request from %s (not in WATCH_ALLOWED_FROM)", sender)
                continue
            out.append(WatchRequest(msg_id, sender, m.group(2), unwatch=bool(m.group(1)), artist=m.group(3) or ""))
    return out
