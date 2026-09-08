"""D11 — human review of detected conflicts.

``memory_conflicts`` recorded what the DETECTOR concluded and nothing recorded
what a PERSON concluded, so detector precision was unmeasurable: in storage a
false positive is indistinguishable from a true one.

The design point these tests protect is the SEPARATION. ``action`` stays the
detector's proposal and ``resolution_action`` is the reviewer's decision, drawn
from the same vocabulary so the two can be compared directly. Collapsing them
into one field would destroy the only record of the detector being wrong.
"""

import inspect

import pytest
from pydantic import ValidationError

from common.models.memory_conflict import REVIEW_STATUSES
from core_api.schemas import ConflictListResponse, ConflictOut, ConflictResolveRequest

pytestmark = pytest.mark.unit


# ── vocabulary ────────────────────────────────────────────────────────────


def test_review_states_are_pending_resolved_dismissed():
    assert REVIEW_STATUSES == ("pending", "resolved", "dismissed")


def test_reviewer_reuses_the_detector_action_vocabulary():
    """The comparison "did the human agree" only works if both sides speak the
    same words. A separate reviewer vocabulary would make precision a mapping
    exercise instead of an equality check."""
    src = inspect.getsource(__import__("common.models.memory_conflict", fromlist=["x"]))
    assert '_one_of("resolution_action", ACTIONS' in src


# ── the resolve contract ──────────────────────────────────────────────────


def test_resolve_refuses_to_reopen_a_reviewed_conflict():
    """'pending' is not an accepted target. Re-opening would erase the record of
    who decided what, which is the only thing this table is for."""
    with pytest.raises(ValidationError):
        ConflictResolveRequest(tenant_id="t", review_status="pending")


@pytest.mark.parametrize("state", ["resolved", "dismissed"])
def test_resolve_accepts_both_terminal_states(state):
    r = ConflictResolveRequest(tenant_id="t", review_status=state)
    assert r.review_status == state


def test_dismissed_is_reachable_without_an_action():
    """A dismissal is 'this was not a real conflict' — there is no action to
    record, and requiring one would push reviewers into inventing a verdict."""
    r = ConflictResolveRequest(tenant_id="t", review_status="dismissed")
    assert r.resolution_action is None


def test_resolve_body_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        ConflictResolveRequest(tenant_id="t", review_status="resolved", reviewer="me")


def test_reviewer_identity_is_never_taken_from_the_body():
    """resolved_by comes from the verified identity. A reviewer must not be able
    to file a decision under someone else's name."""
    assert "resolved_by" not in ConflictResolveRequest.model_fields
    src = inspect.getsource(__import__("core_api.routes.conflicts", fromlist=["x"]))
    # attribution is the gated identity, resolved before the trust check
    assert '"resolved_by": reviewer' in src
    assert "reviewer = auth.agent_id or DEFAULT_AGENT_ID" in src


# ── storage-side guarantees ───────────────────────────────────────────────


def test_resolve_is_compare_and_set_on_pending():
    """Two reviewers working one queue is the ordinary case. Without the CAS the
    second decision silently overwrites the first."""
    from core_storage_api.services.postgres_service import PostgresService

    src = inspect.getsource(PostgresService.memory_conflict_resolve)
    assert 'MemoryConflict.review_status == "pending"' in src
    assert "return bool(result.rowcount)" in src


def test_every_conflict_query_is_tenant_scoped():
    """A conflict row names two memory ids whose contents are reachable from it,
    so an unscoped read hands one tenant another's memories."""
    from core_storage_api.services.postgres_service import PostgresService

    for fn in (
        "memory_conflicts_list",
        "memory_conflict_get",
        "memory_conflict_resolve",
    ):
        src = inspect.getsource(getattr(PostgresService, fn))
        assert "MemoryConflict.tenant_id == tenant_id" in src, (
            f"{fn} is not tenant-scoped"
        )


def test_queue_is_oldest_first():
    """Newest-first leaves the oldest unreviewed rows permanently at the bottom
    — which is exactly the backlog a reviewer is trying to clear."""
    from core_storage_api.services.postgres_service import PostgresService

    src = inspect.getsource(PostgresService.memory_conflicts_list)
    assert "MemoryConflict.created_at.asc()" in src


def test_resolve_rejects_a_non_terminal_target_at_the_storage_layer_too():
    """Defence in depth: the schema blocks it at the edge, the service blocks it
    for any internal caller that bypasses the route."""
    from core_storage_api.services.postgres_service import PostgresService

    src = inspect.getsource(PostgresService.memory_conflict_resolve)
    assert 'review_status == "pending"' in src and "must be a terminal state" in src


# ── wire shape ────────────────────────────────────────────────────────────


def test_list_uses_the_items_envelope():
    assert "items" in ConflictListResponse.model_fields


def test_conflict_out_keeps_detector_and_reviewer_fields_apart():
    f = ConflictOut.model_fields
    for name in (
        "action",
        "audit_reason",
        "resolution_action",
        "resolution_note",
        "review_status",
    ):
        assert name in f, f"{name} missing from the wire model"


def test_missing_and_foreign_conflicts_answer_identically():
    """Ownership is never signalled by a distinct status: a 403-for-foreign would
    turn the endpoint into an existence oracle for other tenants' conflict ids.
    The one 403 in this module is the TRUST gate, which fires before any lookup
    and so reveals nothing about which ids exist."""
    src = inspect.getsource(__import__("core_api.routes.conflicts", fromlist=["x"]))
    assert src.count('status_code=404, detail="Conflict not found"') >= 1
    trust_403 = src.count("cannot review conflicts")
    assert src.count("status_code=403") == trust_403 == 1


def test_review_requires_elevated_trust():
    """Plain tenant scope would let any agent dismiss the contradictions it
    caused — and a dismissal is the only record that detection was wrong."""
    src = inspect.getsource(__import__("core_api.routes.conflicts", fromlist=["x"]))
    assert "_require_trust(body.tenant_id, reviewer, min_level=2)" in src
