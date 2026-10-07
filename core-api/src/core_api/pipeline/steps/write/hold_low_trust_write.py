"""HoldLowTrustWrite — store the write ``quarantined`` when the organization holds its agent (g2.8)."""

from __future__ import annotations

from common.constants import QUARANTINED_MEMORY_STATUS
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepResult
from core_api.services.system_metadata import SYSTEM_NAMESPACE
from core_api.services.write_hold import HOLD_KEY, hold_for


class HoldLowTrustWrite:
    """Right after ``MergeEnrichmentFields`` in every composition that has it.

    It overrides the status that step settled, whatever the caller asked for,
    and every later step reads ``memory_fields``, so the row is written held.
    That includes the auto-chunk path, which writes its parent and chunks from
    the same fields.
    """

    @property
    def name(self) -> str:
        return "hold_low_trust_write"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        data = ctx.data["input"]
        hold = await hold_for(
            data.tenant_id,
            data.agent_id,
            data.fleet_id,
            ctx.tenant_config,
            is_inferred=bool(ctx.data.get("is_inferred")),
        )
        if hold is None:
            return None
        fields = ctx.data["memory_fields"]
        fields["status"] = QUARANTINED_MEMORY_STATUS
        metadata = fields["metadata"]
        metadata.setdefault(SYSTEM_NAMESPACE, {})[HOLD_KEY] = hold
        # A held write gets no atomic-fact children (they would be live rows
        # carrying its claims), so its facts stay on the row, under the key the
        # enrichment worker stores them in, for a release to fan out.
        facts = getattr(ctx.data.get("enrichment"), "atomic_facts", None) or []
        if facts:
            metadata["atomic_facts"] = [fact.model_dump(mode="json") for fact in facts]
        return None
