"""Deterministic PII / PCI / secret pattern library + scan/mask primitives.

Seeded from the 4 high-frequency patterns in the Skill-Factory Sentinel scanner
(``core_api.services.forge.sentinel_scan``) and extended to 60+ patterns across
emails, phones, payment cards (Luhn-validated), IBANs (mod-97-validated),
national IDs, and provider API keys / secrets (high-entropy-validated). The
validators are the whole point — a bare "13–19 digits" regex flags every order
number; gating it on the Luhn checksum cuts the false-positive rate hard.

``scan`` returns :class:`Finding` objects that carry only category + offsets +
severity — **never the matched text** — so audit details built from them can't
leak the very secret they record. ``mask`` redacts the found spans in place.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum


class PIICategory(str, Enum):
    EMAIL = "email"
    PHONE = "phone"
    CREDIT_CARD = "credit_card"
    IBAN = "iban"
    NATIONAL_ID = "national_id"
    API_KEY = "api_key"
    SECRET = "secret"


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# Categories whose presence under detection-uncertainty should trigger the
# fail-closed (safe) action — PCI, credentials and national IDs are the
# high-blast-radius leaks.
HIGH_RISK_CATEGORIES: frozenset[PIICategory] = frozenset(
    {
        PIICategory.CREDIT_CARD,
        PIICategory.IBAN,
        PIICategory.NATIONAL_ID,
        PIICategory.API_KEY,
        PIICategory.SECRET,
    }
)


@dataclass(frozen=True)
class Finding:
    """One detected sensitive span. Carries NO raw text — only the category,
    character offsets ``[start, end)`` and severity — so callers (audit) can
    record *that* something was found without storing the secret itself.
    """

    category: PIICategory
    start: int
    end: int
    severity: Severity


# ── Validators (cut false positives on high-risk shapes) ─────────────


def _luhn_ok(value: str) -> bool:
    """Luhn checksum over the digits in ``value`` (payment-card check)."""
    digits = [int(c) for c in value if c.isdigit()]
    if len(digits) < 13:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


_IBAN_STRIP = re.compile(r"[\s]")


def _iban_mod97_ok(value: str) -> bool:
    """ISO 13616 mod-97 check: move the first 4 chars to the end, map letters
    to numbers (A=10..Z=35), and require the integer ≡ 1 (mod 97).
    """
    s = _IBAN_STRIP.sub("", value).upper()
    if len(s) < 15 or len(s) > 34:
        return False
    rearranged = s[4:] + s[:4]
    digits = []
    for ch in rearranged:
        if ch.isdigit():
            digits.append(ch)
        elif "A" <= ch <= "Z":
            digits.append(str(ord(ch) - 55))
        else:
            return False
    return int("".join(digits)) % 97 == 1


def _shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = {c: value.count(c) for c in set(value)}
    n = len(value)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _entropy_ok(value: str) -> bool:
    """High-entropy gate for the generic ``secret=<value>`` detector — a real
    credential is long and random; a config word like ``password=changeme`` is
    short and low-entropy and should NOT trip the secret detector.
    """
    return len(value) >= 16 and _shannon_entropy(value) >= 3.0


# ── Pattern rules ────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Rule:
    category: PIICategory
    severity: Severity
    pattern: re.Pattern[str]
    validator: Callable[[str], bool] | None = None
    # Which regex group holds the sensitive span (and is fed to the
    # validator). 0 = whole match; >0 lets a ``key=<value>`` rule redact only
    # the value, not the key name.
    group: int = 0


def _c(pattern: str, flags: int = 0) -> re.Pattern[str]:
    return re.compile(pattern, flags)


_RULES: tuple[_Rule, ...] = (
    # ── Email (LOW) ──
    _Rule(
        PIICategory.EMAIL,
        Severity.LOW,
        _c(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ),
    # ── Phone (MEDIUM) — bounded forms to limit false positives ──
    # E.164 / international (+CC then 7-14 digits with optional separators)
    _Rule(
        PIICategory.PHONE,
        Severity.MEDIUM,
        _c(r"\+\d{1,3}[\s.-]?(?:\(?\d{1,4}\)?[\s.-]?){2,5}\d{2,4}"),
    ),
    # North American: (NNN) NNN-NNNN or NNN-NNN-NNNN. A lookbehind (not \b)
    # because a leading "(" is itself a non-word char — \b would never anchor
    # before it; (?<!\d) just rules out matching mid-digit-run.
    _Rule(
        PIICategory.PHONE,
        Severity.MEDIUM,
        _c(r"(?<!\d)(?:\(\d{3}\)\s?|\d{3}[-.\s])\d{3}[-.\s]\d{4}\b"),
    ),
    # UK mobile / national 07xxx xxxxxx
    _Rule(PIICategory.PHONE, Severity.MEDIUM, _c(r"\b0\d{3,4}\s?\d{5,6}\b")),
    # ── Payment cards (HIGH, Luhn-gated) ──
    # Visa / MC / Amex / Discover / 2-series / JCB / Diners. A 4-digit issuer
    # prefix then 9-15 more digits (total 13-19), separator-agnostic so the
    # Amex 4-6-5 grouping matches as well as the common 4-4-4-4; the Luhn
    # validator is what actually confirms it's a card.
    _Rule(
        PIICategory.CREDIT_CARD,
        Severity.HIGH,
        _c(
            r"\b(?:4\d{3}|5[1-5]\d{2}|2[2-7]\d{2}|3[47]\d{2}|6(?:011|5\d{2})|3(?:0[0-5]|[68]\d)\d)(?:[-\s]?\d){9,15}\b"
        ),
        validator=_luhn_ok,
    ),
    # ── IBAN (HIGH, mod-97-gated) ──
    _Rule(
        PIICategory.IBAN,
        Severity.HIGH,
        _c(r"\b[A-Z]{2}\d{2}(?:[\s]?[A-Z0-9]{4}){2,7}(?:[\s]?[A-Z0-9]{1,3})?\b"),
        validator=_iban_mod97_ok,
    ),
    # ── National IDs (HIGH) ──
    # US SSN (seed from Sentinel) — excludes the known-invalid ranges.
    _Rule(
        PIICategory.NATIONAL_ID,
        Severity.HIGH,
        _c(r"\b(?!000|666|9\d\d)\d{3}[- ]?(?!00)\d{2}[- ]?(?!0000)\d{4}\b"),
    ),
    # UK National Insurance number
    _Rule(
        PIICategory.NATIONAL_ID,
        Severity.HIGH,
        _c(r"\b[ABCEGHJ-PRSTW-Z]{2}\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-D]\b"),
    ),
    # US ITIN (9xx-7x/8x-xxxx)
    _Rule(
        PIICategory.NATIONAL_ID, Severity.HIGH, _c(r"\b9\d{2}[- ]?[78]\d[- ]?\d{4}\b")
    ),
    # Spain DNI / NIF
    _Rule(PIICategory.NATIONAL_ID, Severity.HIGH, _c(r"\b\d{8}[- ]?[A-HJ-NP-TV-Z]\b")),
    # ── API keys (HIGH) — provider-specific prefixes ──
    _Rule(
        PIICategory.API_KEY,
        Severity.HIGH,
        _c(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[0-9A-Z]{16}\b"),
    ),  # AWS access key id
    _Rule(
        PIICategory.API_KEY, Severity.HIGH, _c(r"\bAIza[0-9A-Za-z_\-]{35}\b")
    ),  # Google API key
    _Rule(
        PIICategory.API_KEY, Severity.HIGH, _c(r"\bya29\.[0-9A-Za-z_\-]+")
    ),  # Google OAuth access token
    _Rule(
        PIICategory.API_KEY, Severity.HIGH, _c(r"\bgh[pousr]_[0-9A-Za-z]{36}\b")
    ),  # GitHub token
    _Rule(
        PIICategory.API_KEY, Severity.HIGH, _c(r"\bgithub_pat_[0-9A-Za-z_]{82}\b")
    ),  # GitHub fine-grained PAT
    _Rule(
        PIICategory.API_KEY, Severity.HIGH, _c(r"\bglpat-[0-9A-Za-z_\-]{20}\b")
    ),  # GitLab PAT
    _Rule(
        PIICategory.API_KEY, Severity.HIGH, _c(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")
    ),  # Slack token
    _Rule(
        PIICategory.API_KEY, Severity.HIGH, _c(r"\bxapp-\d-[0-9A-Za-z-]{20,}\b")
    ),  # Slack app-level token
    _Rule(
        PIICategory.API_KEY,
        Severity.HIGH,
        _c(r"\b(?:sk|rk|pk)_(?:live|test)_[0-9A-Za-z]{16,}\b"),
    ),  # Stripe
    _Rule(
        PIICategory.API_KEY, Severity.HIGH, _c(r"\bsk-ant-[0-9A-Za-z_\-]{20,}\b")
    ),  # Anthropic
    _Rule(
        PIICategory.API_KEY, Severity.HIGH, _c(r"\bsk-[0-9A-Za-z]{20,}\b")
    ),  # OpenAI-style
    _Rule(
        PIICategory.API_KEY,
        Severity.HIGH,
        _c(r"\bSG\.[0-9A-Za-z_\-]{22}\.[0-9A-Za-z_\-]{43}\b"),
    ),  # SendGrid
    _Rule(
        PIICategory.API_KEY, Severity.HIGH, _c(r"\bSK[0-9a-fA-F]{32}\b")
    ),  # Twilio key SID
    _Rule(
        PIICategory.API_KEY, Severity.HIGH, _c(r"\bAC[0-9a-fA-F]{32}\b")
    ),  # Twilio account SID
    _Rule(
        PIICategory.API_KEY, Severity.HIGH, _c(r"\bnpm_[0-9A-Za-z]{36}\b")
    ),  # npm token
    _Rule(
        PIICategory.API_KEY, Severity.HIGH, _c(r"\bdop_v1_[0-9a-f]{64}\b")
    ),  # DigitalOcean PAT
    _Rule(
        PIICategory.API_KEY, Severity.HIGH, _c(r"\bpypi-[0-9A-Za-z_\-]{16,}\b")
    ),  # PyPI token
    _Rule(
        PIICategory.API_KEY,
        Severity.HIGH,
        _c(r"\bsq0(?:atp|csp)-[0-9A-Za-z_\-]{22,}\b"),
    ),  # Square
    _Rule(
        PIICategory.API_KEY,
        Severity.HIGH,
        _c(r"\bshp(?:at|ss|pa|ca)_[0-9a-fA-F]{32}\b"),
    ),  # Shopify
    _Rule(
        PIICategory.API_KEY, Severity.HIGH, _c(r"\bkey-[0-9a-zA-Z]{32}\b")
    ),  # Mailgun
    _Rule(
        PIICategory.API_KEY, Severity.HIGH, _c(r"\b\d{8,10}:AA[0-9A-Za-z_\-]{33}\b")
    ),  # Telegram bot token
    _Rule(
        PIICategory.API_KEY, Severity.HIGH, _c(r"\bEAACEdEose0cBA[0-9A-Za-z]+")
    ),  # Facebook access token
    # ── Secrets (HIGH) ──
    _Rule(
        PIICategory.SECRET,
        Severity.HIGH,
        _c(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
    ),  # PEM
    _Rule(
        PIICategory.SECRET,
        Severity.HIGH,
        _c(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"),
    ),  # JWT
    _Rule(
        PIICategory.SECRET,
        Severity.HIGH,
        _c(r"\bBearer\s+[A-Za-z0-9_\-.=]{20,}", re.IGNORECASE),
    ),  # Bearer token
    # AWS secret access key in an assignment context (40-char base64); gated
    # by entropy so a 40-char path/sentence doesn't trip it. Group 1 = value.
    _Rule(
        PIICategory.SECRET,
        Severity.HIGH,
        _c(r"(?i)aws_secret_access_key\s*[:=]\s*['\"]?([A-Za-z0-9/+=]{40})['\"]?"),
        validator=_entropy_ok,
        group=1,
    ),
    # Generic ``secret/token/password/api_key = <high-entropy value>``. Group 1
    # = value so masking redacts the credential, not the field name.
    _Rule(
        PIICategory.SECRET,
        Severity.HIGH,
        _c(
            r"(?i)\b(?:secret|token|api[_-]?key|access[_-]?token|auth[_-]?token|"
            r"client[_-]?secret|password|passwd|pwd)\b\s*[:=]\s*['\"]?([A-Za-z0-9+/_\-]{16,})['\"]?"
        ),
        validator=_entropy_ok,
        group=1,
    ),
)


_REDACTION: dict[PIICategory, str] = {
    PIICategory.EMAIL: "«EMAIL»",
    PIICategory.PHONE: "«PHONE»",
    PIICategory.CREDIT_CARD: "«CARD»",
    PIICategory.IBAN: "«IBAN»",
    PIICategory.NATIONAL_ID: "«ID»",
    PIICategory.API_KEY: "«API_KEY»",
    PIICategory.SECRET: "«SECRET»",
}

# Severity ranking for overlap resolution (prefer the higher-risk finding when
# two spans collide — e.g. a JWT also matching the generic secret rule).
_SEVERITY_RANK: dict[Severity, int] = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
}


def scan(
    text: str, *, enabled_categories: Iterable[PIICategory] | None = None
) -> list[Finding]:
    """Find sensitive spans in ``text``.

    ``enabled_categories`` (the per-tenant config toggle) restricts which
    categories are scanned; ``None`` means all. Validator-gated rules
    (cards/IBANs/entropy secrets) only yield a finding when the checksum /
    entropy test passes. Overlapping findings are resolved to one span each.
    """
    if not text:
        return []
    allowed = frozenset(enabled_categories) if enabled_categories is not None else None
    findings: list[Finding] = []
    for rule in _RULES:
        if allowed is not None and rule.category not in allowed:
            continue
        for m in rule.pattern.finditer(text):
            value = m.group(rule.group)
            if rule.validator is not None and not rule.validator(value):
                continue
            start, end = m.span(rule.group)
            if end > start:
                findings.append(Finding(rule.category, start, end, rule.severity))
    return _resolve_overlaps(findings)


def _resolve_overlaps(findings: list[Finding]) -> list[Finding]:
    """Drop overlapping spans, keeping the longer (then higher-severity) one.

    Without this, a JWT/Bearer or ``key=<value>`` can match several rules and
    mask() would splice the same region twice. Sorting by start, then by a
    "stronger first" key, lets a single greedy pass keep the best per region.
    """
    if len(findings) <= 1:
        return findings
    ordered = sorted(
        findings,
        key=lambda f: (f.start, -(f.end - f.start), -_SEVERITY_RANK[f.severity]),
    )
    kept: list[Finding] = []
    last_end = -1
    for f in ordered:
        if f.start >= last_end:  # disjoint from everything kept so far
            kept.append(f)
            last_end = f.end
        # else: overlaps an already-kept (stronger) finding → drop it
    return kept


def mask(text: str, findings: list[Finding]) -> str:
    """Redact each finding's span with its category token, keeping the rest.

    Splices right-to-left so earlier offsets stay valid as later ones are
    replaced. Assumes non-overlapping findings (as :func:`scan` returns).
    """
    if not findings:
        return text
    out = text
    for f in sorted(findings, key=lambda f: f.start, reverse=True):
        out = out[: f.start] + _REDACTION[f.category] + out[f.end :]
    return out
