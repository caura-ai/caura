"""``GET /memories/{memory_id}`` must keep emitting the bytes it always has.

Typing that route's 200 body (it was in ``test_broker_contract._UNTYPED_200``) moved
its serialisation from a hand-built ``JSONResponse`` onto a ``response_model``, and
two properties of the old output are easy to break in the process — neither of which
any other test would notice, because both produce perfectly valid JSON:

  1. **Key order.** FastAPI serialises in model-field order, not dict-insertion
     order, so reordering fields in ``MemoryDetailResponse`` silently reorders the
     response for every caller.
  2. **Timestamp format.** The values arrive from core-storage-api as ISO strings and
     are passed through. Declaring them ``datetime`` instead of ``str`` re-serialises
     them: pydantic v2 turns ``...+00:00`` into ``...Z``. Measured, and the reason
     they are ``str``. ``MemoryOut`` (the POST/PATCH response model) does type them
     as ``datetime`` and does emit ``Z``, so the two shapes genuinely differ today —
     aligning them is an API decision, and this test is what makes the drift
     deliberate rather than accidental.
"""

from __future__ import annotations

import re

import pytest

from core_api.schemas import MemoryDetailResponse
from tests.conftest import get_admin_headers, uid

# ``+00:00``, NOT ``Z`` — see the module docstring.
_ISO_OFFSET = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+00:00$")

# The 29 keys this endpoint emits, in the order it emits them — WRITTEN OUT, not
# derived from ``MemoryDetailResponse``. Deriving it would make the assertion
# tautological: FastAPI builds the response order FROM the model, so a comparison
# between the two moves together and passes for any reordering. Verified by
# reordering two fields and watching this fail. Changing this list is changing the
# response every caller receives.
_WIRE_KEY_ORDER = [
    "id",
    "tenant_id",
    "fleet_id",
    "agent_id",
    "agent_display_name",
    "memory_type",
    "title",
    "content",
    "weight",
    "source_uri",
    "run_id",
    "metadata",
    # C25 — platform-written view added 2026-08-25 (additive; a new key is a
    # deliberate wire change, which is exactly what this list exists to notice).
    "system_metadata",
    "content_hash",
    "created_at",
    "expires_at",
    "deleted_at",
    "subject_entity_id",
    "predicate",
    "object_value",
    "ts_valid_start",
    "ts_valid_end",
    "status",
    "visibility",
    "recall_count",
    "last_recalled_at",
    "supersedes_id",
    "entity_links",
    "embedding_preview",
    "embedding_stats",
]

# Every timestamp-valued field on the response.
_TIMESTAMP_FIELDS = (
    "created_at",
    "expires_at",
    "deleted_at",
    "ts_valid_start",
    "ts_valid_end",
    "last_recalled_at",
)


def test_the_model_field_order_is_the_wire_order() -> None:
    """No DB: catches a reorder at import time, before any request is made.

    Paired with the response-level assertion below. This one fails the moment
    someone reorders (or adds, or drops) a field on the model; that one fails if
    serialisation ever stops following field order. Neither implies the other.
    """
    assert list(MemoryDetailResponse.model_fields) == _WIRE_KEY_ORDER, (
        "MemoryDetailResponse's field order no longer matches the order this "
        "endpoint emits. FastAPI serialises in field order, so this changes the "
        "response bytes for every caller — update _WIRE_KEY_ORDER only if that is "
        "the intent.\n"
        f"  model: {list(MemoryDetailResponse.model_fields)}\n"
        f"  wire:  {_WIRE_KEY_ORDER}"
    )


@pytest.mark.integration
async def test_memory_get_emits_the_pinned_key_order(client, tenant_id) -> None:
    """The live response's key order, against the written-out list."""
    headers = get_admin_headers()
    w = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": "shape-agent",
            "memory_type": "fact",
            "content": f"serialization contract probe {uid()}",
        },
        headers=headers,
    )
    assert w.status_code in (200, 201), w.text
    r = await client.get(
        f"/api/v1/memories/{w.json()['id']}?tenant_id={tenant_id}", headers=headers
    )
    assert r.status_code == 200, r.text

    assert list(r.json()) == _WIRE_KEY_ORDER, (
        "the 200 body's key order drifted from the order this endpoint has always "
        "emitted, which changes the bytes every caller receives.\n"
        f"  got:      {list(r.json())}\n  expected: {_WIRE_KEY_ORDER}"
    )


@pytest.mark.integration
async def test_memory_get_timestamps_keep_the_offset_form(client, tenant_id) -> None:
    """A ``datetime``-typed field would emit ``...Z`` and break the wire format."""
    headers = get_admin_headers()
    w = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": "shape-agent",
            "memory_type": "fact",
            "content": f"timestamp format probe {uid()}",
        },
        headers=headers,
    )
    assert w.status_code in (200, 201), w.text
    body = (
        await client.get(
            f"/api/v1/memories/{w.json()['id']}?tenant_id={tenant_id}", headers=headers
        )
    ).json()

    present = {f: body[f] for f in _TIMESTAMP_FIELDS if body.get(f) is not None}
    assert present, "no timestamp field was populated, so this test proved nothing"
    for field, value in present.items():
        assert _ISO_OFFSET.match(value), (
            f"{field} serialised as {value!r}. It must keep the ``+00:00`` offset form: "
            "typing it as ``datetime`` makes pydantic emit ``Z`` instead, which is a "
            "wire change for every consumer of this endpoint."
        )
