"""Unit tests for ``services.doc_memory`` — minting the doc-derived memory.

Covers:
- The auto-chunk guard: minting is skipped when chunking would fan one doc into
  a parent + N children sharing one ``source_uri``.
- ``safe_sync_doc_memory`` never raises, and treats a 409 from
  ``CheckExactDuplicate`` as the EXPECTED outcome of an idempotent doc
  re-write — logged at info, NOT as an exception. Without this, every no-op doc
  re-sync emits a stack trace.
- Identity resolution: the real doc writer wins; the service fallback is used
  only when the caller has no identity at all, and either way the agent is
  registered (else insights refuses to run for it).
- The ``MemoryCreate`` payload: verbatim content, provenance ``source_uri``,
  ``scope_team``, and ``write_mode="fast"`` (load-bearing — it's what gives
  exact-dedup-without-semantic-reject on rewrites).
- Which fields are shielded from enrichment: ``status`` is pinned ``active``
  (insights selects ``status='active'`` only), while ``memory_type`` is
  deliberately left to the classifier so each document classifies on its own
  content.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from core_api.agent_ids import DOC_INDEXER_AGENT_ID
from core_api.constants import CHUNKING_THRESHOLD_CHARS
from core_api.services import doc_memory
from core_api.services.doc_indexing import DocMemorySpec

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _spec(content: str = "body text") -> DocMemorySpec:
    return DocMemorySpec(
        content=content,
        source_uri="memclaw-doc://runbooks/pg-tuning",
        metadata={"doc_collection": "runbooks", "doc_id": "pg-tuning"},
    )


@pytest.fixture
def patched(monkeypatch):
    """Patch ``create_memory`` / ``resolve_config`` / ``get_or_create_agent``.

    ``sync_doc_memory`` imports these lazily inside the function body, so the
    patch targets are the defining modules rather than ``doc_memory``.
    """
    create_memory = AsyncMock(return_value=SimpleNamespace(id="mem-1"))
    get_or_create_agent = AsyncMock(return_value={"agent_id": DOC_INDEXER_AGENT_ID})

    def _config(auto_chunk_enabled: bool = False):
        async def _resolve(_tenant_id):
            return SimpleNamespace(auto_chunk_enabled=auto_chunk_enabled)

        return _resolve

    monkeypatch.setattr("core_api.services.memory_service.create_memory", create_memory)
    monkeypatch.setattr(
        "core_api.services.agent_service.get_or_create_agent", get_or_create_agent
    )
    monkeypatch.setattr(
        "core_api.services.organization_settings.resolve_config", _config(False)
    )

    return SimpleNamespace(
        create_memory=create_memory,
        get_or_create_agent=get_or_create_agent,
        set_auto_chunk=lambda on: monkeypatch.setattr(
            "core_api.services.organization_settings.resolve_config", _config(on)
        ),
    )


# ── Payload ───────────────────────────────────────────────────────────────────


async def test_creates_memory_with_expected_payload(patched):
    mem_id = await doc_memory.sync_doc_memory(
        _spec("# H\n\nverbatim body"),
        tenant_id="t1",
        fleet_id="f1",
        agent_id="agent-a",
    )

    assert mem_id == "mem-1"
    (payload,) = patched.create_memory.call_args.args
    assert payload.content == "# H\n\nverbatim body"  # verbatim
    assert payload.source_uri == "memclaw-doc://runbooks/pg-tuning"
    assert payload.tenant_id == "t1"
    assert payload.fleet_id == "f1"
    assert payload.agent_id == "agent-a"
    assert payload.visibility == "scope_team"
    assert payload.metadata["doc_collection"] == "runbooks"


async def test_memory_type_is_left_to_the_classifier(patched):
    """A document is not one kind of thing — a decision record classifies as
    ``decision``, a runbook as ``rule``, an incident writeup as ``episode``.

    We must NOT pass ``memory_type``: passing it would put it into
    ``model_fields_set`` -> ``agent_provided_fields`` and shield it from the
    classifier. ``model_fields_set`` is the exact mechanism, so asserting on it
    is asserting the real contract rather than a proxy.
    """
    await doc_memory.sync_doc_memory(
        _spec(), tenant_id="t1", fleet_id=None, agent_id="a"
    )
    (payload,) = patched.create_memory.call_args.args

    assert "memory_type" not in payload.model_fields_set
    assert payload.memory_type is None


async def test_only_status_is_shielded_from_enrichment(patched):
    """The precise contract: of the five enrichment-override fields, doc memories
    pin ``status`` and nothing else."""
    from core_api.services.memory_service import _agent_provided_enrichment_fields

    await doc_memory.sync_doc_memory(
        _spec(), tenant_id="t1", fleet_id=None, agent_id="a"
    )
    (payload,) = patched.create_memory.call_args.args

    assert _agent_provided_enrichment_fields(payload) == ["status"]


async def test_status_is_pinned_active(patched):
    """REGRESSION (wet test, eyal-wet-tests): status must NOT be left to the
    enrichment classifier.

    The enrichment prompt offers "active"|"pending"|"confirmed" and chooses per
    content, while ``insights_query_patterns`` selects ``status == "active"``
    only. A runbook body produced "confirmed", so insights returned
    ``memories_analyzed=0`` and the doc was silently invisible — the feature
    would work or not depending on a non-deterministic per-document choice.
    """
    await doc_memory.sync_doc_memory(
        _spec(), tenant_id="t1", fleet_id=None, agent_id="a"
    )
    (payload,) = patched.create_memory.call_args.args
    assert payload.status == "active"


async def test_status_is_an_enrichment_override_field(patched):
    """The pin above only holds because ``status`` is in the override set — if it
    were dropped, deferred enrichment would silently overwrite it."""
    from core_api.services.memory_service import _ENRICHMENT_AGENT_OVERRIDE_FIELDS

    assert "status" in _ENRICHMENT_AGENT_OVERRIDE_FIELDS


async def test_write_mode_is_fast(patched):
    """Load-bearing: ``fast`` keeps ``CheckExactDuplicate`` (so identical
    rewrites dedupe) while skipping the strong path's ``CheckSemanticDuplicate``
    (which would 409-reject legitimate edits at 0.95)."""
    await doc_memory.sync_doc_memory(
        _spec(), tenant_id="t1", fleet_id=None, agent_id="a"
    )
    (payload,) = patched.create_memory.call_args.args
    assert payload.write_mode == "fast"


# ── Auto-chunk guard ──────────────────────────────────────────────────────────


async def test_skips_when_auto_chunk_would_fire(patched):
    """Chunking writes a parent PLUS N children that all inherit this
    ``source_uri``, so one doc would map to N+1 rows."""
    patched.set_auto_chunk(True)
    over = "y" * (CHUNKING_THRESHOLD_CHARS + 1)

    result = await doc_memory.sync_doc_memory(
        _spec(over), tenant_id="t1", fleet_id=None, agent_id="a"
    )

    assert result is None
    patched.create_memory.assert_not_awaited()


async def test_mints_when_auto_chunk_on_but_body_under_threshold(patched):
    patched.set_auto_chunk(True)
    under = "y" * (CHUNKING_THRESHOLD_CHARS - 1)

    result = await doc_memory.sync_doc_memory(
        _spec(under), tenant_id="t1", fleet_id=None, agent_id="a"
    )

    assert result == "mem-1"
    patched.create_memory.assert_awaited_once()


async def test_mints_large_body_when_auto_chunk_off(patched):
    """Default tenant config: ``auto_chunk_enabled`` is False, so a body over
    the chunk threshold is still one memory."""
    over = "y" * (CHUNKING_THRESHOLD_CHARS + 1)
    result = await doc_memory.sync_doc_memory(
        _spec(over), tenant_id="t1", fleet_id=None, agent_id="a"
    )
    assert result == "mem-1"


# ── Identity ──────────────────────────────────────────────────────────────────


async def test_prefers_the_caller_agent_id(patched):
    """Attribution is not cosmetic: ``memclaw_insights`` defaults to
    ``scope="agent"`` and filters on ``agent_id``, so a service-attributed row
    is invisible to the real agent's default insights run."""
    await doc_memory.sync_doc_memory(
        _spec(), tenant_id="t1", fleet_id=None, agent_id="real-agent"
    )

    (payload,) = patched.create_memory.call_args.args
    assert payload.agent_id == "real-agent"


