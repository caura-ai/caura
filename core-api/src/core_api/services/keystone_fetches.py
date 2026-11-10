"""Bounded direct rule-fetch recording, independent of buffered event logs.

Only the first attempt for a binding/minute is made, including failed writes.
There are no pending tails or background flushes. Missing evidence must never
become a positive match, and recording must never make rules unavailable.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from common.governance.ruleset_hash import rule_set_hash, rules_from_keystone_rows
from common.keystone_fetches import BUCKET_SECONDS, FetchRecord
from core_api.auth import AuthContext
from core_api.clients.storage_client import CoreStorageClient

logger = logging.getLogger(__name__)


class KeystoneFetchRecorder:
    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        max_entries: int = 4096,
        max_inflight: int = 8,
        timeout_seconds: float = 0.25,
    ) -> None:
        if max_entries < 1 or max_inflight < 1 or timeout_seconds <= 0:
            raise ValueError("recorder bounds must be positive")
        self._clock = clock or (lambda: datetime.now(UTC))
        self._max_entries = max_entries
        self._max_inflight = max_inflight
        self._timeout = timeout_seconds
        self._seen: dict[tuple[str, str, str, str, str, int], int] = {}
        self._last_bucket: int | None = None
        self._capacity_warned_bucket: int | None = None
        self._inflight = 0
        self.failed_writes = 0
        self.skipped_records = 0

    def _failed(self, reason: str) -> None:
        self.failed_writes += 1
        # Reason/counter only: database exceptions can contain connection
        # credentials, and neither rule text nor credentials belong in logs.
        logger.warning(
            "keystone fetch recording unavailable: %s; failed_writes=%d",
            reason,
            self.failed_writes,
        )

    async def record(
        self,
        *,
        storage: CoreStorageClient,
        auth: AuthContext,
        tenant_id: str,
        principal: str | None,
        fleet_id: str | None,
        rows: list[dict],
        truncated: bool,
    ) -> None:
        if not isinstance(auth, AuthContext) or not auth.is_install_credential:
            return
        if not auth.install_uuid or not auth.credential_id:
            return
        try:
            install = str(UUID(auth.install_uuid))
            auth.enforce_readable_tenant(tenant_id)
            now = self._clock().astimezone(UTC)
            record = FetchRecord(
                tenant_id=tenant_id,
                install_uuid=UUID(install),
                credential_id=auth.credential_id,
                principal=principal,
                fleet_id=fleet_id,
                rule_set_hash=rule_set_hash(rules_from_keystone_rows(rows)),
                served_at=now,
                truncated=truncated,
            )
        except Exception:
            self.skipped_records += 1
            logger.warning("keystone fetch recording unavailable: invalid attribution or rule hash")
            return
        bucket = int(now.timestamp()) // BUCKET_SECONDS
        if self._last_bucket != bucket:
            self._seen = {key: minute for key, minute in self._seen.items() if minute >= bucket}
            self._last_bucket = bucket
        key = (tenant_id, install, principal or "", fleet_id or "", record.rule_set_hash, bucket)
        if key in self._seen:
            return
        if len(self._seen) >= self._max_entries:
            # Evicting a current bucket permits a second write for that key.
            # Refuse new keys until expiry instead; reads continue normally.
            self.skipped_records += 1
            if self._capacity_warned_bucket != bucket:
                logger.warning("keystone fetch recording unavailable: minute key capacity reached")
                self._capacity_warned_bucket = bucket
            return
        self._seen[key] = bucket
        if self._inflight >= self._max_inflight:
            self._failed("inflight_capacity")
            return
        # No await before this increment: a process cannot accumulate waiting
        # requests on a semaphore during a storage outage.
        self._inflight += 1
        try:
            async with asyncio.timeout(self._timeout):
                await storage.record_keystone_fetch(record.model_dump(mode="json"))
        except asyncio.CancelledError:
            self._failed("request_cancelled")
            raise
        except Exception:
            self._failed("write_failed_or_timed_out")
        finally:
            self._inflight -= 1


_recorder = KeystoneFetchRecorder()


def get_fetch_recorder() -> KeystoneFetchRecorder:
    return _recorder
