"""Local secret scrub — defense in depth before anything leaves the machine.

The server deterministically masks PII/secrets again on receipt
(``common.governance``); this pass exists so credential-shaped strings
never even transit. Patterns favor precision (long, prefixed token
shapes) over recall — the server's scanner is the broad net.
"""

from __future__ import annotations

import re

_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),  # OpenAI / Anthropic-style
    re.compile(r"mc[ai]?_[A-Za-z0-9_\-]{16,}"),  # MemClaw credentials
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub tokens (classic)
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),  # GitHub fine-grained PAT
    re.compile(
        r"(?i)\baws[_-]?secret[_-]?access[_-]?key\s*[=:]\s*['\"]?[A-Za-z0-9/+]{39,41}"
    ),  # AWS secret access key (AKIA covers only the key ID)
    re.compile(r"xox[a-z]-[A-Za-z0-9\-]{10,}"),  # Slack tokens
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    # Bounded span: an unmatched BEGIN scans at most 8192 chars instead of
    # to EOF, keeping K stray markers in a long transcript at O(K*8k) not
    # O(K*N). RSA-4096 PEM is ~3400 chars, so real keys fit comfortably;
    # a pathological >8k key falls through to the server-side mask.
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]{0,8192}?-----END [A-Z ]*PRIVATE KEY-----"
    ),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-\.=]{16,}"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\s*[=:]\s*['\"]?[A-Za-z0-9_\-\.]{20,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}\b"),  # JWT
]

REDACTED = "[REDACTED_SECRET]"


def scrub(text: str) -> str:
    """Replace credential-shaped substrings with a redaction token."""
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text
