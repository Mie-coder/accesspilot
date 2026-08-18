"""Shared redaction helpers for material that may become durable state."""

from __future__ import annotations

import re


def redact_sensitive_content(content: str) -> str:
    """Hide the credential shapes already protected by the conversation layer."""

    redacted = re.sub(
        r"(?i)\b(?:sk|ds)-[a-z0-9_-]{8,}",
        "[已隐藏疑似密钥]",
        content,
    )
    redacted = re.sub(
        (
            r"(?i)(?:api[_ -]?key|client[_ -]?secret|access[_ -]?token|"
            r"refresh[_ -]?token|private[_ -]?key|password)\s*[:=]\s*\S+"
        ),
        "[已隐藏凭证]",
        redacted,
    )
    return re.sub(
        r"(?i)bearer\s+\S+",
        "[已隐藏 Bearer 凭证]",
        redacted,
    )
