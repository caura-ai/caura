"""LogRecallEvent — opt-in diagnostic logging of agent-chosen recalls.

Writes one ``recall_event`` + a handful of ``recall_candidate`` rows (the
returned top-k *and* a few near-misses below the similarity floor) so we can
answer "why aren't good memories recalled?" — distinguishing *nobody asked*
from *just missed the cutoff* from *outranked*.

Gating (both cheap, evaluated before any DB work):
  1. ``source == "mcp_recall"`` — ONLY the agent-chosen tool. The plugin's
     automatic ``/search`` is never logged.
  2. ``tenant_config.recall_logging_enabled`` — per-tenant opt-in (default off).

The actual writes run fire-and-forget in a background task (its own session),
exactly like ``TrackRecalls`` — zero added request latency, and any failure is
swallowed so logging can never break a recall.
"""

from __future__ import annotations

import logging

from core_api.clients.storage_client import get_storage_client
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepResult
from core_api.tasks import track_task

logger = logging.getLogger(__name__)

# Near-misses (below the floor / outside top_k) to record per recall, so we can
# see "the brand memory was rank 7 at cosine 0.27 — just missed."
_NEAR_MISS_LIMIT = 5


def _mem_id(row) -> object:
    m = row.Memory
    return m.id if hasattr(m, "id") else m.get("id")


async def _persist(event: dict, candidates: list[dict]) -> None:
    # Routes the write through core-storage (no core-api DB pool). The payload
    # goes over JSON, so every value must already be JSON-safe — the candidate
    # ``memory_id`` UUIDs are stringified at construction in ``execute`` and the
    # event dict carries only str/int/float/None values (no UUID/datetime).
    try:
        await get_storage_client().log_recall(event, candidates)
    except Exception:
        logger.warning("recall-event logging failed", exc_info=True)


class LogRecallEvent:
    @property
    def name(self) -> str:
        return "log_recall_event"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        try:
            # Gate 1: only the agent-chosen tool.
            if ctx.data.get("source") != "mcp_recall":
                return None
            # Gate 2: per-tenant opt-in.
            cfg = ctx.tenant_config
            if not getattr(cfg, "recall_logging_enabled", False):
                return None

            sp = ctx.data.get("search_params", {}) or {}
            plan = ctx.data.get("retrieval_plan")
            strategy = plan.strategy.value if plan is not None else None
            fleet_ids = ctx.data.get("fleet_ids") or []

            returned_rows = list(ctx.data.get("filtered_rows", []) or [])
            raw_rows = list(ctx.data.get("raw_rows", []) or [])
            returned_ids = {_mem_id(r) for r in returned_rows}

            candidates: list[dict] = []
            for rank, row in enumerate(returned_rows, start=1):
                candidates.append(
                    {
                        "rank": rank,
                        # JSON-safe: _mem_id returns a UUID — the payload now
                        # crosses an HTTP boundary, so stringify it.
                        "memory_id": str(_mem_id(row)),
                        "vec_sim": _f(getattr(row, "vec_sim", None)),
                        "final_score": _f(getattr(row, "score", None)),
                        "recall_boost": _f(getattr(row, "recall_boost", None)),
                        "returned": True,
                    }
                )
            # Near-misses: raw candidates not returned (below floor / outside
            # top_k), best-first, capped.
            near = [r for r in raw_rows if _mem_id(r) not in returned_ids][:_NEAR_MISS_LIMIT]
            for offset, row in enumerate(near):
                candidates.append(
                    {
                        "rank": len(returned_rows) + offset + 1,
                        "memory_id": str(_mem_id(row)),
                        "vec_sim": _f(getattr(row, "vec_sim", None)),
                        "final_score": _f(getattr(row, "score", None)),
                        "recall_boost": _f(getattr(row, "recall_boost", None)),
                        "returned": False,
                    }
                )

            top_score = candidates[0]["final_score"] if candidates and returned_rows else None
            event = {
                "tenant_id": ctx.data.get("tenant_id"),
                "agent_id": ctx.data.get("caller_agent_id"),
                "source": "mcp_recall",
                "query_text": ctx.data.get("query"),
                "strategy": strategy,
                "filter_agent_id": ctx.data.get("filter_agent_id"),
                "fleet_scope": ",".join(fleet_ids) if fleet_ids else None,
                "top_k": ctx.data.get("top_k"),
                "min_similarity": _f(sp.get("min_similarity")),
                "result_count": len(returned_rows),
                "top_score": top_score,
            }
            track_task(_persist(event, candidates))
        except Exception:
            # Never let logging break a recall.
            logger.warning("LogRecallEvent skipped due to error", exc_info=True)
        return None


def _f(v):
    return float(v) if v is not None else None
