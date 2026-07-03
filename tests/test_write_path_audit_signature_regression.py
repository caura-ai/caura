"""Regression: the pipeline write path must invoke the audit hook successfully.

``#491`` (delete core-api's direct DB pool; route all DB via core-storage)
dropped the ``db`` parameter from ``audit_service.log_action`` — its signature
became fully keyword-only. It updated the other call sites (mcp_server,
agents, documents) but MISSED ``write_memory_row.py``, which kept passing
``ctx.db`` positionally. Result on eToro v2.11.0 (backend 2.18.0): every memory
create raised ``TypeError: log_action() takes 0 positional arguments`` inside
``write_memory_row``'s ``try/except`` — swallowed as "Audit hook failed
(non-critical)", so the row still wrote but NO audit-log entry was recorded.

Why this slipped through: ``test_mcp_write_db_none_regression`` drives the same
pipeline but only asserts the row persists — the swallowed audit failure leaves
the write succeeding, so it stays green. The integration test below asserts the
audit hook does NOT fail; the unit test guards the ``log_action`` contract.
"""

from __future__ import annotations

import inspect
import logging
import uuid

import pytest

from core_api.services.audit_service import log_action


def test_log_action_is_keyword_only_no_positional_db():
    """``log_action`` is storage-routed (no ``db``) and must stay fully
    keyword-only, so callers cannot pass a positional. Re-adding any
    positional/positional-or-keyword param reintroduces the #491 mismatch."""
    positional = [
        p.name
        for p in inspect.signature(log_action).parameters.values()
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    assert not positional, (
        f"log_action must remain keyword-only (storage-routed, no db); "
        f"found positional params: {positional}"
    )


# ---------------------------------------------------------------------------
# Integration: drive the real write pipeline and assert the audit hook ran.
# ---------------------------------------------------------------------------
pytestmark_integration = [pytest.mark.integration, pytest.mark.asyncio]

_PADDING = (
    " This memory carries enough surrounding context to pass the content-length gate."
)


@pytest.fixture
def _use_pipeline_write():
    import core_api.services.memory_service as memory_service

    original = memory_service._USE_PIPELINE_WRITE
    memory_service._USE_PIPELINE_WRITE = True
    yield
    memory_service._USE_PIPELINE_WRITE = original


@pytest.mark.integration
@pytest.mark.asyncio
async def test_memory_write_audit_hook_does_not_fail(db, caplog, _use_pipeline_write):
    """A memory create must invoke the audit hook WITHOUT the swallowed
    TypeError. Captures ``write_memory_row``'s logger: if the hook raises
    (the #491 regression — positional ``ctx.db`` into keyword-only
    ``log_action``), it logs "Audit hook failed" and this test fails."""
    from core_api.schemas import MemoryCreate, MemoryOut
    from core_api.services.memory_service import create_memory

    tenant = f"test-tenant-auditsig-{uuid.uuid4().hex[:8]}"
    content = "The Danube flows through ten countries." + _PADDING

    with caplog.at_level(
        logging.WARNING, logger="core_api.pipeline.steps.write.write_memory_row"
    ):
        result = await create_memory(
            MemoryCreate(
                tenant_id=tenant,
                fleet_id="test-fleet",
                agent_id="test-agent",
                content=content,
                persist=True,
                entity_links=[],
            )
        )

    assert isinstance(result, MemoryOut)
    audit_failures = [
        r for r in caplog.records if "Audit hook failed" in r.getMessage()
    ]
    assert not audit_failures, (
        "write_memory_row's audit hook failed (non-critical swallow) — "
        f"#491 signature regression: {[r.getMessage() for r in audit_failures]}"
    )
