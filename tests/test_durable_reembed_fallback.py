"""Batch-failure re-embed fallback must be DURABLE in deferred mode.

Regression cover for the 2026-07-27 incident tail. When a bulk re-embed
batch call fails, the per-item fallback used to retry in-process:
50 items x _REEMBED_MAX_RETRIES x EMBEDDING_RETRY_ATTEMPTS provider calls,
all contending for the same already-saturated backend. When they
exhausted, the rows stayed ``embedding=NULL`` with no further recovery —
only a manual CLI backfill. That stranded ~430 memories.

Routing the fallback through ``_schedule_embed_or_reembed`` moves the
retry onto Pub/Sub (redelivery + DLQ) in deferred mode, while inline mode
keeps in-process retry plus its thundering-herd backoff.

Unit tests validate:
  - Deferred mode publishes EMBED_REQUESTED instead of retrying in-process
  - Inline mode still retries in-process, and still gets the backoff flag
"""

from unittest.mock import AsyncMock, PropertyMock, patch
from uuid import uuid4

import pytest

from core_api.services import memory_service


@pytest.mark.unit
class TestDurableReembedFallback:
    """The fallback's retry must outlive this process in deferred mode."""

    @pytest.mark.asyncio
    async def test_deferred_mode_publishes_instead_of_retrying_inline(self):
        """Deferred mode hands off to Pub/Sub — no in-process embed retry."""
        memory_id, content, tenant_id = uuid4(), "hello", "tenant-a"

        with (
            patch.object(
                type(memory_service.settings),
                "inline_embedding",
                new_callable=PropertyMock,
                return_value=False,
            ),
            patch.object(memory_service, "publish_memory_embed_request", new=AsyncMock()) as publish,
            patch.object(memory_service, "_reembed_memory", new=AsyncMock()) as reembed,
        ):
            await memory_service._schedule_embed_or_reembed(
                memory_id, content, tenant_id, is_failure_fallback=True
            )

        publish.assert_awaited_once()
        assert publish.await_args.kwargs["memory_id"] == memory_id
        assert publish.await_args.kwargs["tenant_id"] == tenant_id
        # The whole point: the retry does NOT stay in this process.
        reembed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_inline_mode_keeps_in_process_retry_with_backoff(self):
        """Inline mode (OSS, no worker fleet) must not lose its backoff."""
        memory_id, content, tenant_id = uuid4(), "hello", "tenant-b"

        with (
            patch.object(
                type(memory_service.settings),
                "inline_embedding",
                new_callable=PropertyMock,
                return_value=True,
            ),
            patch.object(memory_service, "publish_memory_embed_request", new=AsyncMock()) as publish,
            patch.object(memory_service, "_reembed_memory", new=AsyncMock()) as reembed,
        ):
            await memory_service._schedule_embed_or_reembed(
                memory_id, content, tenant_id, is_failure_fallback=True
            )

        reembed.assert_awaited_once()
        # Without this flag the inline retries stampede an already-failing
        # provider with zero delay.
        assert reembed.await_args.kwargs["is_failure_fallback"] is True
        publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_default_is_not_a_failure_fallback(self):
        """The hot-path offload must NOT inherit the 30s backoff."""
        with (
            patch.object(
                type(memory_service.settings),
                "inline_embedding",
                new_callable=PropertyMock,
                return_value=True,
            ),
            patch.object(memory_service, "_reembed_memory", new=AsyncMock()) as reembed,
        ):
            await memory_service._schedule_embed_or_reembed(uuid4(), "hello", "tenant-c")

        assert reembed.await_args.kwargs["is_failure_fallback"] is False
