"""LoadTenantConfig — resolve per-tenant LLM/embedding provider settings."""

from __future__ import annotations

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepResult


class LoadTenantConfig:
    @property
    def name(self) -> str:
        return "load_tenant_config"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        from core_api.services.organization_settings import resolve_config

        data = ctx.data["input"]
        # ``ctx.db`` (nullable), NOT ``require_db``: ``resolve_config`` ignores
        # ``db`` entirely (settings load through core-storage-api since Fix 2
        # Phase 0), so the STM/db=None write path (e.g. evolve's outcome/rule
        # persist) must not be forced to carry a pooled session just for config.
        tenant_config = await resolve_config(data.tenant_id)
        ctx.data["tenant_config"] = tenant_config
        ctx.tenant_config = tenant_config
        return None
