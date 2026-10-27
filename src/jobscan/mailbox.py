"""Read recent mail from Gmail over IMAP, read-only.

The mailbox is opened read-only and messages are fetched with BODY.PEEK, so nothing is marked
read, moved, sent or deleted. The app password is passed in by the caller and is never logged.
"""

from __future__ import annotations

import contextlib
import hashlib
import imaplib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from typing import Protocol

from .timeutil import IST, to_iso

MAX_MESSAGES = 1_500
PROGRESS_EVERY = 10
BODY_CHARS = 3_000
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_TAGS = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"\s+")


class MailError(Exception):
    """The mailbox could not be read. The message says why, in plain words."""


@dataclass(frozen=True)
class Mail:
    message_id: str
    sender: str  # the address only
    subject: str
    date: str  # ISO, IST
    body: str  # the first part of the text
    invite: bool  # carries a calendar invitation
    invite_summary: str = ""


# Called with (done, total, available): emails read so far, to read, and in the window.
ReadProgress = Callable[[int, int, int], None]


class MailSource(Protocol):
    def fetch(self, since: datetime, progress: ReadProgress | None = None) -> list[Mail]: ...


def _imap_date(moment: datetime) -> str:
    return f"{moment.day:02d}-{_MONTHS[moment.month - 1]}-{moment.year}"


def _text_of(message: EmailMessage) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except (LookupError, ValueError):
        return ""
    if part.get_content_type() == "text/html":
        text = _TAGS.sub(" ", text)
    return _SPACES.sub(" ", text).strip()[:BODY_CHARS]


def _invite_of(message: EmailMessage) -> tuple[bool, str]:
    for part in message.walk():
        name = (part.get_filename() or "").lower()
        if part.get_content_type() == "text/calendar" or name.endswith(".ics"):
            try:
                raw = part.get_content()
            except (LookupError, ValueError):
                raw = ""
            text = raw if isinstance(raw, str) else raw.decode("utf-8", "ignore")
            found = re.search(r"^SUMMARY:(.+)$", text, re.MULTILINE)
            cancelled = re.search(r"^METHOD:CANCEL", text, re.MULTILINE)
            if cancelled:
                return False, ""
            return True, found.group(1).strip() if found else ""
    return False, ""


def parse_mail(raw: bytes) -> Mail | None:
    """One message from its raw bytes, or None when it cannot be read at all."""
    try:
        message = message_from_bytes(raw, policy=policy.default)
        assert isinstance(message, EmailMessage)
        subject = _SPACES.sub(" ", str(message.get("Subject", ""))).strip()
        sender = parseaddr(str(message.get("From", "")))[1].lower()
        try:
            moment = parsedate_to_datetime(str(message.get("Date", ""))).astimezone(IST)
        except (TypeError, ValueError):
            return None
        message_id = str(message.get("Message-ID", "")).strip()
        if not message_id:
            digest = hashlib.sha256(f"{sender}|{subject}|{moment.isoformat()}".encode())
            message_id = "synthetic-" + digest.hexdigest()[:24]
        invite, summary = _invite_of(message)
        return Mail(message_id, sender, subject, to_iso(moment), _text_of(message), invite, summary)
    except Exception:  # one odd message must not stop the sync
        return None


class ImapSource:
    def __init__(
        self,
        address: str,
        password: str,
        *,
        host: str = "imap.gmail.com",
        port: int = 993,
        connect: Callable[[str, int], imaplib.IMAP4] = imaplib.IMAP4_SSL,
    ) -> None:
        self._address = address
        self._password = password
        self._host, self._port = host, port
        self._connect = connect

    def fetch(self, since: datetime, progress: ReadProgress | None = None) -> list[Mail]:
        try:
            client = self._connect(self._host, self._port)
        except OSError:
            raise MailError("Gmail could not be reached. Check your internet.") from None
        try:
            try:
                client.login(self._address, self._password)
            except imaplib.IMAP4.error:
                raise MailError(
                    "Gmail refused the sign-in. Check the address and create a new app "
                    "password if needed."
                ) from None
            return self._read(client, since, progress)
        except (imaplib.IMAP4.error, OSError):
            raise MailError(
                "Gmail stopped answering while mail was being read. Try again."
            ) from None
        finally:
            with contextlib.suppress(Exception):  # closing is best effort
                client.logout()

    def _read(
        self, client: imaplib.IMAP4, since: datetime, progress: ReadProgress | None
    ) -> list[Mail]:
        status, _ = client.select("INBOX", readonly=True)
        if status != "OK":
            raise MailError("The Gmail inbox could not be opened.")
        status, data = client.search(None, "SINCE", _imap_date(since))
        if status != "OK" or not data or not data[0]:
            return []
        found = data[0].split()
        numbers = found[-MAX_MESSAGES:]
        if progress:
            progress(0, len(numbers), len(found))
        mails: list[Mail] = []
        for index, number in enumerate(numbers, 1):
            status, parts = client.fetch(number, "(BODY.PEEK[])")
            if status != "OK" or not parts:
                continue
            for part in parts:
                if isinstance(part, tuple) and isinstance(part[1], bytes):
                    mail = parse_mail(part[1])
                    if mail is not None:
                        mails.append(mail)
            if progress and (index % PROGRESS_EVERY == 0 or index == len(numbers)):
                progress(index, len(numbers), len(found))
        return mails
