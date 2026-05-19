"""A5c — plumb tenant_config through process_entity_extraction.

Today (pre-A5c): the worker at ``entity_extraction_worker.py:71`` calls
``extract_entities_from_content(content, memory_type)`` with no
``tenant_config`` argument. ``extract_entities_from_content`` falls back
to ``settings.entity_extraction_provider`` from global env — so the
tenant-level ``entity_extraction.provider`` / ``.model`` overrides
exposed by ``ResolvedConfig`` are DEAD CODE.

After A5c: the worker resolves tenant_config FIRST (it already does
this for ``entity_blocklist`` further down the function — A5c just
moves the call up and threads the result through). Tenant overrides
take effect; a tenant that sets ``entity_extraction.provider=gemini``
in organisation_settings will actually route to gemini.

Test pins: when the worker is invoked, ``extract_entities_from_content``
receives a non-None ``tenant_config`` kwarg whose
``entity_extraction_provider`` matches the resolved value. Production
code that satisfies this test ships as ~5 LOC in
``entity_extraction_worker.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_worker_forwards_resolved_tenant_config_to_extractor() -> None:
    """When ``process_entity_extraction`` runs for tenant ``t-a5c`` whose
    organisation_settings has ``entity_extraction.provider=gemini``, the
    call to ``extract_entities_from_content`` must include
    ``tenant_config`` with that provider visible.

    Pre-A5c: worker calls extractor with no tenant_config kwarg →
    tenant_config=None → settings.entity_extraction_provider wins →
    gemini override silently ignored.
    """
    from core_api.services import entity_extraction_worker
    from core_api.services.entity_extraction import ExtractedGraph

    captured: dict = {}

    async def fake_extract(content, memory_type, tenant_config=None):
        captured["tenant_config"] = tenant_config
        # Return empty so the worker short-circuits before hitting storage
        # — we only need to observe what was passed IN.
        return ExtractedGraph(entities=[], relations=[], mentions=[])

    fake_cfg = MagicMock()
    fake_cfg.entity_extraction_provider = "gemini"
    fake_cfg.entity_extraction_model = "gemini-1.5-flash"
    fake_cfg.entity_blocklist = frozenset()
    fake_cfg.entity_extraction_enabled = True

    fake_resolve = AsyncMock(return_value=fake_cfg)

    with (
        patch.object(
            entity_extraction_worker,
            "extract_entities_from_content",
            side_effect=fake_extract,
        ),
        patch(
            "core_api.services.organization_settings.resolve_config",
            fake_resolve,
        ),
    ):
        await entity_extraction_worker.process_entity_extraction(
            memory_id=uuid4(),
            tenant_id="t-a5c",
            fleet_id=None,
            agent_id="agent-x",
            content="Anna ships Vermillion to Pelagic.",
            memory_type="episode",
        )

    assert "tenant_config" in captured, (
        "extract_entities_from_content was not called — worker short-circuited "
        "before reaching the extractor."
    )
    tc = captured["tenant_config"]
    assert tc is not None, (
        "Worker passed tenant_config=None to the extractor. The tenant-level "
        "entity_extraction.provider override on ResolvedConfig is dead code "
        "without this fix."
    )
    assert tc.entity_extraction_provider == "gemini", (
        f"Expected resolved tenant_config with provider=gemini to reach the "
        f"extractor; got provider={tc.entity_extraction_provider!r}. The fix "
        f"must thread the resolved config through, not just None or a fresh "
        f"unresolved instance."
    )


@pytest.mark.asyncio
async def test_worker_still_works_when_resolve_config_returns_default() -> None:
    """Back-compat: tenants without any entity_extraction override (provider
    falls through to global settings) must still get a non-None tenant_config
    passed to the extractor — and the worker must complete cleanly with no
    entities returned.
    """
    from core_api.services import entity_extraction_worker
    from core_api.services.entity_extraction import ExtractedGraph

    captured: dict = {}

    async def fake_extract(content, memory_type, tenant_config=None):
        captured["tenant_config"] = tenant_config
        return ExtractedGraph(entities=[], relations=[], mentions=[])

    # Default config — provider falls back to the global setting (in practice
    # 'openai' / 'fake' / whatever env has). We still expect the worker to
    # pass *something*, not None, so per-tenant overrides remain wired.
    fake_cfg = MagicMock()
    fake_cfg.entity_extraction_provider = "openai"
    fake_cfg.entity_extraction_model = None
    fake_cfg.entity_blocklist = frozenset()
    fake_cfg.entity_extraction_enabled = True

    fake_resolve = AsyncMock(return_value=fake_cfg)

    with (
        patch.object(
            entity_extraction_worker,
            "extract_entities_from_content",
            side_effect=fake_extract,
        ),
        patch(
            "core_api.services.organization_settings.resolve_config",
            fake_resolve,
        ),
    ):
        await entity_extraction_worker.process_entity_extraction(
            memory_id=uuid4(),
            tenant_id="t-default",
            fleet_id=None,
            agent_id="agent-x",
            content="Some content.",
            memory_type="fact",
        )

    tc = captured.get("tenant_config")
    assert tc is not None, (
        "Even with default config, worker must pass tenant_config (not None) "
        "to keep the override channel live for future per-tenant settings."
    )
    assert tc.entity_extraction_provider == "openai"
