"""LoadAndSerialize — serialize results to MemoryOut using pre-loaded entity links."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from core_api.clients.storage_client import get_storage_client
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepResult
from core_api.schemas import ScoreParts
from core_api.services.memory_service import _memory_to_out

logger = logging.getLogger(__name__)

MAX_SUCCESSOR_LOOKUPS = 10

_SCORE_FACTORS = (
    "vec_sim",
    "fts_score",
    "freshness",
    "entity_boost",
    "recall_boost",
    "temporal_boost",
    "status_penalty",
)


def _mem_field(memory, key: str):
    """Field access tolerating both SimpleNamespace and dict Memory shapes."""
    if hasattr(memory, key):
        return getattr(memory, key)
    if isinstance(memory, dict):
        return memory.get(key)
    return None


def _score_parts(row) -> ScoreParts | None:
    """Build the D12 factor breakdown from a scored row; None when unscored.

    A row with every factor None (successor-injected rows, hand-built test
    rows) yields None rather than an all-null object, so unscored contexts
    keep the field absent instead of noisy.
    """
    values = {k: getattr(row, k, None) for k in _SCORE_FACTORS}
    if all(v is None for v in values.values()):
        return None
    return ScoreParts(**{k: (round(float(v), 4) if v is not None else None) for k, v in values.items()})


class LoadAndSerialize:
    @property
    def name(self) -> str:
        return "load_and_serialize"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        rows = list(ctx.data["filtered_rows"])

        # Follow supersedes chain: inject successors for outdated/conflicted memories
        outdated_ids = [
            row.Memory.id if hasattr(row.Memory, "id") else row.Memory.get("id")
            for row in rows
            if (row.Memory.status if hasattr(row.Memory, "status") else row.Memory.get("status"))
            in ("outdated", "conflicted")
        ]
        if outdated_ids:
            if len(outdated_ids) > MAX_SUCCESSOR_LOOKUPS:
                logger.warning(
                    "Capping successor lookups from %d to %d",
                    len(outdated_ids),
                    MAX_SUCCESSOR_LOOKUPS,
                )
                outdated_ids = outdated_ids[:MAX_SUCCESSOR_LOOKUPS]
            existing_ids = {
                str(row.Memory.id if hasattr(row.Memory, "id") else row.Memory.get("id")) for row in rows
            }
            data = ctx.data
            tenant_id = data["tenant_id"]

            sc = get_storage_client()
            try:
                successors = await sc.find_successors(
                    {
                        "supersedes_ids": [str(oid) for oid in outdated_ids],
                        "tenant_id": tenant_id,
                        "fleet_ids": data.get("fleet_ids"),
                        "caller_agent_id": data.get("caller_agent_id"),
                        "filter_agent_id": data.get("filter_agent_id"),
                        "memory_type_filter": data.get("memory_type_filter"),
                        "valid_at": str(data["valid_at"]) if data.get("valid_at") else None,
                    }
                )
            except Exception:
                logger.warning(
                    "find_successors failed; continuing without successor enrichment", exc_info=True
                )
                successors = []
            for successor in successors:
                sid = successor.get("id")
                if sid not in existing_ids:
                    rows.append(
                        SimpleNamespace(
                            Memory=SimpleNamespace(**successor),
                            score=None,
                            similarity=None,
                            vec_sim=None,
                            fts_score=None,
                            freshness=None,
                            entity_boost=None,
                            recall_boost=None,
                            temporal_boost=None,
                            status_penalty=None,
                            entity_links=[],
                        )
                    )
                    existing_ids.add(sid)

        # A34 — the retrieval contract for a genuine contradiction (ratified
        # 2026-08-25): whenever a result set contains both a superseded row
        # and its successor, the successor ranks IMMEDIATELY ABOVE its stale
        # predecessor. This applies to BOTH successor arrivals:
        #   * injected above (unscored — find_successors pulled it in), and
        #   * organically recalled on its own merit BELOW the stale row — the
        #     Hermes/STALE-T2 trap: a query exact-matching the OLD wording
        #     ranks the stale conflicted row #1 (A31's exact-match exemption
        #     keeps it un-penalized so it stays surfaced) with the correction
        #     underneath, and the answer LLM picks the stale value. Wet-test
        #     verified: the organic path alone reproduces the bug.
        # A successor that already ranks ABOVE its predecessor keeps its
        # earned position (it is reached first in the walk). Chains
        # (C supersedes B supersedes A) resolve newest-first via recursion.
        # No new wire fields: ``supersedes_id`` names the loser, its
        # ``status`` says why it lost.
        succ_of: dict[str, list] = {}
        for row in rows:
            sup = _mem_field(row.Memory, "supersedes_id")
            if sup:
                succ_of.setdefault(str(sup), []).append(row)
        if succ_of:
            placed: set[str] = set()
            reordered: list = []

            def _place(row) -> None:
                rid = str(_mem_field(row.Memory, "id"))
                if rid in placed:
                    return
                placed.add(rid)
                for s in succ_of.get(rid, []):
                    _place(s)
                reordered.append(row)

            # ``_place`` marks a row placed BEFORE recursing so a (corrupt)
            # supersession cycle terminates instead of recursing forever;
            # append happens after the successors so they land above.
            for row in rows:
                _place(row)
            rows = reordered

        ctx.data["results"] = [
            _memory_to_out(
                row.Memory,
                entity_links=row.entity_links,
                # Expose the raw vector cosine (``vec_sim``), NOT ``row.score`` —
                # ``score`` is the multiplicative ranking composite (similarity *
                # freshness * entity/recall/temporal boosts) which routinely
                # exceeds 1.0, so it's useless for client-side threshold gating.
                # ``vec_sim`` is the 0..1 cosine and matches the ``min_similarity``
                # gate in PostFilterResults. Rank order is unchanged (rows are
                # already ordered by ``score`` upstream). None for FTS-only hits,
                # which have no vector similarity.
                similarity=(round(float(row.vec_sim), 4) if row.vec_sim is not None else None),
                # D12 — the composite that ordered this row, and its factors.
                # The storage SQL has always computed these per row; this is the
                # first place they survive serialization. None end-to-end for
                # successor-injected rows, which were never scored.
                score=(round(float(row.score), 4) if row.score is not None else None),
                score_parts=_score_parts(row),
            )
            for row in rows
        ]
        return None
