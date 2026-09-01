"""A28 / C6 / D6 — three ways the API was quietly withholding information.

A28: successor injection truncated at 10 with only a server-side log, so the
     11th stale row surfaced as if no correction existed.
C6:  ``supersedes_id`` on create was refused by ``extra="forbid"`` with a
     generic message, so a caller could not tell refused-on-purpose from typo.
D6:  the default status policy hides superseded rows and said so nowhere.
"""

import pytest
from pydantic import ValidationError

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.search import load_and_serialize as las
from core_api.schemas import (
    SERVER_OWNED_MEMORY_FIELDS,
    MemoryCreate,
    SearchRequest,
    SearchResponse,
)

pytestmark = pytest.mark.unit


# ── C6 ────────────────────────────────────────────────────────────────────


def _body(**over):
    return {"tenant_id": "t1", "agent_id": "a1", "content": "x", **over}


def test_create_refuses_supersedes_id_with_an_explanatory_message():
    with pytest.raises(ValidationError) as e:
        MemoryCreate(**_body(supersedes_id="11111111-1111-1111-1111-111111111111"))
    msg = str(e.value)
    assert "supersedes_id" in msg
    assert "set by the server" in msg
    # It must point at the mechanism that DOES exist. There is no public route
    # that sets supersedes_id, so naming one would send callers nowhere.
    assert "contradiction detection" in msg
    assert "/status" not in msg


def test_unknown_fields_keep_the_ordinary_rejection():
    """The denylist must not swallow the generic unknown-field path — a typo is
    still a typo, and C26's behaviour has to survive."""
    with pytest.raises(ValidationError) as e:
        MemoryCreate(**_body(superseeds_id="oops"))
    msg = str(e.value)
    assert "set by the server" not in msg
    assert "superseeds_id" in msg


def test_a_valid_create_body_is_untouched():
    m = MemoryCreate(**_body())
    assert m.content == "x" and m.tenant_id == "t1"


def test_server_owned_list_is_narrow():
    """Widening this is a wire-contract decision, not a refactor: every name
    here converts a generic 422 into a specific refusal."""
    assert SERVER_OWNED_MEMORY_FIELDS == ("supersedes_id",)


# ── D6 ────────────────────────────────────────────────────────────────────


def test_status_filter_documents_the_default_exclusion():
    """The row's complaint was 'silently excluded'. The override already
    existed; what was missing was any statement that a default policy applies."""
    desc = SearchRequest.model_fields["status_filter"].description or ""
    assert "outdated" in desc and "conflicted" in desc
    assert "exactly match" in desc  # the carve-out is stated, not just the rule


# ── A28 ───────────────────────────────────────────────────────────────────


def test_bound_cannot_engage_under_the_public_contract():
    """top_k caps the result set, which caps the stale-id list. The bound is a
    guard against a pathological array param, not a cost control — if it ever
    drops below reachable, truncation becomes silent-by-default again."""
    from core_api.constants import MAX_SEARCH_TOP_K

    assert las.MAX_SUCCESSOR_LOOKUPS > MAX_SEARCH_TOP_K


def _ctx():
    return PipelineContext(data={})


def test_warn_emits_a_coded_caller_visible_entry():
    ctx = _ctx()
    las._warn(
        ctx, reason="lookup_bound_exceeded", stale_result_count=1200, enriched=1000
    )
    (w,) = ctx.data["warnings"]
    assert w["code"] == las.SUCCESSOR_ENRICHMENT_INCOMPLETE
    assert w["details"] == {
        "reason": "lookup_bound_exceeded",
        "stale_result_count": 1200,
        "enriched": 1000,
    }
    # the message has to be readable by whoever gets the response, not a slug
    assert "supersedes" in w["message"]


def test_warn_accumulates_rather_than_overwrites():
    ctx = _ctx()
    las._warn(ctx, reason="lookup_bound_exceeded", stale_result_count=9, enriched=5)
    las._warn(ctx, reason="storage_error", stale_result_count=9, enriched=0)
    assert [w["details"]["reason"] for w in ctx.data["warnings"]] == [
        "lookup_bound_exceeded",
        "storage_error",
    ]


def test_warnings_is_null_when_there_is_nothing_to_say():
    """Pins the WIRE behaviour, which is ``"warnings": null`` — not an absent
    key. FastAPI serializes None fields, so the response carries the key with a
    null value, exactly as the pre-existing ``diagnostic`` field already does.

    Verified live against the deployed image, not just the model:
        keys: ['diagnostic', 'items', 'warnings'] | warnings=None diagnostic=None

    Asserted against ``diagnostic`` rather than hard-coded so the two can't
    diverge: if anyone later adds exclude_none, both change together or this
    fails.
    """
    r = SearchResponse(items=[])
    assert r.warnings is None
    dumped = r.model_dump()
    assert dumped["warnings"] is None
    assert ("warnings" in dumped) == ("diagnostic" in dumped)


def test_response_carries_warnings_when_present():
    r = SearchResponse(
        items=[],
        warnings=[
            {
                "code": "successor_enrichment_incomplete",
                "message": "m",
                "details": {"a": 1},
            }
        ],
    )
    dumped = r.model_dump(exclude_none=True)
    assert dumped["warnings"][0]["code"] == "successor_enrichment_incomplete"


def test_storage_failure_is_reported_not_swallowed():
    """Before A28 a find_successors failure logged and returned an un-enriched
    result set with no caller signal — the same silence as the cap."""
    import inspect

    src = inspect.getsource(las.LoadAndSerialize.execute)
    fail_block = src[src.index("except Exception:") :]
    assert "_warn(" in fail_block
    assert 'reason="storage_error"' in fail_block
