"""A duplicate 409 answers in fields, not in a sentence — API-06 / C29.

The winning row's id used to travel as English. Storage built
``Duplicate memory exists: <uuid>``, core-api lifted that string out of
storage's JSON and re-raised it as its own detail, and the MCP server
regex-parsed the uuid back out at the far end. A uuid was serialised into
prose, shipped across two service boundaries, and recovered with a regular
expression — by us, against our own API.

Two properties matter and both are pinned here:

* the structured fields exist, including ``existing_status``, which the
  sentence could never carry; and
* the sentence is UNCHANGED, because four other test modules and any customer
  who parsed it depend on it, and because byte-identical prose is what lets the
  two services deploy in either order.
"""

from __future__ import annotations

import uuid

import pytest

from common import duplicate_memory
from tests.conftest import get_test_auth


@pytest.mark.integration
async def test_exact_duplicate_answers_with_fields_and_keeps_the_sentence(client) -> None:
    tenant_id, headers = get_test_auth(f"t-c29-{uuid.uuid4().hex[:8]}")
    body = {
        "tenant_id": tenant_id,
        "agent_id": "a-c29",
        "content": f"c29 exact duplicate probe {uuid.uuid4().hex}",
    }
    first = await client.post("/api/v1/memories", json=body, headers=headers)
    assert first.status_code == 201, first.text
    existing_id = first.json()["id"]

    second = await client.post("/api/v1/memories", json=body, headers=headers)
    assert second.status_code == 409, second.text
    payload = second.json()

    # The structured half — what C29 adds.
    assert payload["error"]["code"] == duplicate_memory.DUPLICATE_MEMORY_CODE
    details = payload["error"]["details"]
    assert details["existing_id"] == existing_id
    assert details["reason"] == duplicate_memory.REASON_EXACT
    # Never expressible in the sentence: a duplicate of an archived row is not
    # the same situation as a duplicate of a live one.
    assert "existing_status" in details

    # The prose half — unchanged, byte for byte.
    assert payload["detail"] == duplicate_memory.exact_message(existing_id)


@pytest.mark.unit
def test_the_messages_are_byte_identical_to_what_they_replaced() -> None:
    """If these drift, the mid-deploy regex fallback and four other test
    modules break at the same time — so they are pinned as literals here
    rather than derived from the module under test."""
    mid = "0d1e2f34-5678-4abc-8def-0123456789ab"
    assert duplicate_memory.exact_message(mid) == f"Duplicate memory exists: {mid}"
    assert duplicate_memory.near_message(mid) == f"Near-duplicate memory exists: {mid}"
    assert (
        duplicate_memory.NOT_LIVE_MESSAGE
        == "Duplicate memory exists but is no longer live; retry the write"
    )


@pytest.mark.unit
def test_absent_fields_are_omitted_not_nulled() -> None:
    """A consumer must be able to tell 'not applicable' from 'unknown'."""
    fields = duplicate_memory.duplicate_fields(reason=duplicate_memory.REASON_EXACT)
    assert fields == {"reason": duplicate_memory.REASON_EXACT}
    assert "existing_id" not in fields
    assert "existing_status" not in fields


@pytest.mark.unit
def test_mcp_reads_the_structured_form_including_status() -> None:
    from core_api.mcp_server import _duplicate_info

    detail = duplicate_memory.core_api_detail(
        duplicate_memory.exact_message("abc"),
        **duplicate_memory.duplicate_fields(
            reason=duplicate_memory.REASON_EXACT,
            existing_id="abc",
            existing_status="archived",
        ),
    )
    info = _duplicate_info(detail)
    assert info == {
        "reason": duplicate_memory.REASON_EXACT,
        "existing_id": "abc",
        "existing_status": "archived",
    }


@pytest.mark.unit
def test_mcp_still_understands_an_older_storage_talking_prose() -> None:
    """The mid-deploy window: a new core-api against a storage that predates
    the structured body still recovers the id, and says which reason it is."""
    from core_api.mcp_server import _duplicate_info

    mid = "0d1e2f34-5678-4abc-8def-0123456789ab"
    info = _duplicate_info(duplicate_memory.exact_message(mid))
    assert info == {"existing_id": mid, "reason": duplicate_memory.REASON_EXACT}


@pytest.mark.unit
def test_a_semantic_hit_is_not_treated_as_a_retry_no_op() -> None:
    """A near-duplicate means the caller wrote NEW content that we suppressed.
    That used to be distinguished only by the regex failing to match the other
    wording — i.e. by accident."""
    from core_api.mcp_server import _duplicate_info

    info = _duplicate_info(
        duplicate_memory.core_api_detail(
            duplicate_memory.near_message("abc"),
            **duplicate_memory.duplicate_fields(
                reason=duplicate_memory.REASON_SEMANTIC, existing_id="abc", similarity=0.97
            ),
        )
    )
    assert info is not None
    assert info["reason"] == duplicate_memory.REASON_SEMANTIC
    assert info["similarity"] == 0.97


@pytest.mark.unit
def test_a_non_duplicate_detail_is_not_mistaken_for_one() -> None:
    from core_api.mcp_server import _duplicate_info

    assert _duplicate_info("tenant_id required") is None
    assert _duplicate_info({"code": "INVALID_ARGUMENTS", "message": "nope"}) is None


@pytest.mark.unit
def test_structured_details_never_reach_a_model_as_a_dict_repr() -> None:
    """``str()`` on the new detail shape renders braces and quotes into a
    message a model reads. Twelve MCP sites used to do exactly that."""
    from core_api.mcp_server import _detail_code, _detail_text

    detail = duplicate_memory.core_api_detail("Duplicate memory exists: abc")
    assert _detail_text(detail) == "Duplicate memory exists: abc"
    assert "{" not in _detail_text(detail)
    assert _detail_text("plain string") == "plain string"
    # The raiser's own code wins over the status-derived one.
    assert _detail_code(detail, 409) == duplicate_memory.DUPLICATE_MEMORY_CODE
    assert _detail_code("plain string", 409) == "CONFLICT"


@pytest.mark.unit
def test_an_older_storage_response_yields_no_fields_rather_than_an_error() -> None:
    """Deploy-order independence: core-api must tolerate a storage that only
    sends ``detail``."""
    import httpx

    from core_api.clients.storage_client import _storage_detail, _storage_duplicate_fields

    old = httpx.Response(409, json={"detail": "Duplicate memory exists: abc"})
    assert _storage_duplicate_fields(old) == {}
    assert _storage_detail(old) == "Duplicate memory exists: abc"

    new = httpx.Response(
        409,
        json={
            "detail": "Duplicate memory exists: abc",
            "reason": duplicate_memory.REASON_EXACT,
            "existing_id": "abc",
            "existing_status": "active",
        },
    )
    assert _storage_duplicate_fields(new) == {
        "reason": duplicate_memory.REASON_EXACT,
        "existing_id": "abc",
        "existing_status": "active",
    }

    # Error paths must not raise from inside an exception handler.
    assert _storage_duplicate_fields(httpx.Response(409, text="<html>502</html>")) == {}
