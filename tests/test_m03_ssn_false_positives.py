r"""09/02 M-03 — the US SSN rule matched things that are not SSNs.

The rule was ``\d{3}[- ]?\d{2}[- ]?\d{4}``. Both separators were OPTIONAL and,
crucially, INDEPENDENT of each other, so it matched:

* a bare nine-digit number — every invoice no., order id and pid;
* ZIP+4 (``12345-6789``), where the first separator is absent and the second
  present — a shape no SSN has;
* any nine digits split 5/4 by a dash in running text.

This is not a cosmetic over-match. The drop policy 422s a legitimate write and
the mask policy REWRITES STORED CONTENT, so a false positive silently corrupts
a memory that merely mentioned an order number — and the corruption is
unrecoverable, because the original text is gone.

An SSN has no checksum (unlike Luhn for cards, mod-97 for IBAN), so format and
context are the only signals available. Hence two rules: a separated form with
matched separators, and a bare form that requires a cue word.
"""

import pytest

from common.governance.pii_patterns import _RULES, PIICategory

pytestmark = pytest.mark.unit

_NATIONAL_ID = [r for r in _RULES if r.category == PIICategory.NATIONAL_ID]


def _matches(text: str) -> list[str]:
    """Every NATIONAL_ID span found in ``text``, as the redactor would see it.

    Uses ``rule.group`` rather than ``group(0)`` because the bare-form rule
    captures only the digits — the redacted span must not swallow the cue word.
    """
    out = []
    for rule in _NATIONAL_ID:
        for m in rule.pattern.finditer(text):
            if rule.validator is None or rule.validator(m.group(rule.group)):
                out.append(m.group(rule.group))
    return out


# ── what must still be caught ────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("SSN 123-45-6789", "123-45-6789"),
        ("123 45 6789", "123 45 6789"),
        ("employee ssn 456-78-9012 on file", "456-78-9012"),
    ],
)
def test_the_separated_form_is_still_caught(text, expected):
    """The distinctive shape stands on its own — no cue needed."""
    assert expected in _matches(text)


@pytest.mark.parametrize(
    "text",
    [
        "SSN 123456789",
        "ssn: 123456789",
        "SSN# 123456789",
        "Social Security Number 123456789",
        "social-security 123456789",
        "soc sec 123456789",
    ],
)
def test_the_bare_form_is_caught_when_a_cue_names_it(text):
    """Nine bare digits are ambiguous to a reader too. The cue is what makes
    the claim honest, so requiring it is not a weakened rule."""
    assert "123456789" in _matches(text)


def test_the_cue_word_itself_is_not_redacted():
    """``group=1`` exists so a masked memory still reads "SSN <redacted>"
    instead of losing the sentence that gave it meaning."""
    spans = _matches("SSN 123456789")
    assert spans == ["123456789"]
    assert not any("SSN" in s for s in spans)


# ── what must no longer be caught ────────────────────────────────────────


@pytest.mark.parametrize(
    "text,why",
    [
        ("12345-6789", "ZIP+4 — separator in a position no SSN uses"),
        ("Seattle WA 98109-1234", "ZIP+4 in an address"),
        ("123-456789", "mismatched separators"),
        ("12345 6789", "5/4 split on a space"),
    ],
)
def test_split_five_four_is_not_an_ssn(text, why):
    """The backreference requires the SAME separator in both positions."""
    assert _matches(text) == [], why


@pytest.mark.parametrize(
    "text,why",
    [
        ("Invoice 100234567 paid", "invoice number"),
        ("order 555123456 shipped", "order id"),
        ("port 8080 pid 123456789", "process id"),
        ("build 202409158 green", "build number"),
        ("tracking 123456789 delivered", "tracking number"),
    ],
)
def test_a_bare_nine_digit_number_without_a_cue_is_left_alone(text, why):
    """The corrupting case. Under the mask policy each of these previously had
    its number rewritten in the STORED memory."""
    assert _matches(text) == [], why


# ── invalid ranges still excluded (unchanged behaviour) ──────────────────


@pytest.mark.parametrize(
    "text", ["000-12-3456", "666-12-3456", "900-12-3456", "987-65-4321"]
)
def test_known_invalid_areas_are_still_excluded(text):
    """``9xx`` is the ITIN range, not an SSN area — it has its own rule below.
    Worth an explicit case: I first wrote ``987-65-4321`` as a *positive*
    example and the suite caught it."""
    assert _matches(text) == []


@pytest.mark.parametrize("text", ["123-00-6789", "123-45-0000"])
def test_known_invalid_groups_and_serials_are_still_excluded(text):
    assert _matches(text) == []


def test_both_rules_are_high_severity():
    """Splitting the rule must not have quietly downgraded either half."""
    from common.governance.pii_patterns import Severity

    assert len(_NATIONAL_ID) >= 2
    ssn_like = [
        r
        for r in _NATIONAL_ID
        if "6789" in r.pattern.pattern or "\\d{4}" in r.pattern.pattern
    ]
    assert all(r.severity is Severity.HIGH for r in ssn_like)
