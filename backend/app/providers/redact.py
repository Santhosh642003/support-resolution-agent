"""Redaction for anything that leaves the adapter: logs, exception text, the llm_calls table.

Provider error bodies are ALWAYS captured and logged (an unexplained 400 is undiagnosable), but
never with key material or account identifiers in them. Redaction is by pattern plus the exact
secrets the caller knows about.
"""

import re

_PATTERNS = (
    re.compile(r"gsk_[A-Za-z0-9_\-]{8,}"),  # Groq API keys
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),  # Google API keys
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),  # OpenAI-style keys
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"org_[A-Za-z0-9]+"),  # provider organisation identifiers
)


def redact(text: str, *secrets: str) -> str:
    out = text
    for secret in secrets:
        if secret:
            out = out.replace(secret, "[REDACTED]")
    for pattern in _PATTERNS:
        out = pattern.sub("[REDACTED]", out)
    return out
