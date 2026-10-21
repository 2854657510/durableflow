"""日志脱敏。

原则：日志可以定位问题，但不能出现手机号、邮箱、API Key、Bearer Token。
"""

from __future__ import annotations

import re
from typing import Any

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
OPENAI_KEY_RE = re.compile(r"sk-[A-Za-z0-9_-]{8,}")
BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.IGNORECASE)
SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "passwd",
    "secret",
    "token",
}


def redact_text(value: str) -> str:
    value = OPENAI_KEY_RE.sub("[REDACTED_API_KEY]", value)
    value = BEARER_RE.sub(r"\1[REDACTED_TOKEN]", value)
    value = EMAIL_RE.sub("[REDACTED_EMAIL]", value)
    value = PHONE_RE.sub("[REDACTED_PHONE]", value)
    return value


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if str(key).lower() in SENSITIVE_KEYS:
                cleaned[key] = "[REDACTED]"
            else:
                cleaned[key] = redact_value(item)
        return cleaned
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    return value
