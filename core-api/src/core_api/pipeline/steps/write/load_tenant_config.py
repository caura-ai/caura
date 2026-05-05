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
        tenant_config = await resolve_config(ctx.require_db, data.tenant_id)
        ctx.data["tenant_config"] = tenant_config
        ctx.tenant_config = tenant_config
        return None
