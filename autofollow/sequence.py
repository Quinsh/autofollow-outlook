"""Validated, per-email follow-up plans.

Days are offsets from the original message. Custom bodies are literal text,
not templates; braces and other punctuation are preserved exactly.
"""
from __future__ import annotations

import json
from typing import Any

from .content_safety import validate_content

MAX_FOLLOWUPS = 3
MAX_DAY = 3650
MAX_BODY_LENGTH = 50_000


def validate_plan(value: Any) -> list[dict]:
    if not isinstance(value, list) or len(value) > MAX_FOLLOWUPS:
        raise ValueError(f"Follow-up plan must be a list containing 0 to {MAX_FOLLOWUPS} messages")
    result = []
    previous_day = 0
    for number, step in enumerate(value, 1):
        if not isinstance(step, dict) or set(step) != {"day", "body"}:
            raise ValueError(f"Follow-up {number} must contain exactly day and body")
        day, body = step["day"], step["body"]
        if type(day) is not int or not 1 <= day <= MAX_DAY:
            raise ValueError(f"Follow-up {number}: day must be an integer from 1 to {MAX_DAY}")
        if day <= previous_day:
            raise ValueError("Each follow-up day must be later than the previous follow-up day")
        if not isinstance(body, str) or not body.strip() or len(body) > MAX_BODY_LENGTH or "\x00" in body:
            raise ValueError(f"Follow-up {number}: enter a nonempty message of at most {MAX_BODY_LENGTH} characters")
        validate_content(body, field=f"Follow-up {number}")
        result.append({"day": day, "body": body})
        previous_day = day
    return result


def parse_plan(text: str) -> list[dict]:
    try:
        value = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ValueError("Follow-up plan must be valid JSON") from exc
    return validate_plan(value)


def saved_plan(thread) -> list[dict] | None:
    raw = getattr(thread, "followup_plan", None)
    return None if raw is None else parse_plan(raw)


def progress(thread) -> tuple[int, int]:
    plan = saved_plan(thread)
    if plan is None:
        return thread.followup_count, thread.max_followups
    sent = thread.followup_count - thread.followup_plan_start_count
    if sent < 0 or sent > len(plan):
        raise ValueError("Stored follow-up progress is inconsistent; no follow-up will be sent")
    return sent, len(plan)


def pending_step(thread) -> dict | None:
    plan = saved_plan(thread)
    if plan is None:
        return None
    index, count = progress(thread)
    return plan[index] if index < count else None
