"""ITIN, UK phone and Spain DNI rules must not flag ordinary digit runs.

Each of these rules used to match a bare digit (or digit + letter) shape with
no separator, cue or checksum, so order numbers, SAP document ids, PO numbers
and build stamps were reported as PII. The drop policy then 422s the write and
the mask policy rewrites the stored memory, so a false positive here corrupts
content that merely mentioned a reference number.

The positives below pin that the real shapes are still caught.
"""

import pytest

from common.governance import PIICategory, mask, scan

pytestmark = pytest.mark.unit


def _spans(text: str, category: PIICategory) -> list[str]:
    return [text[f.start : f.end] for f in scan(text) if f.category == category]


# ── US ITIN ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "order 987812345 shipped",
        "invoice 912783456 paid",
        "ref 900703333",
        "ticket 912-783456 closed",  # mismatched separators
    ],
)
def test_bare_or_mismatched_nine_digit_run_is_not_an_itin(text):
    assert _spans(text, PIICategory.NATIONAL_ID) == []


@pytest.mark.parametrize(
    "text,expected",
    [
        ("ITIN 912-78-3456 on the W-7", "912-78-3456"),
        ("taxpayer 912 78 3456 filed", "912 78 3456"),
        ("ITIN 912783456 on file", "912783456"),
        ("itin: 987812345", "987812345"),
    ],
)
def test_real_itin_shapes_are_still_detected(text, expected):
    assert _spans(text, PIICategory.NATIONAL_ID) == [expected]


def test_bare_itin_mask_keeps_the_cue_word():
    text = "ITIN 912783456 on file"
    assert mask(text, scan(text)) == "ITIN «ID» on file"


# ── UK phone ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "call 0123456789 now",
        "SAP order 0000123456 posted",
        "invoice 0012345678 due",
        "batch 012345678 queued",
        "record 0551234567 archived",
    ],
)
def test_zero_led_digit_run_is_not_a_phone(text):
    assert _spans(text, PIICategory.PHONE) == []


def test_order_number_is_not_rewritten_by_mask():
    text = "SAP order 0000123456 posted"
    assert mask(text, scan(text)) == text


@pytest.mark.parametrize(
    "text,expected",
    [
        ("call me on 07700 900123", "07700 900123"),
        ("ring 07700900123 after six", "07700900123"),
        ("office 020 7946 0958", "020 7946 0958"),
        ("Manchester desk 0161 496 0000", "0161 496 0000"),
        ("landline 01632 960123", "01632 960123"),
        ("freephone 0800 123 4567", "0800 123 4567"),
        ("tel: 01632960123", "01632960123"),
        ("phone 0207 9460958", "0207 9460958"),
    ],
)
def test_real_uk_numbers_are_still_detected(text, expected):
    assert _spans(text, PIICategory.PHONE) == [expected]


# ── Spain DNI / NIF ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "PO 12345678 B approved",
        "released build 20260930 T",
        "batch 20260930-B",
        "SKU 12345678K",
        "deadline 20261015 A reminder",
        "DNI 12345678A",  # wrong check letter (the right one is Z)
    ],
)
def test_digits_plus_letter_without_a_valid_check_letter_is_not_a_dni(text):
    assert _spans(text, PIICategory.NATIONAL_ID) == []


@pytest.mark.parametrize(
    "text,expected",
    [
        ("DNI 12345678Z issued in Madrid", "12345678Z"),
        ("NIF 12345678-Z", "12345678-Z"),
        ("titular 00000000T", "00000000T"),
        ("DNI 12345678 Z on the contract", "12345678 Z"),
        ("nif: 87654321 X", "87654321 X"),
    ],
)
def test_valid_dnis_are_still_detected(text, expected):
    assert _spans(text, PIICategory.NATIONAL_ID) == [expected]
