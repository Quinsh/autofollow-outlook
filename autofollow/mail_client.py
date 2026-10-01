"""Mail provider boundary.

Adapters normalize received messages to the established message dictionary shape
(id, threadId, internalDate, labelIds, payload, snippet). This keeps scheduling,
classification and legacy SQLite IDs compatible without provider conditionals.
"""
from __future__ import annotations
import base64
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Iterator, Protocol


class MailClient(Protocol):
    """Common read/auth contract; submission is coordinated by the send journal."""

    provider: str

    def get_profile_email(self) -> str: ...
    def ensure_authenticated(self) -> str: ...
    def clear_token(self) -> None: ...
    def get_thread_metadata(self, thread_id: str) -> dict[str, Any]: ...
    def get_message_full(self, message_id: str) -> dict[str, Any]: ...
    def iter_inbox_pages_since(self, after_date: str, page_size: int = 100) -> Iterator[list[dict]]: ...


class MailError(RuntimeError):
    def __init__(self, status: int, code: str = ""):
        self.status = status
        self.code = code
        hints = {401: "Run auth to reconnect the mailbox", 403: "Check Microsoft delegated permissions and tenant consent",
                 404: "Message no longer available", 429: "Mail service throttled the request; try again later"}
        super().__init__(f"Mail API HTTP {status}: {hints.get(status, 'Request failed')} ({code})")


def create_mail_client(config) -> MailClient:
    # Reject a provider switch before even reading another mailbox into this DB.
    # Old databases without a binding are verified/adopted by sending.bind_account.
    if config.database_path.exists():
        with closing(sqlite3.connect(config.database_path.as_uri() + "?mode=ro", uri=True)) as con:
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='sending_account'").fetchone():
                columns = {row[1] for row in con.execute("PRAGMA table_info(sending_account)")}
                row = con.execute("SELECT " + ("provider" if "provider" in columns else "'gmail'") +
                                  " FROM sending_account WHERE id=1").fetchone()
                if row and row[0] != config.mail_provider:
                    raise RuntimeError("This database belongs to a different mail provider; use a new database")
    if config.mail_provider == "outlook":
        from .outlook_client import OutlookClient
        return OutlookClient(config)
    from .gmail_client import GmailClient
    return GmailClient(config)


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"p", "div", "br", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def extract_plain_body(payload: dict) -> str:
    """Prefer text/plain across the MIME tree, then safely extract HTML text."""
    def candidates(part, mime):
        headers = {h["name"].lower(): h["value"] for h in part.get("headers", [])}
        if part.get("filename") or headers.get("content-disposition", "").lower().startswith("attachment"):
            return
        if part.get("mimeType") == mime:
            data = part.get("body", {}).get("data", "")
            if data:
                yield base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        for child in part.get("parts", []):
            yield from candidates(child, mime)

    for text in candidates(payload, "text/plain"):
        if text.strip():
            return text
    for html in candidates(payload, "text/html"):
        parser = _HTMLText()
        parser.feed(html)
        text = "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())
        if text:
            return text
    return ""


@dataclass(frozen=True)
class SentMessage:
    """Confirmed sent copy. Legacy ID field name is retained for SQLite compatibility."""

    gmail_message_id: str
    thread_id: str
    rfc_message_id: str | None
    web_link: str | None = None
    sent_at_utc: str | None = None
