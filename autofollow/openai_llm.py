"""Shared OpenAI transport and classification with the same policy as Ollama."""
import json
import os
import math
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import urllib.error
import urllib.request

from .db import VALID_CATEGORIES
from .local_llm import build_classification_prompt, enforce_priority_policy, OllamaError


class OpenAIError(RuntimeError):
    pass


def _retry_delay(headers):
    """Honor server cooldowns without accepting invalid/nonfinite header values."""
    for name, scale in (("retry-after-ms", .001), ("retry-after", 1)):
        value = headers.get(name) if headers else None
        if value is None:
            continue
        try:
            seconds = float(value) * scale
        except (TypeError, ValueError):
            try:
                seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                continue
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    return 0


def _request_json(request, report):
    for attempt in range(5):
        delay = 0
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            code = exc.code
            delay = _retry_delay(exc.headers)
            # Inspect only the machine-readable code; never log the API body or message.
            try:
                error = json.loads(exc.read(65536)).get("error", {})
                quota = error.get("code") in {"insufficient_quota", "billing_hard_limit_reached"}
            except (ValueError, AttributeError, TypeError):
                quota = False
            finally:
                exc.close()
            if quota:
                raise OpenAIError("OpenAI quota exhausted: check API billing/credits before resuming") from None
            retryable = code in {408, 409, 429} or code >= 500
            hints = {401: "Check OPENAI_API_KEY", 403: "Check project/model access",
                     429: "Rate limit reached"}
            message = f"OpenAI HTTP {code}: {hints.get(code, 'Request failed')}"
            if not retryable:
                raise OpenAIError(message) from None
        except OSError:
            message = "OpenAI connection failed"
        except ValueError:
            raise OpenAIError("OpenAI returned an invalid response") from None
        if attempt == 4 or delay > 60:
            suffix = f"; server requests a {delay:.0f}s cooldown" if delay > 60 else "; retries exhausted"
            raise OpenAIError(message + suffix + ". Try again later") from None
        delay = max(delay, 2 ** (attempt + 1) + random.uniform(0, 1))
        if report:
            report(f"{message}; retry {attempt + 1}/4 for the same email in {delay:.1f}s")
        time.sleep(delay)


def selected_provider(cfg) -> str:
    provider = cfg.llm_provider
    if provider == "auto":
        return "openai" if os.environ.get("OPENAI_API_KEY", "").strip() else "ollama"
    if provider not in {"openai", "ollama"}:
        raise ValueError("Unknown LLM provider")
    return provider


def selected_model(cfg) -> str:
    return cfg.openai_model if selected_provider(cfg) == "openai" else cfg.llm_model


_PROPERTIES = {
    **{key: {"type": "boolean"} for key in
       ("needs_action", "important", "very_important", "direct_contact", "bulk_message")},
    "category": {"type": "string", "enum": sorted(VALID_CATEGORIES)},
    **{key: {"type": ["string", "null"]} for key in
       ("deadline", "important_evidence", "personal_evidence")},
    "reason": {"type": "string"},
    "priority_basis": {"type": "string", "enum": ["required_action", "personal_opportunity", "none"]},
}


def request_response(payload: dict, report=None) -> dict:
    """Use the server's key and shared bounded retries; never expose API errors."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise OpenAIError("OpenAI mode requires OPENAI_API_KEY in the server environment")
    if not key.startswith("sk-") or any(char.isspace() for char in key):
        raise OpenAIError(
            "OPENAI_API_KEY is malformed: it must contain only the API key. "
            "A pasted multi-line read/export command can capture command text instead. "
            "Stop the server, run source .autofollow/openai.env, then start the server again"
        )
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses", data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    return _request_json(request, report)


def classify_email_with_openai(
    subject: str, sender: str, body: str, model: str = "gpt-4.1-mini",
    received_at: str = "unknown", timezone: str = "UTC",
    recipient_context: str = "", is_bulk: bool = False,
    report=None,
) -> dict:
    payload = {
        "model": model,
        "store": False,
        "input": build_classification_prompt(
            subject, sender, body, received_at, timezone, recipient_context, is_bulk,
        ),
        "max_output_tokens": 1200,
        "text": {"format": {
            "type": "json_schema", "name": "email_classification", "strict": True,
            "schema": {"type": "object", "properties": _PROPERTIES,
                       "required": list(_PROPERTIES), "additionalProperties": False},
        }},
    }
    data = request_response(payload, report)
    if not isinstance(data, dict) or data.get("status") != "completed":
        raise OpenAIError("OpenAI response did not complete; message remains pending")
    try:
        content = [part for item in data.get("output", []) if item.get("type") == "message"
                   for part in item.get("content", [])]
        if any(part.get("type") == "refusal" for part in content):
            raise OpenAIError("OpenAI declined classification; message remains pending")
        raw = "".join(part["text"] for part in content if part.get("type") == "output_text")
        result = json.loads(raw)
        return enforce_priority_policy(result, subject=subject, body=body[:6000], is_bulk=is_bulk)
    except (ValueError, TypeError, KeyError, AttributeError, OllamaError):
        raise OpenAIError("OpenAI returned an invalid classification; message remains pending") from None
