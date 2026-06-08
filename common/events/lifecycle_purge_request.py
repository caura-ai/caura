"""Typed payload for ``memclaw.lifecycle.purge-soft-deleted-requested``
(CAURA-656). Diverges from
:class:`~common.events.lifecycle_archive_request.LifecycleArchiveRequest`
by carrying ``retention_days`` — the per-org policy snapshot that the
fanout endpoint reads from organization_settings at cron-tick time and
bakes into each per-org message. Consumer trusts the value (no
re-fetch), so a settings change between fanout and consume uses the
snapshot from the previous tick — acceptable for daily cadence.
"""

from __future__ import annotations

from pydantic import Field

from common.events.lifecycle_archive_request import LifecycleRequestBase

# Inclusive range applied at three boundaries: the org-settings PUT
# validator, this Pydantic field, and the storage-side primitive's
# default. One source of truth so a future widening only touches one
# constant.
MEMORY_RETENTION_MIN_DAYS = 1
MEMORY_RETENTION_MAX_DAYS = 30


class LifecyclePurgeRequest(LifecycleRequestBase):
    retention_days: int = Field(
        ge=MEMORY_RETENTION_MIN_DAYS, le=MEMORY_RETENTION_MAX_DAYS
    )
