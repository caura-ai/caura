"""D11 — ConflictOut OpenAPI model must document the conflict fields.

The conflicts routes return ``core_api.schemas.ConflictOut`` instances (the
runtime model). ``core_api.openapi_responses.ConflictOut`` is the spec-only
model attached via ``responses={200: {"model": ...}}`` to document the wire
shape for generated clients. It is NOT used for serialization, so nothing
enforces parity at runtime — issue #1440 was exactly this drift: seven runtime
fields were missing from the spec, so generated clients understated the
response.

This test ratchets that regression: the seven fields below must stay present in
the documented model.
"""

import pytest

pytestmark = pytest.mark.unit

# Fields the runtime conflict payload carries that the spec-only model must
# document (see issue #1440).
REQUIRED_CONFLICT_FIELDS = {
    "fleet_id",
    "relationship_confidence",
    "diagnosis_confidence",
    "evidence_strength",
    "audit_reason",
    "created_by",
    "created_at",
}


def test_conflict_openapi_model_documents_required_fields():
    from core_api import openapi_responses

    spec_fields = set(openapi_responses.ConflictOut.model_fields.keys())

    missing = REQUIRED_CONFLICT_FIELDS - spec_fields
    assert not missing, (
        f"ConflictOut OpenAPI model is missing fields the handler serializes: "
        f"{sorted(missing)}. Add them to core_api/openapi_responses.py "
        f"(issue #1440)."
    )
