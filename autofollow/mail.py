from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, getaddresses, make_msgid, parseaddr
from typing import Iterable

from .content_safety import validate_content


@dataclass(frozen=True)
class BuiltMessage:
    message: EmailMessage
    raw: str
    rfc_message_id: str


def normalize_email_address(value: str) -> str:
    _, addr = parseaddr(value)
    return addr.strip().lower()


def split_addresses(value: str | Iterable[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        candidates = getaddresses([value])
    else:
        candidates = getaddresses(list(value))
    return [addr.strip() for _, addr in candidates if addr.strip()]


def address_header(addresses: str | Iterable[str] | None) -> str | None:
    values = split_addresses(addresses)
    return ", ".join(values) if values else None


def extract_addresses(header_value: str | None) -> list[str]:
    if not header_value:
        return []
    return [addr.lower() for _, addr in getaddresses([header_value]) if addr]


def make_reply_subject(subject: str) -> str:
    stripped = subject.strip()
    if stripped.lower().startswith("re:"):
        return stripped
    return f"Re: {stripped}"


def encode_message(message: EmailMessage) -> str:
    return base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")


def build_email(
    *,
    to: str | Iterable[str],
    subject: str,
    body: str,
    sender_email: str | None = None,
    sender_name: str | None = None,
    cc: str | Iterable[str] | None = None,
    bcc: str | Iterable[str] | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    reply: bool = False,
) -> BuiltMessage:
    validate_content(subject, field="Subject")
    validate_content(body, field="Message")
    if sender_name:
        validate_content(sender_name, field="Sender name")
    for label, values in (("To", to), ("Cc", cc), ("Bcc", bcc)):
        inputs = [values] if isinstance(values, str) else list(values or [])
        if any("\r" in value or "\n" in value for value in inputs):
            raise ValueError(f"{label} cannot contain newlines")
        for addr in split_addresses(inputs):
            if not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", addr):
                raise ValueError(f"Invalid {label} address: {addr}")
    if sender_email and not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", sender_email):
        raise ValueError("Invalid sender address")
    if not address_header(to):
        raise ValueError("At least one recipient is required")
    if not subject.strip():
        raise ValueError("Subject cannot be empty")
    if not body.strip():
        raise ValueError("Body cannot be empty")

    msg = EmailMessage()
    rfc_message_id = make_msgid()

    msg["To"] = address_header(to)
    cc_header = address_header(cc)
    bcc_header = address_header(bcc)
    if cc_header:
        msg["Cc"] = cc_header
    if bcc_header:
        msg["Bcc"] = bcc_header

    msg["Subject"] = make_reply_subject(subject) if reply else subject.strip()
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = rfc_message_id

    if sender_email:
        msg["From"] = formataddr((sender_name or "", sender_email))

    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references

    msg.set_content(body)

    return BuiltMessage(message=msg, raw=encode_message(msg), rfc_message_id=rfc_message_id)


def format_preview(built: BuiltMessage) -> str:
    """Show the message a person reviews without transport implementation headers."""
    headers = [f"{name}: {built.message[name]}" for name in ("From", "To", "Cc", "Bcc", "Subject")
               if built.message[name]]
    return "\n".join(headers) + "\n\n" + built.message.get_content()
