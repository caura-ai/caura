"""Deterministic PII / PCI / secret pattern library + scan/mask primitives.

Seeded from the 4 high-frequency patterns in the Skill-Factory Sentinel scanner
(``core_api.services.forge.sentinel_scan``) and extended to 60+ patterns across
emails, phones, payment cards (Luhn-validated), IBANs (mod-97-validated),
national IDs, and provider API keys / secrets (high-entropy-validated). The
validators are the whole point — a bare "13-19 digits" regex flags every order
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


def _uk_national_length_ok(value: str) -> bool:
    """A UK national number (leading 0 included) is 10 or 11 digits."""
    return sum(c.isdigit() for c in value) in (10, 11)


_DNI_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"


def _dni_letter_ok(value: str) -> bool:
    """Spanish DNI / NIF check letter: ``_DNI_LETTERS[number % 23]``."""
    digits = value[:8]
    return digits.isdigit() and value[-1].upper() == _DNI_LETTERS[int(digits) % 23]


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


# An identifier's pieces: words of two or more lower-case letters, each with at
# most one capital in front (``Player``, ``inventory``), or an upper-case run.
# Each repeat starts at a capital, so a lower-case run splits only one way and a
# failed match backtracks in linear time; ``(?:[A-Z]?[a-z]{2,})+`` says the same
# but is exponential on a long lower-case run, and this scans user content.
_IDENTIFIER_PIECE_RE = re.compile(r"[A-Z]?[a-z]{2,}(?:[A-Z][a-z]{2,})*|[A-Z]{2,}")
_IDENTIFIER_BREAK_RE = re.compile(r"[-_0-9]+")


def _reads_as_words(value: str) -> bool:
    """True when ``value`` is spelt in words, split at ``-``, ``_`` and digits:
    ``PlayerInventorySerializationHandler``, ``Hynix-Memory-Roadmap-2026-Plan``.
    """
    pieces = [p for p in _IDENTIFIER_BREAK_RE.split(value) if p]
    return bool(pieces) and all(_IDENTIFIER_PIECE_RE.fullmatch(p) for p in pieces)


def _token_body_ok(value: str) -> bool:
    """Gate for prefix rules whose prefix alone is too common to trust.

    A minted credential body (``secrets.token_urlsafe``, an HMAC digest in
    base64url, an OpenAI key) is mixed-case and high-entropy. What the same
    prefix also starts in ordinary text is a snake_case or kebab-case
    identifier — ``ca_certificate_bundle_path``,
    ``sk-hynix-memory-roadmap-2026`` — which is single-case. Requiring both
    cases, with the entropy floor on top, keeps those out. Digits are not
    required: a 43-char random base64url body has none about once in 1,500
    draws, while it lacks a case about once in ten billion.

    A PascalCase or Title-Case identifier has both cases and enough entropy,
    so a body that reads as words is set aside too (L-227). A random body
    reads as words far less often than it lacks a digit: over 2,000,000
    draws, once for a 43-char base64url body, and never for a 48-char base62
    one.
    """
    return (
        any(c.islower() for c in value)
        and any(c.isupper() for c in value)
        and _entropy_ok(value)
        and not _reads_as_words(value)
    )


def _openai_key_ok(value: str) -> bool:
    """Token-shape gate on the body after ``sk-``, as ``_caura_credential_ok``
    gates the body after its prefix: the prefix's own lower case would let an
    upper-case body pass the mixed-case test."""
    return _token_body_ok(value.removeprefix("sk-"))


# Every prefix Caura mints or still accepts on a credential — see the Caura
# rule in ``_RULES`` for what each one is.
_CAURA_CREDENTIAL_PREFIX = r"(?:mc(?:a|x|o|rk|ft)?|ca(?:rk|ft)?|(?:mc|ca)i_v\d+)_"
_CAURA_CREDENTIAL_PREFIX_RE = re.compile(_CAURA_CREDENTIAL_PREFIX)


def _caura_credential_ok(value: str) -> bool:
    """Token-shape gate on the body only. Gating the whole match would let the
    lowercase prefix count as a character class, so ``ca_SOME_LONG_CONSTANT``
    would pass on its upper-case body plus the prefix's ``ca``.
    """
    return _token_body_ok(_CAURA_CREDENTIAL_PREFIX_RE.sub("", value, count=1))


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
    # UK national numbers. The previous single rule, ``0\d{3,4}\s?\d{5,6}``,
    # made the space optional and accepted any second digit, so every
    # zero-led 9-11 digit run — order numbers, SAP document ids, zero-padded
    # keys — was a "phone" and the mask policy rewrote it. A UK number has no
    # checksum, so shape and context are the signals:
    #
    # 1. Separated groups ("01632 960123", "020 7946 0958", "0161 496 0000") —
    #    a real UK prefix (01/02/03/07/08), the separator a person types, and
    #    the 10-11 digit length of a UK national number.
    _Rule(
        PIICategory.PHONE,
        Severity.MEDIUM,
        _c(r"(?<!\d)0[12378]\d{1,3}[ -]\d{3,4}[ -]?\d{3,4}\b"),
        validator=_uk_national_length_ok,
    ),
    # 2. Unseparated mobile (07 + 9 digits, 11 in all) — distinctive enough to
    #    stand alone; landline-length runs are not.
    _Rule(PIICategory.PHONE, Severity.MEDIUM, _c(r"(?<!\d)07\d{9}\b")),
    # 3. Any other unseparated national number only next to a phone cue.
    #    ``group=1`` redacts just the digits and keeps the cue word.
    _Rule(
        PIICategory.PHONE,
        Severity.MEDIUM,
        _c(
            r"\b(?:tel|phone|telephone|mobile|mob|cell|landline)\b[^0-9\n]{0,12}"
            r"(0[12378]\d{8,9})\b",
            re.IGNORECASE,
        ),
        group=1,
    ),
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
    # US SSN — split into two rules (09/02 M-03). The single rule this replaces
    # was ``\d{3}[- ]?\d{2}[- ]?\d{4}``, whose two separators were OPTIONAL and
    # INDEPENDENT. That made it match three things an SSN is not:
    #
    #   * a bare nine-digit number — every invoice no., order id and pid
    #     ("Invoice 100234567", "pid 123456789");
    #   * ZIP+4 — "12345-6789" passes because the first separator is absent and
    #     the second present, a shape no SSN has;
    #   * any nine digits split 5/4 by a dash anywhere in running text.
    #
    # That is not a cosmetic over-match. The drop policy 422s a legitimate write
    # and the mask policy REWRITES STORED CONTENT, so a false positive here
    # silently corrupts a memory that merely mentioned an order number.
    #
    # There is no checksum for an SSN (unlike Luhn for cards or mod-97 for
    # IBAN), so format and context are the only signals available.
    #
    # 1. Separated form — distinctive enough to stand alone. The backreference
    #    ``\1`` requires the SAME separator in both positions, which is what
    #    kills ZIP+4 and 5/4 splits; a real SSN is written "123-45-6789" or
    #    "123 45 6789", never "12345-6789".
    _Rule(
        PIICategory.NATIONAL_ID,
        Severity.HIGH,
        _c(r"\b(?!000|666|9\d\d)\d{3}([- ])(?!00)\d{2}\1(?!0000)\d{4}\b"),
    ),
    # 2. Bare nine-digit form — matched ONLY next to a cue that says what the
    #    number is. Nine bare digits are genuinely ambiguous to a reader too,
    #    so requiring the cue is not a weakened rule, it is the only honest one.
    #    ``group=1`` redacts just the digits and leaves the cue word intact, so
    #    a masked memory still reads "SSN <redacted>" rather than losing the
    #    sentence — the same reason the ``key=<value>`` rules use it.
    _Rule(
        PIICategory.NATIONAL_ID,
        Severity.HIGH,
        _c(
            r"\b(?:ssn|s\.s\.n\.|social[\s-]?security(?:\s+(?:number|no\.?|#))?"
            r"|soc\.?\s?sec\.?)\b[^0-9\n]{0,16}"
            r"((?!000|666|9\d\d)\d{3}(?!00)\d{2}(?!0000)\d{4})\b",
            re.IGNORECASE,
        ),
        group=1,
    ),
    # UK National Insurance number
    _Rule(
        PIICategory.NATIONAL_ID,
        Severity.HIGH,
        _c(r"\b[ABCEGHJ-PRSTW-Z]{2}\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-D]\b"),
    ),
    # US ITIN (9xx-7x/8x-xxxx) — the same two-rule split as the SSN above.
    # With optional separators every bare nine-digit run starting 9 with a 7/8
    # in the fourth place ("order 987812345") was a HIGH national id.
    # 1. Separated form, matched separators.
    _Rule(
        PIICategory.NATIONAL_ID,
        Severity.HIGH,
        _c(r"\b9\d{2}([- ])[78]\d\1\d{4}\b"),
    ),
    # 2. Bare form only next to an ITIN cue; ``group=1`` keeps the cue word.
    _Rule(
        PIICategory.NATIONAL_ID,
        Severity.HIGH,
        _c(
            r"\b(?:itin|individual\s+taxpayer\s+identification(?:\s+number)?)\b"
            r"[^0-9\n]{0,16}(9\d{2}[78]\d{5})\b",
            re.IGNORECASE,
        ),
        group=1,
    ),
    # Spain DNI / NIF — the check letter is ``_DNI_LETTERS[number % 23]``, so
    # the validator rejects the 22 in 23 "8 digits + capital letter" strings
    # (PO numbers, build stamps, SKUs) that are not a DNI.
    # 1. Written as a DNI is written: "12345678Z" or "12345678-Z".
    _Rule(
        PIICategory.NATIONAL_ID,
        Severity.HIGH,
        _c(r"\b\d{8}-?[A-HJ-NP-TV-Z]\b"),
        validator=_dni_letter_ok,
    ),
    # 2. The space-separated form ("build 20260930 T") reads as a number and a
    #    word far more often than as a DNI, so it needs a DNI/NIF cue as well.
    _Rule(
        PIICategory.NATIONAL_ID,
        Severity.HIGH,
        _c(
            r"\b(?:dni|nif|d\.n\.i\.|n\.i\.f\.)(?!\w)[^0-9\n]{0,16}"
            r"(\d{8} [A-HJ-NP-TV-Z])\b",
            re.IGNORECASE,
        ),
        validator=_dni_letter_ok,
        group=1,
    ),
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
    ),  # OpenAI-style, legacy hyphen-less body
    # Current OpenAI keys carry a type segment and ``-`` / ``_`` in the body —
    # ``sk-proj-…``, ``sk-svcacct-…``, ``sk-admin-…`` — which the rule above
    # can never match (``proj`` is four alphanumerics, then a hyphen). Widened
    # here rather than there so the hyphen-less form keeps matching with no
    # gate, while the hyphenated form is token-shape-gated: ``sk-`` also starts
    # kebab-case words. An ``sk-ant-`` key matches both this and the Anthropic
    # rule over the same span; overlap resolution keeps one ``API_KEY`` finding.
    _Rule(
        PIICategory.API_KEY,
        Severity.HIGH,
        _c(r"\bsk-[0-9A-Za-z_\-]{20,}"),
        validator=_openai_key_ok,
    ),  # OpenAI project / service-account / admin
    # Caura's own credentials — every prefix the platform mints or still
    # accepts (see caura-enterprise ``common/credential_schemes.py``):
    # ``mc_`` / ``ca_`` API credentials, ``mca_`` / ``mcx_`` / ``mco_``
    # pre-unification spellings, ``mcrk_`` / ``cark_`` registration keys,
    # ``mcft_`` / ``caft_`` fleet join tokens, and ``mci_v<N>_`` /
    # ``cai_v<N>_`` install credentials. Bodies are ``token_urlsafe(32)`` or a
    # base64url HMAC-SHA256 digest — 43 chars either way — so 32 is a floor
    # that still catches a lightly truncated paste. Token-shape-gated because
    # ``ca_`` and ``mc_`` also start ordinary snake_case identifiers.
    _Rule(
        PIICategory.API_KEY,
        Severity.HIGH,
        _c(rf"\b{_CAURA_CREDENTIAL_PREFIX}[A-Za-z0-9_\-]{{32,}}"),
        validator=_caura_credential_ok,
    ),  # Caura
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
    #
    # Both key/value rules accept an optional closing quote between the name
    # and the separator, so the JSON spelling (``"api_key": "…"``) matches as
    # well as the ``.env`` / YAML ones; without it the name's closing ``"``
    # sat where ``[:=]`` was required and no JSON-quoted credential matched.
    _Rule(
        PIICategory.SECRET,
        Severity.HIGH,
        _c(
            r"(?i)aws_secret_access_key['\"]?\s*[:=]\s*['\"]?([A-Za-z0-9/+=]{40})['\"]?"
        ),
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
            r"client[_-]?secret|password|passwd|pwd)\b['\"]?\s*[:=]\s*['\"]?"
            r"([A-Za-z0-9+/_\-]{16,})['\"]?"
        ),
        validator=_entropy_ok,
        group=1,
    ),
    # The same, for environment-variable names: ``OPENAI_API_KEY=…``,
    # ``CAURA_API_KEY=…``, ``GITHUB_TOKEN=…``. The rule above cannot see the
    # cue inside them — ``_`` is a word character, so there is no ``\b``
    # before ``API_KEY``. Case-SENSITIVE and upper-case only, which is what
    # makes it an env-var name rather than any identifier ending in
    # ``_token`` (``next_page_token``, ``csrf_token`` in prose); the value
    # still has to pass the same entropy gate.
    _Rule(
        PIICategory.SECRET,
        Severity.HIGH,
        _c(
            r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_(?:API_?KEY|TOKEN|SECRET|SECRET_KEY|"
            r"ACCESS_KEY|PASSWORD|PASSWD)\b['\"]?\s*[:=]\s*['\"]?([A-Za-z0-9+/_\-]{16,})['\"]?"
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
