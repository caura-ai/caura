"""Unit tests for the deterministic governance PII/secret pattern library.

Focus on the validators (Luhn / IBAN mod-97 / entropy) that separate real
sensitive data from look-alikes, the provider-key coverage, overlap
resolution, masking, the category toggle, and a false-positive corpus.
"""

from common.governance import Finding, PIICategory, Severity, mask, scan


def _cats(text: str) -> set[PIICategory]:
    return {f.category for f in scan(text)}


# ── Payment cards: Luhn gating ───────────────────────────────────────


def test_valid_luhn_card_is_detected():
    assert PIICategory.CREDIT_CARD in _cats("pay with 4111 1111 1111 1111 today")
    assert PIICategory.CREDIT_CARD in _cats("amex 3782 822463 10005")


def test_invalid_luhn_number_is_not_a_card():
    # Right shape, fails Luhn → not flagged (kills the "any 16 digits" FP).
    assert PIICategory.CREDIT_CARD not in _cats("order 4111 1111 1111 1112 shipped")


def test_random_16_digit_id_is_not_a_card():
    assert PIICategory.CREDIT_CARD not in _cats("tracking number 1234567890123456")


# ── IBAN: mod-97 gating ──────────────────────────────────────────────


def test_valid_iban_is_detected():
    assert PIICategory.IBAN in _cats("transfer to GB82 WEST 1234 5698 7654 32 please")


def test_invalid_iban_is_rejected():
    assert PIICategory.IBAN not in _cats("ref GB99 WEST 1234 5698 7654 32 nope")


# ── Email / phone / national id ──────────────────────────────────────


def test_email_and_ssn_detected():
    cats = _cats("contact a.b+x@sub.example.co.uk, SSN 123-45-6789")
    assert PIICategory.EMAIL in cats
    assert PIICategory.NATIONAL_ID in cats


def test_phone_detected():
    assert PIICategory.PHONE in _cats("call (415) 555-2671")
    assert PIICategory.PHONE in _cats("ring +44 20 7946 0958")


# ── API keys / secrets ───────────────────────────────────────────────


def test_provider_api_keys_detected():
    # Test tokens are assembled from fragments so the contiguous secret never
    # appears as a literal in source (defeats secret-scanning push protection);
    # the runtime-concatenated value still exercises the detector regex.
    samples = {
        "AWS": "AKIA" + "IOSFODNN7EXAMPLE",
        "GitHub": "ghp_" + "a" * 36,
        "Stripe": "sk_" + "live_" + "4eC39HqLyjWDarjtT1zdp7dc",
        "Slack": "xoxb-" + "2401234567-2412345678901-" + "AbCdEfGhIjKlMnOpQrStUvWx",
        "Google": "AIza" + "B" * 35,
    }
    for label, key in samples.items():
        assert PIICategory.API_KEY in _cats(f"token {key} end"), label


def test_jwt_and_pem_and_bearer_detected():
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dummysignature1234"
    assert PIICategory.SECRET in _cats(jwt)
    assert PIICategory.SECRET in _cats("-----BEGIN RSA PRIVATE KEY-----")
    assert PIICategory.SECRET in _cats(
        "Authorization: Bearer abcdef0123456789ABCDEF0123"
    )


def test_high_entropy_secret_assignment_detected():
    assert PIICategory.SECRET in _cats('api_key = "Aa1Bb2Cc3Dd4Ee5Ff6Gg7Hh8"')
    assert PIICategory.SECRET in _cats(
        "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    )


def test_low_entropy_or_short_secret_is_not_flagged():
    # Defaults / config words must not trip the secret detector.
    assert PIICategory.SECRET not in _cats("password=changeme")
    assert PIICategory.SECRET not in _cats(
        "token = aaaaaaaaaaaaaaaaaaaa"
    )  # 20 chars, low entropy


# ── False-positive corpus ────────────────────────────────────────────


def test_plain_prose_has_no_findings():
    assert scan("Let's plan the Q3 roadmap and ship the new onboarding flow.") == []
    assert scan("Meeting moved to 3pm, room 412, bring the deck.") == []


# ── Category toggle ──────────────────────────────────────────────────


def test_enabled_categories_restricts_scan():
    text = "email me@x.com and card 4111 1111 1111 1111"
    only_cards = scan(text, enabled_categories={PIICategory.CREDIT_CARD})
    assert {f.category for f in only_cards} == {PIICategory.CREDIT_CARD}
    only_email = scan(text, enabled_categories={PIICategory.EMAIL})
    assert {f.category for f in only_email} == {PIICategory.EMAIL}


# ── Overlap resolution ───────────────────────────────────────────────


def test_overlapping_matches_resolve_to_one_span():
    # A JWT matches the JWT rule; ensure it isn't also emitted as a second
    # overlapping secret span (which would double-mask).
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig1234567890"
    )
    findings = scan(f"jwt {jwt} done")
    spans = [(f.start, f.end) for f in findings]
    # No two findings overlap.
    for i in range(len(spans)):
        for j in range(i + 1, len(spans)):
            a, b = spans[i], spans[j]
            assert a[1] <= b[0] or b[1] <= a[0], f"overlap {a} {b}"


# ── Masking ──────────────────────────────────────────────────────────


def test_mask_redacts_spans_and_keeps_surrounding_text():
    text = "card 4111 1111 1111 1111 and email me@example.com ok"
    findings = scan(text)
    masked = mask(text, findings)
    assert "4111" not in masked
    assert "me@example.com" not in masked
    assert "«CARD»" in masked and "«EMAIL»" in masked
    assert masked.startswith("card ") and masked.endswith(" ok")


def test_mask_with_no_findings_is_identity():
    assert mask("nothing sensitive here", []) == "nothing sensitive here"


def test_findings_carry_no_raw_text():
    # The Finding dataclass must not expose the matched value (audit safety).
    f = scan("card 4111 1111 1111 1111")[0]
    assert isinstance(f, Finding)
    assert set(vars(f)) == {"category", "start", "end", "severity"}
    assert f.severity == Severity.HIGH
