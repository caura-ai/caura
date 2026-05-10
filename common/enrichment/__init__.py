"""Memory-enrichment service shared across services.

Public surface:

* :class:`AtomicFact`, :class:`EnrichmentResult` — Pydantic schemas
  mirroring the LLM JSON output.
* :func:`enrich_memory` — async entrypoint with 3-tier fallback.
* :func:`fake_enrich` — keyword heuristic fallback used as the
  ``"fake"`` provider and last-resort safety net.
* Constants: :data:`MEMORY_TYPES`, :data:`MEMORY_STATUSES`,
  :data:`DEFAULT_MEMORY_TYPE`, :data:`DEFAULT_MEMORY_WEIGHT`.

Reads provider config from environment variables
(``ENTITY_EXTRACTION_PROVIDER``, plus the tenant-config attributes
``enrichment_provider`` / ``enrichment_model``) — no dependency on a
service-specific config module — so both core-api (tenant-aware) and
core-worker (handler-only) share the same implementation.

CAURA-595 full form: extracted from
``core_api.services.memory_enrichment`` so that ``core-worker`` can
handle ``MemoryEnrichRequest`` events without depending on
``core-api``.
"""

from __future__ import annotations

from common.enrichment.constants import (
    DEFAULT_MEMORY_TYPE,
    DEFAULT_MEMORY_WEIGHT,
    MEMORY_STATUSES,
    MEMORY_TYPES,
)
from common.enrichment.schema import AtomicFact, EnrichmentResult
from common.enrichment.service import (
    enrich_memory,
    fake_enrich,
)

__all__ = [
    "DEFAULT_MEMORY_TYPE",
    "DEFAULT_MEMORY_WEIGHT",
    "MEMORY_STATUSES",
    "MEMORY_TYPES",
    "AtomicFact",
    "EnrichmentResult",
    "enrich_memory",
    "fake_enrich",
]
