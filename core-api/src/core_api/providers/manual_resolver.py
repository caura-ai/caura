"""Manual conflict resolver for OSS deployments.

Logs detected contradictions and keeps both memories without auto-resolving.
Business deployments use trust-rank resolvers and policy chains.
"""

from __future__ import annotations

import logging

from core_api.protocols import ConflictResult, Resolution

logger = logging.getLogger(__name__)


class ManualResolver:
    """Keeps both memories and logs the conflict for human review."""

    async def resolve(self, conflict: ConflictResult) -> Resolution:
        logger.info(
            "Conflict detected between %s and %s (%s) — keeping both",
            conflict.existing_memory_id,
            conflict.new_memory_id,
            conflict.reason,
        )
        return Resolution(
            action="keep_both",
            explanation="OSS tier: conflicts are logged, not auto-resolved",
        )
