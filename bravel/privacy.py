"""Best-effort secret redaction for terminal output, API context and saved chats."""
from __future__ import annotations

import re

MASK = "[СКРЫТО]"
PATTERNS = (
    r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{16,}|AIza[A-Za-z0-9_-]{30,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b",
    r"(?i)([\"']?\b(?:[A-Z0-9_]*(?:API_KEY|ACCESS_KEY|TOKEN|SECRET|PASSWORD|PASSWD)|authorization)\b[\"']?\s*[=:]\s*)(?:Bearer\s+)?(?:\"[^\"\n]+\"|'[^'\n]+'|[^\s,;]+)",
    r"(?i)(\bBearer\s+)[A-Za-z0-9_.+/=-]{8,}",
    r"(?i)(https?://[^\s/:]+:)[^\s/@]+(?=@)",
)


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    for secret in secrets:
        if secret and secret != "your-api-key":
            text = text.replace(secret, MASK)
    for pattern in PATTERNS:
        text = re.sub(pattern, lambda match: (match[1] if match.lastindex else "") + MASK, text)
    text = re.sub(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)",
                  MASK, text, flags=re.S)
    return text


def redact_data(value, secrets: tuple[str, ...] = ()):
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, list):
        return [redact_data(item, secrets) for item in value]
    if isinstance(value, tuple):
        return [redact_data(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: redact_data(item, secrets) for key, item in value.items()}
    return value
