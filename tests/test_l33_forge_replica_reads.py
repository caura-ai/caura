"""09/02 L-33 — two forge reads went to the replica that must not.

Both failures are silent, and they fail in opposite directions.

1. **The no-overwrite guard** (`cron_handler._make_status_checker`) exists to
   skip writes against a slug an operator has already moved to `active`,
   `rejected` or `quarantined`. Read from the replica, a recent status flip may
   not have arrived, so the check returns the OLD status — or `None` — and the
   write it was meant to prevent goes through. **The guard fails unsafe.**

   `skill_promoter`'s equivalent existence check already passed `read=False`
   for exactly this reason. Only the cron handler's did not.

2. **Same-tick promotion** (`skill_promoter.promote_pending_candidates`). The
   forge cron mines candidates and then promotes them in the SAME tick, so the
   rows being queried were written moments earlier. On the replica they may not
   have arrived, and the query returns a short list rather than an error — so
   promotion is quietly deferred to the next tick with nothing logged.

`query_documents` hardcoded `read=True` with no way to opt out, so (2) could
not be fixed at the call site until the client grew a parameter.
"""

import inspect

import pytest

pytestmark = pytest.mark.unit


def _src(fn) -> str:
    return inspect.getsource(fn)


# ── the client can now express the choice ────────────────────────────────


def test_query_documents_accepts_a_read_target():
    from core_api.clients.storage_client import CoreStorageClient

    sig = inspect.signature(CoreStorageClient.query_documents)
    assert "read" in sig.parameters


def test_the_default_is_still_the_replica():
    """Six other callers rely on it. This fix must not quietly move every
    document query onto the primary."""
    from core_api.clients.storage_client import CoreStorageClient

    assert (
        inspect.signature(CoreStorageClient.query_documents).parameters["read"].default
        is True
    )


def test_the_parameter_actually_reaches_the_transport():
    """A parameter that is accepted and ignored would make every assertion
    below pass while changing nothing."""
    from core_api.clients.storage_client import CoreStorageClient

    assert "read=read" in _src(CoreStorageClient.query_documents)


# ── the clobber guard reads the primary ──────────────────────────────────


def test_the_no_overwrite_guard_reads_the_primary():
    from core_api.services.forge import cron_handler

    src = _src(cron_handler._make_status_checker)
    assert "read=False" in src


def test_the_guard_matches_the_sibling_check_that_was_already_correct():
    """``skill_promoter`` had this right; the two existence checks answer the
    same question and must not disagree about where they read it from."""
    from core_api.services import skill_promoter

    src = _src(skill_promoter)
    assert (
        "get_document(tenant_id=tenant_id, collection=collection, doc_id=doc_id, read=False)"
        in src
    )


# ── same-tick promotion reads the primary ────────────────────────────────


def test_promotion_queries_the_primary():
    """Read via AST, not substring. The comment explaining this fix contains
    the literal ``read=False`` and sits ABOVE the call, so a text search finds
    the prose and asserts nothing about the code — which is exactly what it did
    when I first wrote this test."""
    import ast

    from core_api.services.skill_promoter import promote_pending_candidates

    tree = ast.parse(_src(promote_pending_candidates).lstrip())
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "query_documents"
    ]
    assert calls, "expected a query_documents call"
    kwargs = {k.arg: ast.unparse(k.value) for k in calls[0].keywords}
    assert kwargs.get("read") == "False", (
        f"query_documents must read the primary; got read={kwargs.get('read')}"
    )


def test_promotion_still_filters_to_forge_candidates():
    """Guard against the read-target change disturbing the query itself."""
    from core_api.services.skill_promoter import promote_pending_candidates

    src = _src(promote_pending_candidates)
    assert '"status": "candidate"' in src
    assert '"source": "forge"' in src
    assert '"order_by": "created_at"' in src
