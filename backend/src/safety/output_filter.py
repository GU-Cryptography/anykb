"""L3 — output filter. Redact PII patterns."""
from __future__ import annotations

import re

PHONE = re.compile(r"\b1[3-9]\d{9}\b")
ID_CARD = re.compile(r"\b\d{17}[\dXx]\b")


def redact_pii(text: str) -> str:
    text = PHONE.sub("[手机号已隐藏]", text)
    text = ID_CARD.sub("[身份证已隐藏]", text)
    return text
