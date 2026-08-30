"""CAURA-716 — the inline enrichment path must honour ``agent_provided_fields``.

Background
----------
``_schedule_enrich_or_inline`` computes ``agent_provided_fields`` (via
``_agent_provided_enrichment_fields`` → Pydantic ``model_fields_set``) and hands
it to the deferred/worker path, but used to DROP it on the inline/OSS branch. The
inline task instead inferred caller intent by comparing the row's current value
against the schema default::

    if mem.get("memory_type") == "fact" and enrichment.memory_type: ...
    if mem.get("status")      == "active" and enrichment.status:      ...
    if mem.get("weight")      == 0.5     and enrichment.weight:       ...

That comparison cannot distinguish "the caller pinned this to the default" from
"the caller said nothing" — so a write passing ``memory_type="fact",
status="active"`` was silently rewritten to the LLM's classification seconds
later. ``fact`` / ``active`` are the defaults precisely because they are the
neutral choices, which is exactly why a caller pins them.

These tests pin the contract at both layers: the argument is forwarded, and the
gate honours it. The legacy default-comparison is retained for callers that pass
no list, so the no-list cases below assert unchanged behaviour.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core_api.services import memory_service

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

TENANT = "t-716"


def _enrichment(**over):
    """An enrichment result that wants to change every override field."""
    base = dict(
        memory_type="decision",
        weight=0.9,
        status="confirmed",
        title="Enriched title",
        summary="",
        tags=[],
        llm_ms=12,
        contains_pii=False,
        pii_types=[],
        retrieval_hint="",
        ts_valid_start=None,
        ts_valid_end=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _row(**over):
    """A stored row as ``get_memory`` returns it — all defaults."""
    base = dict(
        id=str(uuid.uuid4()),
        memory_type="fact",  # schema default
        status="active",  # schema default
        weight=0.5,  # schema default
        ts_valid_start=None,
        ts_valid_end=None,
        metadata_={},
        deleted_at=None,
        fleet_id="f1",
        embedding=None,
        content="body",
    )
    base.update(over)
    return base


async def _run(agent_provided_fields, *, row=None, enrichment=None):
    """Invoke the inline task and capture what it patched.

    Returns ``(patch_dict, status_calls)`` — status is applied through
    ``update_memory_status`` rather than the generic patch, so it is captured
    separately.
    """
    # AsyncMock so every storage method is awaitable — the function under test
    # touches several, and a plain MagicMock would fail on the first await of
    # one this harness didn't anticipate.
    sc = AsyncMock(name="storage_client")
    sc.get_memory = AsyncMock(return_value=row or _row())
    # Real contract (see ``_enrich_memory_background``): non-status fields go
    # through ``update_memory(id, tenant_id, patch)``; status goes through
    # ``update_memory_status(id, status, tenant_id=...)``.
    sc.update_memory = AsyncMock(return_value=None)
    sc.update_memory_status = AsyncMock(return_value=None)

    with (
        patch.object(memory_service, "get_storage_client", lambda: sc),
        patch.object(memory_service, "track_task", MagicMock()),
        patch(
            "core_api.services.memory_enrichment.enrich_memory",
            new=AsyncMock(return_value=enrichment or _enrichment()),
        ),
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    enrichment_enabled=True,
                    enrichment_provider="fake",
                    entity_extraction_enabled=False,
                )
            ),
        ),
    ):
        kwargs = {}
        if agent_provided_fields is not _SENTINEL:
            kwargs["agent_provided_fields"] = agent_provided_fields
        await memory_service._enrich_memory_background(
            uuid.uuid4(), "body", TENANT, "f1", "a", **kwargs
        )

    applied: dict = {}
    for call in sc.update_memory.await_args_list:
        for arg in list(call.args) + list(call.kwargs.values()):
            if isinstance(arg, dict):
                applied.update(arg)
    statuses = [
        c.args[1] if len(c.args) > 1 else c.kwargs.get("status")
        for c in sc.update_memory_status.await_args_list
    ]
    return applied, statuses


_SENTINEL = object()


# ── The regression: pinning a field to its own default ────────────────────────


async def test_pinned_memory_type_survives_even_when_equal_to_default():
    """The bug: caller passes ``memory_type="fact"`` — the default — and the
    value-vs-default gate read that as "not set" and overwrote it."""
    applied, _ = await _run(["memory_type"])
    assert "memory_type" not in applied, (
        "enrichment overwrote a caller-pinned memory_type"
    )


async def test_pinned_status_survives_even_when_equal_to_default():
    """Same bug for ``status="active"``. This is the one that made doc-derived
    memories invisible to ``insights focus=patterns`` (which selects
    ``status='active'`` only)."""
    _, statuses = await _run(["status"])
    assert statuses == [], f"enrichment overwrote a caller-pinned status: {statuses}"


async def test_pinned_weight_survives_even_when_equal_to_default():
    applied, _ = await _run(["weight"])
    assert "weight" not in applied


async def test_pinning_both_fields_together():
    """The real doc-memory call site pins memory_type AND status."""
    applied, statuses = await _run(["memory_type", "status"])
    assert "memory_type" not in applied
    assert statuses == []
    # weight was NOT pinned, so enrichment still owns it.
    assert applied.get("weight") == 0.9


# ── Unpinned fields are still enriched ───────────────────────────────────────


async def test_unpinned_fields_are_still_enriched():
    """An empty list means "caller pinned nothing" — enrichment owns everything."""
    applied, statuses = await _run([])
    assert applied.get("memory_type") == "decision"
    assert applied.get("weight") == 0.9
    assert statuses == ["confirmed"]


async def test_title_is_always_enriched_regardless_of_pins():
    """``title`` is not an override field — enrichment always owns it."""
    applied, _ = await _run(["memory_type", "status", "weight"])
    assert applied.get("title") == "Enriched title"


# ── Legacy fallback: no list passed (behaviour must not change) ───────────────


async def test_no_list_falls_back_to_default_comparison_and_enriches():
    """Callers that pass no list keep the pre-CAURA-716 behaviour: a row still
    holding the defaults gets enriched."""
    applied, statuses = await _run(_SENTINEL)
    assert applied.get("memory_type") == "decision"
    assert applied.get("weight") == 0.9
    assert statuses == ["confirmed"]


async def test_no_list_still_respects_a_non_default_row_value():
    """The legacy gate's one working case: a row whose value already differs from
    the default is left alone. Must keep working."""
    applied, statuses = await _run(
        _SENTINEL, row=_row(memory_type="rule", status="pending", weight=0.8)
    )
    assert "memory_type" not in applied
    assert "weight" not in applied
    assert statuses == []


async def test_explicit_none_behaves_like_no_list():
    """``None`` means "no information" — same legacy path, not "pin nothing"."""
    applied, statuses = await _run(None)
    assert applied.get("memory_type") == "decision"
    assert statuses == ["confirmed"]


# ── Timestamps route through the same gate ───────────────────────────────────


async def test_pinned_timestamps_are_not_overwritten():
    from datetime import UTC, datetime

    enr = _enrichment(
        ts_valid_start=datetime(2026, 1, 1, tzinfo=UTC),
        ts_valid_end=datetime(2026, 2, 1, tzinfo=UTC),
    )
    applied, _ = await _run(["ts_valid_start", "ts_valid_end"], enrichment=enr)
    assert "ts_valid_start" not in applied
    assert "ts_valid_end" not in applied


async def test_unpinned_timestamps_are_resolved():
    from datetime import UTC, datetime

    enr = _enrichment(ts_valid_start=datetime(2026, 1, 1, tzinfo=UTC))
    applied, _ = await _run([], enrichment=enr)
    assert applied.get("ts_valid_start") == datetime(2026, 1, 1, tzinfo=UTC)


# ── The argument is actually forwarded (the drop was the bug) ─────────────────


async def test_schedule_enrich_forwards_the_list_to_the_inline_path():
    """The list was computed correctly and then dropped on the inline branch.
    This asserts the wiring, not the gate."""
    seen: dict = {}

    async def _capture(*args, **kwargs):
        seen.update(kwargs)

    with (
        patch.object(memory_service, "_enrich_memory_background", new=_capture),
        patch.object(memory_service.settings, "deployment_mode", "inline"),
    ):
        await memory_service._schedule_enrich_or_inline(
            uuid.uuid4(),
            "body",
            TENANT,
            "f1",
            "a",
            SimpleNamespace(enrichment_enabled=True),
            agent_provided_fields=["memory_type", "status"],
        )

    assert seen.get("agent_provided_fields") == ["memory_type", "status"]


async def test_deferred_path_still_receives_the_list():
    """The deferred/worker branch already worked — don't regress it."""
    seen: dict = {}

    async def _capture_publish(**kwargs):
        seen.update(kwargs)

    with (
        patch.object(
            memory_service, "publish_memory_enrich_request", new=_capture_publish
        ),
        patch.object(memory_service.settings, "deployment_mode", "deferred"),
    ):
        await memory_service._schedule_enrich_or_inline(
            uuid.uuid4(),
            "body",
            TENANT,
            "f1",
            "a",
            SimpleNamespace(enrichment_enabled=True),
            agent_provided_fields=["status"],
        )

    assert seen.get("agent_provided_fields") == ["status"]


# ── The mechanism the fix depends on ─────────────────────────────────────────


async def test_model_fields_set_captures_a_pin_to_default():
    """``_agent_provided_enrichment_fields`` is what makes the fix possible: it
    records the pin even when the value equals the default, which the
    value-comparison provably cannot."""
    from core_api.schemas import MemoryCreate

    m = MemoryCreate(
        tenant_id=TENANT,
        agent_id="a",
        content="body",
        memory_type="fact",  # == default
        status="active",  # == default
    )
    assert memory_service._agent_provided_enrichment_fields(m) == [
        "memory_type",
        "status",
    ]
