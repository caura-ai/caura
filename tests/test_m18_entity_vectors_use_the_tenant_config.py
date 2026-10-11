"""M-18: the nightly entity backfill embeds entity names in the tenant's space.

``entity_link`` resolved the tenant's config for its flag check, then built the
pipeline context without it. ``BackfillEntityEmbeddings`` therefore embedded
every entity name with no tenant config: the process-wide provider, not the
``embedding.provider``, model and key the tenant's memories are embedded with.
Two vector spaces in one tenant error nowhere and show up only as worse
entity-boosted recall.
"""

from types import SimpleNamespace

import pytest

from core_api.pipeline.compositions import entity_linking
from core_api.services import lifecycle_audit

pytestmark = pytest.mark.unit


async def test_the_entity_link_pipeline_runs_with_the_tenant_config(monkeypatch):
    config = SimpleNamespace(auto_entity_linking_enabled=True)
    contexts = []

    async def _resolve_config(org_id):
        return config

    class _Pipeline:
        async def run(self, ctx):
            contexts.append(ctx)
            return SimpleNamespace(failed=False, steps=[])

    monkeypatch.setattr(lifecycle_audit, "resolve_config", _resolve_config)
    monkeypatch.setattr(entity_linking, "build_full_entity_linking_pipeline", _Pipeline)

    adapter = lifecycle_audit.make_storage_adapter(None)
    await adapter.entity_link(org_id="org-m18", fleet_id=None)

    assert [ctx.tenant_config for ctx in contexts] == [config]
