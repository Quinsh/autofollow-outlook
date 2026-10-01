"""Reject recognizable unfinished mail-merge fields without altering user text."""
import re

# Preserve ordinary brackets, mathematical notation and JSON. These tokens are
# recognizable address/signature/template fields, not arbitrary brace contents.
_FIELDS = r'(?:name|first[ _-]?name|last[ _-]?name|full[ _-]?name|recipient(?:[ _-]?name|[ _-]?email)?|professor(?:[ _-]?name)?|your[ _-]?(?:name|email)|sender(?:[ _-]?name|[ _-]?email)?|university(?:[ _-]?name)?|institution|company(?:[ _-]?name)?|subject|followup_number|max_followups|original_sent_date|days_since_last_email)'
_TOKEN = re.compile(r'(?:\[\s*' + _FIELDS + r'\s*\]|\{\{?\s*' + _FIELDS + r'\s*\}\}?|<\s*' + _FIELDS + r'\s*>|\[\s*(?:insert|enter|replace|add)\b[^\]\n]*\])', re.IGNORECASE)


def validate_content(text: str, *, field: str = 'Message') -> None:
    match = _TOKEN.search(text)
    if match:
        raise ValueError(f'{field} contains an unresolved placeholder {match.group(0)!r}. Replace it with final text before sending.')
