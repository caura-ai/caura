"""Background worker: extract entities from a memory and upsert them."""

import logging
from uuid import UUID

from common.embedding import get_embedding
from core_api.clients.storage_client import get_storage_client
from core_api.constants import ENTITY_NAME_BLOCKLIST, MIN_ENTITY_NAME_LENGTH
from core_api.schemas import EntityUpsert, RelationUpsert
from core_api.services.audit_service import log_action
from core_api.services.entity_extraction import extract_entities_from_content
from core_api.services.entity_service import upsert_entity, upsert_relation

logger = logging.getLogger(__name__)


def _is_valid_entity(name: str, blocklist: frozenset[str] | None = None) -> bool:
    """Reject obviously generic names that are not real named entities."""
    bl = blocklist if blocklist is not None else ENTITY_NAME_BLOCKLIST
    return len(name) >= MIN_ENTITY_NAME_LENGTH and name.lower() not in bl


async def _discover_cross_links_for_memory(
    memory_id: UUID,
    tenant_id: str,
    fleet_id: str | None,
) -> None:
    """Run cross-link discovery for a single memory after entity extraction."""
    from core_api.db.session import async_session
    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.entity_linking.discover_cross_links import DiscoverCrossLinks

    async with async_session() as db:
        ctx = PipelineContext(
            db=db,
            data={
                "tenant_id": tenant_id,
                "target_memory_ids": [memory_id],
                **({"fleet_id": fleet_id} if fleet_id else {}),
            },
        )
        step = DiscoverCrossLinks()
        await step.execute(ctx)
        await db.commit()
        links = ctx.data.get("links_created", 0)
        if links:
            logger.info(
                "Cross-link discovery created %d links for memory %s",
                links,
                memory_id,
            )


async def process_entity_extraction(
    memory_id: UUID,
    tenant_id: str,
    fleet_id: str | None,
    agent_id: str,
    content: str,
    memory_type: str,
) -> None:
    # CAURA-595: today this runs in-process in core-api (every scheduler
    # in the codebase wraps it in `track_task`). That satisfies the
    # literal "off the hot path" framing but not the original intent of
    # the scaling plan, which was to land the work on a dedicated worker
    # fleet so core-api isn't CPU/memory-contended by burst-time LLM
    # calls. Full migration: CAURA-593 lands Pub/Sub first, then a new
    # worker service subscribes to ``Topics.Pipeline.ENTITY_EXTRACT_REQUESTED``
    # and this function becomes its handler body.
    try:
        # A5c: resolve tenant_config BEFORE the extraction call so the
        # tenant-level ``entity_extraction.provider`` / ``.model``
        # overrides on ResolvedConfig actually take effect. Pre-A5c the
        # worker passed nothing here, falling back to global settings,
        # so per-tenant routing was dead code.
        from core_api.services.organization_settings import resolve_config

        tenant_cfg = await resolve_config(None, tenant_id)

        graph = await extract_entities_from_content(content, memory_type, tenant_config=tenant_cfg)
        if not graph.entities:
            return

        sc = get_storage_client()

        try:
            blocklist = tenant_cfg.entity_blocklist

            # Embed entity names for fuzzy resolution
            name_embeddings: dict[str, list[float]] = {}
            for ent in graph.entities:
                try:
                    name_embeddings[ent.canonical_name] = await get_embedding(ent.canonical_name)
                except Exception:
                    logger.debug(
                        "Failed to embed entity name '%s', skipping fuzzy resolution",
                        ent.canonical_name,
                    )

            # Upsert entities and build name -> UUID map
            name_to_id: dict[str, UUID] = {}
            entity_roles: dict[str, str] = {}

            for ent in graph.entities:
                if not _is_valid_entity(ent.canonical_name, blocklist):
                    logger.debug(
                        "Skipping invalid entity name '%s'",
                        ent.canonical_name,
                    )
                    continue
                # ``data`` is positional, ``name_embedding`` keyword-only
                # under the CAURA-127 signature cleanup.
                result = await upsert_entity(
                    EntityUpsert(
                        tenant_id=tenant_id,
                        fleet_id=fleet_id,
                        entity_type=ent.entity_type,
                        canonical_name=ent.canonical_name,
                    ),
                    name_embedding=name_embeddings.get(ent.canonical_name),
                )
                name_to_id[ent.canonical_name] = result.id
                entity_roles[ent.canonical_name] = ent.role

            # Create memory-entity links
            for name, entity_id in name_to_id.items():
                existing = await sc.find_entity_link(
                    str(memory_id),
                    str(entity_id),
                )
                if not existing:
                    await sc.create_entity_link(
                        {
                            "memory_id": str(memory_id),
                            "entity_id": str(entity_id),
                            "role": entity_roles.get(name, "mentioned"),
                        }
                    )

            # Upsert relations
            rel_count = 0
            for rel in graph.relations:
                from_id = name_to_id.get(rel.from_entity)
                to_id = name_to_id.get(rel.to_entity)
                if from_id and to_id:
                    await upsert_relation(
                        None,
                        RelationUpsert(
                            tenant_id=tenant_id,
                            fleet_id=fleet_id,
                            from_entity_id=from_id,
                            relation_type=rel.relation_type,
                            to_entity_id=to_id,
                            evidence_memory_id=memory_id,
                        ),
                    )
                    rel_count += 1

            # Audit log
            await log_action(
                None,
                tenant_id=tenant_id,
                agent_id=agent_id,
                action="entity_extraction",
                resource_type="memory",
                resource_id=memory_id,
                detail={
                    "entities_count": len(name_to_id),
                    "relations_count": rel_count,
                },
            )

            logger.info(
                "Entity extraction complete for memory %s: %d entities, %d relations",
                memory_id,
                len(name_to_id),
                rel_count,
            )

            # Trigger entity-based contradiction detection now that entity links exist
            if name_to_id:
                from core_api.services.contradiction_detector import (
                    detect_contradictions_by_entities_async,
                )
                from core_api.tasks import track_task

                track_task(detect_contradictions_by_entities_async(memory_id, tenant_id, fleet_id))

            # Cross-link discovery (non-fatal)
            if tenant_cfg.auto_entity_linking_enabled:
                try:
                    await _discover_cross_links_for_memory(memory_id, tenant_id, fleet_id)
                except Exception:
                    logger.warning(
                        "Cross-link discovery failed for memory %s (non-fatal)",
                        memory_id,
                        exc_info=True,
                    )
        except Exception:
            raise

    except Exception:
        logger.exception("Entity extraction failed for memory %s (non-fatal)", memory_id)
