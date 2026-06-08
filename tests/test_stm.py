"""Tests for the STM (Short-Term Memory) layer."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core_api.schemas import MemoryCreate, STMWriteResponse


# ---------------------------------------------------------------------------
# STM pipeline (write_mode="stm")
# ---------------------------------------------------------------------------


class TestSTMWritePipeline:
    """Verify the STM write pipeline runs 3 steps and produces STMWriteResponse."""

    @pytest.mark.asyncio
    async def test_stm_pipeline_notes(self):
        """STM pipeline routes scope_agent to notes."""
        from core_api.pipeline.compositions.write import build_stm_write_pipeline
        from core_api.pipeline.context import PipelineContext
        import time

        # Reset singleton so we get a fresh InMemorySTM
        import core_api.services.stm_service as svc

        svc._stm_instance = None

        data = MemoryCreate(
            tenant_id="t1",
            agent_id="agent-1",
            content="test note content",
            visibility="scope_agent",
            write_mode="stm",
        )
        ctx = PipelineContext(
            db=None,
            data={"input": data, "t0": time.perf_counter()},
        )
        pipeline = build_stm_write_pipeline()
        result = await pipeline.run(ctx)

        assert not result.failed
        assert result.step_count == 3
        resp = ctx.data["stm_response"]
        assert isinstance(resp, STMWriteResponse)
        assert resp.target == "notes"
        assert resp.write_mode == "stm"
        assert resp.tenant_id == "t1"
        assert resp.agent_id == "agent-1"

    @pytest.mark.asyncio
    async def test_stm_pipeline_bulletin(self):
        """STM pipeline routes scope_team to bulletin."""
        from core_api.pipeline.compositions.write import build_stm_write_pipeline
        from core_api.pipeline.context import PipelineContext
        import time

        import core_api.services.stm_service as svc

        svc._stm_instance = None

        data = MemoryCreate(
            tenant_id="t1",
            agent_id="agent-1",
            content="team note content",
            fleet_id="fleet-1",
            visibility="scope_team",
            write_mode="stm",
        )
        ctx = PipelineContext(
            db=None,
            data={"input": data, "t0": time.perf_counter()},
        )
        pipeline = build_stm_write_pipeline()
        result = await pipeline.run(ctx)

        assert not result.failed
        resp = ctx.data["stm_response"]
        assert resp.target == "bulletin"

    @pytest.mark.asyncio
    async def test_stm_pipeline_no_db(self):
        """STM pipeline works with db=None."""
        from core_api.pipeline.compositions.write import build_stm_write_pipeline
        from core_api.pipeline.context import PipelineContext
        import time

        import core_api.services.stm_service as svc

        svc._stm_instance = None

        data = MemoryCreate(
            tenant_id="t1",
            agent_id="a1",
            content="no db needed",
            write_mode="stm",
        )
        ctx = PipelineContext(
            db=None,
            data={"input": data, "t0": time.perf_counter()},
        )
        pipeline = build_stm_write_pipeline()
        result = await pipeline.run(ctx)
        assert not result.failed
        assert ctx.db is None

    @pytest.mark.asyncio
    async def test_stm_rejects_short_content(self):
        """STM pipeline still enforces minimum content length."""
        from core_api.pipeline.compositions.write import build_stm_write_pipeline
        from core_api.pipeline.context import PipelineContext
        from fastapi import HTTPException
        import time

        import core_api.services.stm_service as svc

        svc._stm_instance = None

        data = MemoryCreate(
            tenant_id="t1",
            agent_id="a1",
            content="hi",
            write_mode="stm",
        )
        ctx = PipelineContext(
            db=None,
            data={"input": data, "t0": time.perf_counter()},
        )
        pipeline = build_stm_write_pipeline()
        with pytest.raises(HTTPException) as exc_info:
            await pipeline.run(ctx)
        assert exc_info.value.status_code == 422


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------


class TestSTMFeatureFlag:
    """Verify STM is gated by use_stm config."""

    @pytest.mark.asyncio
    async def test_stm_disabled_returns_422(self):
        """write_mode=stm with use_stm=False raises 422."""
        from fastapi import HTTPException

        from core_api.config import settings

        original = settings.use_stm
        try:
            settings.use_stm = False
            if not settings.use_stm:
                with pytest.raises(HTTPException):
                    raise HTTPException(status_code=422, detail="STM is not enabled")
        finally:
            settings.use_stm = original


# ---------------------------------------------------------------------------
# STM service
# ---------------------------------------------------------------------------


class TestSTMService:
    """Verify STM service orchestration."""

    @pytest.mark.asyncio
    async def test_read_notes_empty(self):
        import core_api.services.stm_service as svc

        svc._stm_instance = None
        notes = await svc.read_notes("t1", "agent-1")
        assert notes == []

    @pytest.mark.asyncio
    async def test_read_bulletin_empty(self):
        import core_api.services.stm_service as svc

        svc._stm_instance = None
        bulletin = await svc.read_bulletin("t1", "fleet-1")
        assert bulletin == []

    @pytest.mark.asyncio
    async def test_write_then_read_notes(self):
        """Write via pipeline, then read via service."""
        from core_api.pipeline.compositions.write import build_stm_write_pipeline
        from core_api.pipeline.context import PipelineContext
        import time
        import core_api.services.stm_service as svc

        svc._stm_instance = None

        data = MemoryCreate(
            tenant_id="t1",
            agent_id="agent-1",
            content="service test note",
            visibility="scope_agent",
            write_mode="stm",
        )
        ctx = PipelineContext(
            db=None,
            data={"input": data, "t0": time.perf_counter()},
        )
        await build_stm_write_pipeline().run(ctx)

        notes = await svc.read_notes("t1", "agent-1")
        assert len(notes) == 1
        assert notes[0]["content"] == "service test note"

    @pytest.mark.asyncio
    async def test_clear_notes(self):
        import core_api.services.stm_service as svc

        svc._stm_instance = None

        stm = svc.get_stm_backend_instance()
        await stm.post_note("t1", "agent-1", {"content": "temp"})
        await svc.clear_notes("t1", "agent-1")
        notes = await svc.read_notes("t1", "agent-1")
        assert notes == []

    @pytest.mark.asyncio
    async def test_visibility_routing_scope_agent(self):
        """scope_agent routes to notes."""
        from core_api.pipeline.steps.write.resolve_stm_target import ResolveSTMTarget
        from core_api.pipeline.context import PipelineContext

        data = MemoryCreate(
            tenant_id="t1",
            agent_id="a1",
            content="private note",
            visibility="scope_agent",
            write_mode="stm",
        )
        ctx = PipelineContext(db=None, data={"input": data})
        await ResolveSTMTarget().execute(ctx)
        assert ctx.data["stm_target"] == "notes"

    @pytest.mark.asyncio
    async def test_visibility_routing_scope_team(self):
        """scope_team routes to bulletin."""
        from core_api.pipeline.steps.write.resolve_stm_target import ResolveSTMTarget
        from core_api.pipeline.context import PipelineContext

        data = MemoryCreate(
            tenant_id="t1",
            agent_id="a1",
            content="team note",
            fleet_id="fleet-1",
            visibility="scope_team",
            write_mode="stm",
        )
        ctx = PipelineContext(db=None, data={"input": data})
        await ResolveSTMTarget().execute(ctx)
        assert ctx.data["stm_target"] == "bulletin"
        assert ctx.data["stm_fleet_id"] == "fleet-1"

    @pytest.mark.asyncio
    async def test_visibility_routing_scope_org(self):
        """scope_org also routes to bulletin."""
        from core_api.pipeline.steps.write.resolve_stm_target import ResolveSTMTarget
        from core_api.pipeline.context import PipelineContext

        data = MemoryCreate(
            tenant_id="t1",
            agent_id="a1",
            content="org note",
            fleet_id="fleet-1",
            visibility="scope_org",
            write_mode="stm",
        )
        ctx = PipelineContext(db=None, data={"input": data})
        await ResolveSTMTarget().execute(ctx)
        assert ctx.data["stm_target"] == "bulletin"


# ---------------------------------------------------------------------------
# Search injection
# ---------------------------------------------------------------------------


class TestInjectSTMContext:
    """Verify STM entries are injected into search results."""

    @pytest.mark.asyncio
    async def test_injects_notes_into_results(self):
        from core_api.pipeline.steps.search.inject_stm_context import InjectSTMContext
        from core_api.pipeline.context import PipelineContext
        import core_api.services.stm_service as svc

        svc._stm_instance = None

        stm = svc.get_stm_backend_instance()
        await stm.post_note(
            "t1",
            "agent-1",
            {
                "id": "note-1",
                "agent_id": "agent-1",
                "content": "STM note content",
                "memory_type": "fact",
                "metadata": {},
                "posted_at": "2026-04-07T14:00:00Z",
            },
        )

        ctx = PipelineContext(
            db=None,
            data={
                "tenant_id": "t1",
                "caller_agent_id": "agent-1",
                "fleet_ids": None,
                "results": [],
            },
        )

        with patch("core_api.config.settings") as mock_settings:
            mock_settings.use_stm = True
            step = InjectSTMContext()
            await step.execute(ctx)

        results = ctx.data["results"]
        assert len(results) == 1
        assert results[0].content == "STM note content"
        assert results[0].metadata["source"] == "stm"
        assert results[0].metadata["stm_target"] == "notes"
        assert results[0].memory_type == "stm"
        assert results[0].similarity == 1.0

    @pytest.mark.asyncio
    async def test_skips_when_disabled(self):
        from core_api.pipeline.steps.search.inject_stm_context import InjectSTMContext
        from core_api.pipeline.context import PipelineContext
        from core_api.pipeline.step import StepOutcome

        ctx = PipelineContext(
            db=None,
            data={"tenant_id": "t1", "results": []},
        )

        with patch("core_api.config.settings") as mock_settings:
            mock_settings.use_stm = False
            step = InjectSTMContext()
            result = await step.execute(ctx)

        assert result is not None
        assert result.outcome == StepOutcome.SKIPPED

    @pytest.mark.asyncio
    async def test_stm_entries_prepended_before_ltm(self):
        """STM entries appear before existing LTM results."""
        from core_api.pipeline.steps.search.inject_stm_context import InjectSTMContext
        from core_api.pipeline.context import PipelineContext
        from core_api.schemas import MemoryOut
        from datetime import datetime, timezone
        from uuid import uuid4
        import core_api.services.stm_service as svc

        svc._stm_instance = None

        stm = svc.get_stm_backend_instance()
        await stm.post_note(
            "t1",
            "agent-1",
            {
                "id": "stm-1",
                "agent_id": "agent-1",
                "content": "STM result",
                "memory_type": "fact",
                "metadata": {},
                "posted_at": "2026-04-07T14:00:00Z",
            },
        )

        ltm_result = MemoryOut(
            id=uuid4(),
            tenant_id="t1",
            agent_id="agent-1",
            memory_type="fact",
            content="LTM result",
            weight=0.8,
            source_uri=None,
            run_id=None,
            metadata=None,
            created_at=datetime.now(timezone.utc),
            expires_at=None,
        )

        ctx = PipelineContext(
            db=None,
            data={
                "tenant_id": "t1",
                "caller_agent_id": "agent-1",
                "fleet_ids": None,
                "results": [ltm_result],
            },
        )

        with patch("core_api.config.settings") as mock_settings:
            mock_settings.use_stm = True
            await InjectSTMContext().execute(ctx)

        results = ctx.data["results"]
        assert len(results) == 2
        assert results[0].metadata["source"] == "stm"  # STM first
        assert results[1].content == "LTM result"  # LTM second


# ---------------------------------------------------------------------------
# RedisSTM (mocked)
# ---------------------------------------------------------------------------


class TestRedisSTM:
    """Verify RedisSTM key patterns and operations with mocked Redis."""

    @pytest.mark.asyncio
    async def test_key_pattern_notes(self):
        """Notes use key pattern stm:notes:{tenant_id}:{agent_id}."""
        from core_api.providers.redis_stm import RedisSTM

        mock_redis = AsyncMock()
        mock_redis.lrange = AsyncMock(return_value=[])
        mock_pipe = AsyncMock()
        mock_pipe.lpush = MagicMock(return_value=mock_pipe)
        mock_pipe.ltrim = MagicMock(return_value=mock_pipe)
        mock_pipe.expire = MagicMock(return_value=mock_pipe)
        mock_pipe.execute = AsyncMock(return_value=[])
        mock_redis.pipeline = MagicMock(return_value=mock_pipe)

        stm = RedisSTM()
        with patch.object(RedisSTM, "_redis", return_value=mock_redis):
            await stm.post_note("tenant-1", "agent-1", {"content": "hello"})

        mock_pipe.lpush.assert_called_once()
        key_arg = mock_pipe.lpush.call_args[0][0]
        assert key_arg == "stm:notes:tenant-1:agent-1"

    @pytest.mark.asyncio
    async def test_key_pattern_bulletin(self):
        """Bulletin uses key pattern stm:bul:{tenant_id}:{fleet_id}."""
        from core_api.providers.redis_stm import RedisSTM

        mock_redis = AsyncMock()
        mock_pipe = AsyncMock()
        mock_pipe.lpush = MagicMock(return_value=mock_pipe)
        mock_pipe.ltrim = MagicMock(return_value=mock_pipe)
        mock_pipe.expire = MagicMock(return_value=mock_pipe)
        mock_pipe.execute = AsyncMock(return_value=[])
        mock_redis.pipeline = MagicMock(return_value=mock_pipe)

        stm = RedisSTM()
        with patch.object(RedisSTM, "_redis", return_value=mock_redis):
            await stm.post_bulletin("tenant-1", "fleet-1", {"content": "team msg"})

        key_arg = mock_pipe.lpush.call_args[0][0]
        assert key_arg == "stm:bul:tenant-1:fleet-1"

    @pytest.mark.asyncio
    async def test_graceful_fallback_on_redis_failure(self):
        """RedisSTM returns empty list when Redis is unavailable."""
        from core_api.providers.redis_stm import RedisSTM

        stm = RedisSTM()
        with patch.object(RedisSTM, "_redis", return_value=None):
            notes = await stm.get_notes("t1", "a1")
            assert notes == []
            bulletin = await stm.get_bulletin("t1", "f1")
            assert bulletin == []


# ---------------------------------------------------------------------------
# Resolve write mode
# ---------------------------------------------------------------------------


class TestResolveWriteMode:
    """Verify _resolve_write_mode passes 'stm' through."""

    def test_stm_passthrough(self):
        from core_api.services.memory_service import _resolve_write_mode

        data = MemoryCreate(
            tenant_id="t1",
            agent_id="a1",
            content="test content",
            write_mode="stm",
        )
        result = _resolve_write_mode(data, MagicMock(default_write_mode="fast"))
        assert result == "stm"