async def test_registers_the_caller_agent(patched):
    """REGRESSION (wet test, eyal-wet-tests): REST ``POST /documents`` has no
    ``enforce_fleet_write`` step, so without registering here the mint produced a
    memory attributed to an identity with no ``agents`` row — and
    ``memclaw_insights`` then 403s with "Agent X is not registered", leaving the
    minted memory unreachable by the default ``scope="agent"`` pass.
    """
    await doc_memory.sync_doc_memory(
        _spec(), tenant_id="t1", fleet_id="f1", agent_id="real-agent"
    )

    patched.get_or_create_agent.assert_awaited_once()
    args = patched.get_or_create_agent.call_args.args
    assert args[0] == "t1"
    assert args[1] == "real-agent"
    assert args[2] == "f1"


@pytest.mark.parametrize("missing", [None, ""])
async def test_falls_back_to_service_identity_only_when_no_caller(patched, missing):
    await doc_memory.sync_doc_memory(
        _spec(), tenant_id="t1", fleet_id="f1", agent_id=missing
    )

    (payload,) = patched.create_memory.call_args.args
    assert payload.agent_id == DOC_INDEXER_AGENT_ID
    # Self-registered on first use, mirroring memclaw-insighter.
    patched.get_or_create_agent.assert_awaited_once()
    args = patched.get_or_create_agent.call_args.args
    assert args[0] == "t1"
    assert args[1] == DOC_INDEXER_AGENT_ID


