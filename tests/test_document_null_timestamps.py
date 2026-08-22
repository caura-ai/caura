"""A document row with NULL timestamps must serialise, not 500.

`documents.created_at` / `updated_at` carry `server_default=now()` but were never
declared `nullable=False` (001_initial_schema, never tightened), so a NULL is
representable. `DocOut` required a `datetime`, and `_dict_to_out` guarded with
`d.get(key, datetime.min)` — which only substitutes when the KEY IS ABSENT. For the
case the schema actually permits, key present with value `None`, the default never
fired and pydantic raised inside the read route.

The sentinel was also wrong on its own terms where it did fire: `datetime.min` is
year 1 and naive, in a timezone-aware field, handed to a caller with no way to
recognise it as anything but a real timestamp.
"""

from __future__ import annotations

import pytest

from core_api.routes.documents import _dict_to_out

pytestmark = [pytest.mark.unit]


def _row(**over) -> dict:
    """A storage row as the documents endpoints receive it."""
    base = {
        "id": "11111111-1111-1111-1111-111111111111",
        "tenant_id": "t1",
        "fleet_id": None,
        "collection": "invoices",
        "doc_id": "inv-1",
        "data": {"total": 42},
        "created_at": "2026-08-18T10:00:00Z",
        "updated_at": "2026-08-18T10:00:00Z",
    }
    base.update(over)
    return base


def test_a_null_created_at_serialises_as_null() -> None:
    """The case the old guard missed: key present, value None."""
    out = _dict_to_out(_row(created_at=None))

    assert out.created_at is None
    assert out.updated_at is not None, "only the null field becomes null"
    assert out.data == {"total": 42}, "the rest of the document still round-trips"


def test_both_timestamps_null_serialises() -> None:
    out = _dict_to_out(_row(created_at=None, updated_at=None))

    assert out.created_at is None and out.updated_at is None


def test_a_missing_key_is_null_not_year_one() -> None:
    """The case the old guard DID cover, but covered wrongly.

    ``datetime.min`` sorts before every real row and renders as 0001-01-01, which a
    client cannot distinguish from a genuine timestamp.
    """
    row = _row()
    del row["created_at"]

    out = _dict_to_out(row)

    assert out.created_at is None, f"expected null, not a sentinel date; got {out.created_at!r}"


def test_the_null_case_is_logged_with_identifiers_only(caplog) -> None:
    """It should be findable — a NULL here means a row written outside the normal
    path. And the log must not carry ``data``, which is caller-supplied content.
    """
    import logging

    with caplog.at_level(logging.WARNING, logger="core_api.routes.documents"):
        _dict_to_out(_row(created_at=None, data={"secret": "shh"}))

    lines = [r.getMessage() for r in caplog.records if "document_timestamp_null" in r.getMessage()]
    assert len(lines) == 1, f"expected one warning; got {lines}"
    assert "11111111-1111-1111-1111-111111111111" in lines[0]
    assert "secret" not in lines[0] and "shh" not in lines[0], (
        f"document data must never reach log storage; got {lines[0]!r}"
    )


def test_a_normal_row_is_untouched_and_silent(caplog) -> None:
    """Control: the fix must not fire on the overwhelmingly common case."""
    import logging

    with caplog.at_level(logging.WARNING, logger="core_api.routes.documents"):
        out = _dict_to_out(_row())

    assert out.created_at is not None and out.updated_at is not None
    assert not [r for r in caplog.records if "document_timestamp_null" in r.getMessage()]
