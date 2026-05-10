"""Back-compat re-export shim for :mod:`common.enrichment` (CAURA-595).

The implementation moved to ``common/enrichment/`` so core-worker can
handle ``MemoryEnrichRequest`` events without depending on core-api.

Renames preserved as aliases:

* ``MemoryEnrichment`` → :class:`common.enrichment.EnrichmentResult`
* ``_fake_enrich``      → :func:`common.enrichment.fake_enrich`

Internal-detail re-exports (``_validate_enrichment``, ``call_with_fallback``,
``_call_with_retry``) are exposed for tests that patch this module's
attribute path.  New tests should patch ``common.enrichment.service.*``
or ``common.llm.retry.*`` directly.
"""

from __future__ import annotations

from common.enrichment._prompts import ENRICHMENT_PROMPT
from common.enrichment.schema import AtomicFact
from common.enrichment.schema import EnrichmentResult as MemoryEnrichment
from common.enrichment.service import (
    _validate_enrichment,  # noqa: F401  # re-export for legacy tests; not in __all__
    enrich_memory,
)
from common.enrichment.service import (
    fake_enrich as _fake_enrich,
)
from common.llm.retry import call_with_fallback
from common.llm.retry import call_with_retry as _call_with_retry

__all__ = [
    "ENRICHMENT_PROMPT",
    "AtomicFact",
    "MemoryEnrichment",
    "_call_with_retry",
    "_fake_enrich",
    "call_with_fallback",
    "enrich_memory",
]