# ── safe_sync_doc_memory never raises ─────────────────────────────────────────


async def test_409_is_expected_and_logged_at_info(patched, caplog):
    """An identical doc re-write hits ``CheckExactDuplicate``. That is a no-op,
    not a failure — it must not emit a stack trace."""
    patched.create_memory.side_effect = HTTPException(
        status_code=409, detail="Duplicate memory exists: abc"
    )

    with caplog.at_level(logging.DEBUG, logger="core_api.services.doc_memory"):
        result = await doc_memory.safe_sync_doc_memory(
            _spec(), tenant_id="t1", fleet_id=None, agent_id="a"
        )

    assert result is None
    records = [r for r in caplog.records if r.name == "core_api.services.doc_memory"]
    assert records, "expected a log record"
    assert all(r.levelno <= logging.INFO for r in records)
    assert all(r.exc_info is None for r in records), "409 must not log a traceback"
    assert any("already exists" in r.getMessage() for r in records)


async def test_other_http_errors_are_swallowed_and_warned(patched, caplog):
    patched.create_memory.side_effect = HTTPException(
        status_code=422, detail="bad content"
    )

    with caplog.at_level(logging.DEBUG, logger="core_api.services.doc_memory"):
        result = await doc_memory.safe_sync_doc_memory(
            _spec(), tenant_id="t1", fleet_id=None, agent_id="a"
        )

    assert result is None
    assert any(r.levelno == logging.WARNING for r in caplog.records)


async def test_unexpected_errors_are_swallowed_and_logged_with_traceback(
    patched, caplog
):
    """The document is the source of truth and has already committed — a
    derived-memory failure must never turn a successful write into an error."""
    patched.create_memory.side_effect = RuntimeError("storage exploded")

    with caplog.at_level(logging.DEBUG, logger="core_api.services.doc_memory"):
        result = await doc_memory.safe_sync_doc_memory(
            _spec(), tenant_id="t1", fleet_id=None, agent_id="a"
        )

    assert result is None
    assert any(r.levelno == logging.ERROR and r.exc_info for r in caplog.records)


async def test_safe_wrapper_returns_id_on_success(patched):
    assert (
        await doc_memory.safe_sync_doc_memory(
            _spec(), tenant_id="t1", fleet_id=None, agent_id="a"
        )
        == "mem-1"
    )
