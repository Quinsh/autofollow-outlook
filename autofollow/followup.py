from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from email.utils import parseaddr
from string import Formatter

from .db import TrackedThread
from .mail import extract_addresses
from .sequence import saved_plan, progress
from .timeutil import from_gmail_internal_ms, from_iso, to_iso, utc_now


@dataclass(frozen=True)
class ReplyInfo:
    gmail_message_id: str
    from_header: str | None
    received_at_utc: str


def headers_map(message: dict) -> dict[str, str]:
    payload = message.get("payload", {}) or {}
    headers = payload.get("headers", []) or []
    out: dict[str, str] = {}
    for item in headers:
        name = str(item.get("name", "")).lower()
        value = str(item.get("value", ""))
        if name:
            out[name] = value
    return out


def is_from_self(from_header: str | None, own_addresses: set[str]) -> bool:
    if not from_header:
        return False
    message_addresses = set(extract_addresses(from_header))
    return bool(message_addresses & {address.lower() for address in own_addresses})


def is_outgoing(message: dict, own_addresses: set[str]) -> bool:
    """Recognize sent aliases, but never treat an unsent draft as a delivery."""
    labels = set(message.get("labelIds", []) or [])
    return "DRAFT" not in labels and (
        "SENT" in labels or is_from_self(headers_map(message).get("from"), own_addresses)
    )


def find_external_reply(
    thread: dict,
    *,
    own_addresses: set[str],
    after_utc: str,
    initial_message_id: str | None = None,
) -> ReplyInfo | None:
    """Return the earliest non-self message after the initial send time."""
    after_dt = from_iso(after_utc)
    if after_dt is None:
        return None

    candidates: list[ReplyInfo] = []
    for message in thread.get("messages", []) or []:
        if "DRAFT" in (message.get("labelIds", []) or []):
            continue
        if initial_message_id and str(message.get("id", "")) == initial_message_id:
            continue
        received = from_gmail_internal_ms(message.get("internalDate"))
        # Stored timestamps have second precision. With an explicit initial ID,
        # conservatively recognize an incoming reply in that same second too.
        if received is None or received < after_dt or (received == after_dt and not initial_message_id):
            continue

        headers = headers_map(message)
        from_header = headers.get("from")
        if is_outgoing(message, own_addresses):
            continue

        candidates.append(
            ReplyInfo(
                gmail_message_id=str(message.get("id", "")),
                from_header=from_header,
                received_at_utc=to_iso(received) or after_utc,
            )
        )

    if not candidates:
        return None
    candidates.sort(key=lambda item: item.received_at_utc)
    return candidates[0]


def latest_outgoing_at(thread: dict, own_addresses: set[str]) -> str | None:
    dates = [received for message in thread.get("messages", []) or []
             if is_outgoing(message, own_addresses)
             if (received := from_gmail_internal_ms(message.get("internalDate"))) is not None]
    return to_iso(max(dates)) if dates else None


def next_due_at(thread: TrackedThread):
    if thread.status != "waiting" or thread.followup_count >= thread.max_followups:
        return None
    plan = saved_plan(thread)
    if plan is not None:
        index, count = progress(thread)
        if index >= count:
            return None
        initial = from_iso(thread.initial_sent_at_utc)
        if initial is None:
            return None
        step = plan[index]
        scheduled = initial + timedelta(days=step["day"])
        # Keep the intended gap after a delayed follow-up or manual email.
        # A stopped scheduler must never cause follow-ups to go out together.
        latest = from_iso(thread.last_followup_at_utc)
        if latest is not None:
            previous_day = plan[index - 1]["day"] if index else 0
            scheduled = max(scheduled, latest + timedelta(days=step["day"] - previous_day))
        return scheduled
    base = from_iso(thread.last_followup_at_utc) or from_iso(thread.initial_sent_at_utc)
    if base is None:
        return None
    return base + timedelta(days=thread.followup_delay_days)


def is_due(thread: TrackedThread) -> bool:
    if thread.status != "waiting":
        return False
    if thread.followup_count >= thread.max_followups:
        return False
    due = next_due_at(thread)
    return due is not None and utc_now() >= due


def guess_name(recipient: str, recipient_name: str | None = None) -> str:
    if recipient_name and recipient_name.strip():
        return recipient_name.strip()
    name, _ = parseaddr(recipient)
    if name.strip():
        return name.strip().split()[0]
    # A mailbox local part is not a person's name (e.g. jsmith23 or admissions).
    return "there"


def render_template(template: str, *, thread: TrackedThread, sender_name: str, sender_email: str) -> str:
    name = guess_name(thread.recipient, thread.recipient_name)
    initial_sent = from_iso(thread.initial_sent_at_utc)
    last_email_dt = from_iso(thread.last_followup_at_utc) or initial_sent
    days_since_last_email = 0
    if last_email_dt:
        days_since_last_email = max(0, (utc_now() - last_email_dt).days)

    values = {
        "name": name,
        "recipient": thread.recipient,
        "recipient_name": thread.recipient_name or "",
        "subject": thread.subject,
        "sender_name": sender_name,
        "sender_email": sender_email,
        "followup_number": thread.followup_count + 1,
        "max_followups": thread.max_followups,
        "original_sent_date": initial_sent.strftime("%Y-%m-%d") if initial_sent else "",
        "days_since_last_email": days_since_last_email,
    }

    # Fail fast on missing fields. This is better than silently sending a broken template.
    fields = {field_name for _, field_name, _, _ in Formatter().parse(template) if field_name}
    missing = sorted(field for field in fields if field not in values)
    if missing:
        raise ValueError(f"Unknown template field(s): {', '.join(missing)}")

    return template.format(**values)
