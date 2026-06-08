"""Prompt-injection sanitization shared across services.

Memory content and titles are agent-controlled input. Before interpolating
them into LLM prompts, strip common hijack phrases ("ignore previous",
"system:", ChatML delimiters, role prefixes, "[INST]"/"[/INST]") and bound
length to a reasonable per-field cap.
"""

import re

INJECTION_PATTERN = re.compile(
    r"(?i)(?:"
    r"\b(?:ignore\s+(?:previous|all|above|prior)|system\s*:)"
    r"|\[/?inst"
    r"|<\|(?:system|user|assistant|im_start|im_end)\|>"
    r"|\b(?:human|assistant)\s*:"
    r")",
)


def sanitize_content(value: str, max_len: int = 500) -> str:
    """Truncate, strip newlines, and redact common prompt-injection phrases."""
    if not value:
        return ""
    value = value[:max_len].replace("\n", " ").replace("\r", " ")
    return INJECTION_PATTERN.sub("[…]", value)
