"""Per-item ``write_mode`` on the bulk write path.

``write_mode="strong"`` asks for the embedding to be generated inline so the row
is searchable as soon as it persists, instead of after the background backfill.
On a deployment that already embeds inline it is a no-op.

The granularity is per ITEM, not per batch, because callers funnel unrelated
writes through one bulk request — the broker's durable queue coalesces them — so
one item opting in must not change how anything batched alongside it is treated.
"""

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import text

from core_api.config import settings
from core_api.constants import (
    BULK_EMBEDDING_TIMEOUT_SECONDS,
    BULK_STRONG_EMBED_TIMEOUT_SECONDS,
)
from core_api.schemas import BulkMemoryCreate, BulkMemoryItem
from core_api.services.memory_service import create_memories_bulk

# Long enough to clear CheckContentLength's minimum-length quality gate.
_PADDING = " This memory carries enough surrounding context to pass the content-length gate."


def _item(content: str, **kw) -> BulkMemoryItem:
    return BulkMemoryItem(content=content + _PADDING, **kw)


def _request(*items: BulkMemoryItem) -> BulkMemoryCreate:
    return BulkMemoryCreate(
        # ``test-tenant-%`` rows are auto-cleaned by the conftest schema fixture.
        # A fresh tenant per call keeps these independent of committed leftovers.
        tenant_id=f"test-tenant-writemode-{uuid.uuid4().hex[:8]}",
        fleet_id="test-fleet",
        agent_id="test-agent",
        items=list(items),
    )


async def _write(*items: BulkMemoryItem):
    """Write a batch, assert every item was created, return the result rows."""
    resp = await create_memories_bulk(_request(*items), bulk_attempt_id=uuid.uuid4().hex)
    assert [r.status for r in resp.results] == ["created"] * len(items), resp.results
    return resp.results


async def _embedded(engine, memory_id) -> bool:
    """True when the stored row has an embedding.

    Read straight from the column: ``get_memory`` omits the embedding (it is
    deliberately excluded from the serialised field set), and ``vec_sim`` would
    conflate "no embedding" with "orthogonal".
    """
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT embedding IS NOT NULL FROM memories WHERE id = :i"),
                {"i": memory_id},
            )
        ).first()
    assert row is not None, f"memory {memory_id} not found"
    return bool(row[0])


@pytest.fixture
def deferring_deployment(monkeypatch):
    """Make the deployment defer embedding, as SaaS does.

    ``settings.inline_embedding`` is a read-only property over
    ``deployment_mode``, so the mode is what has to move.
    """
    monkeypatch.setattr(settings, "deployment_mode", "deferred")
    assert not settings.inline_embedding


@pytest.mark.integration
async def test_strong_item_embeds_inline_when_deployment_defers(_engine, deferring_deployment):
    rows = await _write(_item("Strong write embeds on the request path.", write_mode="strong"))

    assert await _embedded(_engine, rows[0].id), (
        "write_mode='strong' must embed inline even when the deployment defers — "
        "that is the whole point of the opt-in"
    )


@pytest.mark.integration
async def test_omitted_write_mode_still_defers(_engine, deferring_deployment):
    """Control: without the opt-in, nothing about the deferred path changes."""
    rows = await _write(_item("Ordinary write keeps deferring."))

    assert not await _embedded(_engine, rows[0].id)


@pytest.mark.integration
async def test_mixed_batch_embeds_only_the_strong_item(_engine, deferring_deployment):
    """The per-item guarantee: one item's opt-in does not leak onto its neighbours."""
    rows = await _write(
        _item("Deferred neighbour one."),
        _item("The one that opted in.", write_mode="strong"),
        _item("Deferred neighbour two.", write_mode="fast"),
    )

    embedded = [await _embedded(_engine, r.id) for r in rows]
    assert embedded == [False, True, False], (
        f"only the write_mode='strong' item should be embedded inline, got {embedded}"
    )


@pytest.mark.integration
async def test_unknown_write_mode_behaves_as_fast(_engine, deferring_deployment):
    """An unrecognised value must degrade, not 422 the whole batch.

    ``BulkMemoryItem`` deliberately avoids schema constraints that reject every
    item over one item's bad field — 'stm' is valid on the single-write schema and
    a caller reusing that payload shape must not lose the batch for it.
    """
    rows = await _write(
        _item("Copied a single-write payload.", write_mode="stm"),
        _item("Nonsense value.", write_mode="banana"),
    )

    assert [await _embedded(_engine, r.id) for r in rows] == [False, False]


@pytest.mark.integration
async def test_inline_deployment_embeds_everything_regardless(_engine):
    """On a deployment that embeds inline, ``write_mode`` changes nothing."""
    assert settings.inline_embedding, "local default is expected to embed inline"
    rows = await _write(_item("No opt-in here."), _item("Opted in.", write_mode="strong"))

    assert [await _embedded(_engine, r.id) for r in rows] == [True, True]


@pytest.mark.integration
async def test_strong_embed_failure_does_not_fail_the_batch(_engine, deferring_deployment):
    """A failed opt-in embed falls back to the backfill instead of 5xx-ing.

    These rows were going to defer anyway, so one item's opportunistic inline
    embed must not take down a batch containing items that never asked for it.
    """
    with patch(
        "core_api.services.memory_service.get_embeddings_batch",
        side_effect=RuntimeError("provider exploded"),
    ):
        rows = await _write(
            _item("Innocent bystander."),
            _item("Opted in, will fail.", write_mode="strong"),
        )

    # Both fell back to the deferred path; neither is lost.
    assert [await _embedded(_engine, r.id) for r in rows] == [False, False]


@pytest.mark.integration
async def test_inline_deployment_still_fails_loudly_on_embed_error():
    """The pre-existing contract for inline deployments is unchanged.

    There the embed is not opportunistic — it is how every row gets its vector —
    so a failure must surface rather than silently persisting unembedded rows.
    """
    from fastapi import HTTPException

    assert settings.inline_embedding
    with (
        patch(
            "core_api.services.memory_service.get_embeddings_batch",
            side_effect=TimeoutError("too slow"),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await create_memories_bulk(
            _request(_item("Inline deployment, embed fails.")),
            bulk_attempt_id=uuid.uuid4().hex,
        )
    assert exc.value.status_code == 504


@pytest.mark.unit
def test_opportunistic_embed_budget_sits_between_the_gate_and_the_required_cap():
    """The opt-in deadline is bounded on both sides, and neither bound is cosmetic.

    Below the concurrency-gate timeout, a saturated gate stops being attributable
    as backpressure and shows up as an anonymous caller timeout instead. At or
    above the required cap, the opt-in stops being opportunistic — it would spend
    a required embed's full budget to reach an outcome it already accepted for
    free, charged to every other item in the batch.
    """
    from common.embedding.constants import EMBEDDING_GATE_TIMEOUT_SECONDS

    assert EMBEDDING_GATE_TIMEOUT_SECONDS < BULK_STRONG_EMBED_TIMEOUT_SECONDS
    assert BULK_STRONG_EMBED_TIMEOUT_SECONDS < BULK_EMBEDDING_TIMEOUT_SECONDS


@pytest.mark.unit
def test_default_budget_tracks_the_gate_so_raising_it_cannot_break_startup():
    """The default is derived, not fixed, and that is an upgrade-safety property.

    ``EMBEDDING_GATE_TIMEOUT_SECONDS`` is an operator's env var. Against a fixed
    literal, any install that had already raised the gate past it — plausible
    after a gate-saturation incident — would fail the startup ordering check on
    upgrade, fixable only by a code change and redeploy. Deriving the default
    keeps the ordering true however the gate is tuned.
    """
    from core_api.constants import _default_strong_embed_timeout

    required = BULK_EMBEDDING_TIMEOUT_SECONDS
    # Sweeps the band where the full margin does NOT fit under the cap
    # (>= required - margin), which is where a clamp-to-cap lands exactly ON the
    # bound the validator rejects. An earlier version asserted ``got <= required``
    # and stopped at 25.0, so it passed while every gate in [27, 30) produced a
    # config that could not start.
    for gate in (1.0, 5.0, 7.9, 10.0, 25.0, 26.9, 27.0, 28.0, 29.0, 29.99):
        got = _default_strong_embed_timeout(gate, required)
        assert gate < got < required, (
            f"gate={gate} must yield a budget STRICTLY inside (gate, required) — "
            f"that is the bound _validate_timeout_ordering enforces — got {got}"
        )

    # A gate at or above the required cap has no room to sit inside, and is
    # incoherent on its own terms: it would outlive the very embed it gates.
    # Returns the cap so the startup validator reports it, rather than inventing a
    # budget that looks valid.
    assert _default_strong_embed_timeout(required, required) == required
    assert _default_strong_embed_timeout(required + 10.0, required) == required


@pytest.mark.unit
def test_startup_rejects_an_override_that_conflicts_with_the_gate(monkeypatch):
    """The lower bound is enforced, not merely documented.

    With the default derived, this fires only when an operator explicitly sets
    ``BULK_STRONG_EMBED_TIMEOUT_SECONDS`` into conflict with the gate — still
    worth catching at startup rather than letting every gate-saturated strong
    write be reported as a generic embed failure, and consistent with the other
    cross-constant orderings in ``_validate_timeout_ordering``.
    """
    import core_api.config as config_mod

    monkeypatch.setattr(
        "common.embedding.constants.EMBEDDING_GATE_TIMEOUT_SECONDS",
        BULK_STRONG_EMBED_TIMEOUT_SECONDS + 1.0,
    )
    with pytest.raises(ValueError, match="EMBEDDING_GATE_TIMEOUT_SECONDS"):
        config_mod.Settings()


@pytest.mark.unit
def test_budget_margin_leaves_the_gate_room_to_report_itself():
    """The gate ordering above is necessary but not sufficient on its own.

    The embed layer caps the provider call at
    ``budget_s - EMBEDDING_BUDGET_MARGIN_S``, a bound that sits INSIDE the
    window the gate's own timeout lives in. So "gate < budget" can hold while
    the effective cap still lands under the gate.
    """
    from common.embedding.constants import (
        EMBEDDING_BUDGET_MARGIN_S,
        EMBEDDING_GATE_TIMEOUT_SECONDS,
    )

    assert (
        BULK_STRONG_EMBED_TIMEOUT_SECONDS - EMBEDDING_BUDGET_MARGIN_S
        > EMBEDDING_GATE_TIMEOUT_SECONDS
    )


@pytest.mark.unit
def test_startup_rejects_a_margin_that_pre_empts_the_gate(monkeypatch):
    """A margin wide enough to swallow the gate must not start.

    Set it past the headroom and the budget cap always fires first, so a
    gate-saturation event is reported as a generic "embed exceeded its
    budget" and the gate's dedicated warning never runs — silently
    inverting the attribution the gate ordering exists to guarantee.

    Note the gate ordering check alone does NOT catch this: it still passes
    here, because ``EMBEDDING_GATE_TIMEOUT_SECONDS`` is untouched and remains
    below ``BULK_STRONG_EMBED_TIMEOUT_SECONDS``. That is the whole reason
    this is a separate check.
    """
    import core_api.config as config_mod
    from common.embedding.constants import EMBEDDING_GATE_TIMEOUT_SECONDS

    # Just past the headroom: budget - margin lands exactly ON the gate,
    # which the validator rejects (it requires strictly greater).
    margin = BULK_STRONG_EMBED_TIMEOUT_SECONDS - EMBEDDING_GATE_TIMEOUT_SECONDS
    monkeypatch.setattr(
        "common.embedding.constants.EMBEDDING_BUDGET_MARGIN_S", margin
    )
    with pytest.raises(ValueError, match="EMBEDDING_BUDGET_MARGIN_S"):
        config_mod.Settings()


@pytest.mark.unit
def test_startup_rejects_an_override_above_the_required_embed_cap(monkeypatch):
    """Both ends of the bound are enforced, not just the one the gate can break.

    The derived default clamps here, so only an explicit
    ``BULK_STRONG_EMBED_TIMEOUT_SECONDS`` override can exceed the required cap —
    and it must not. An opportunistic embed permitted to run as long as a required
    one stops being opportunistic and can hold a whole batch for that budget on one
    item's opt-in. It would also invalidate the storage-phase additive ordering
    check, which is proved against the embed phase never exceeding
    ``BULK_EMBEDDING_TIMEOUT_SECONDS``.
    """
    import core_api.config as config_mod

    monkeypatch.setattr(
        "core_api.constants.BULK_STRONG_EMBED_TIMEOUT_SECONDS",
        BULK_EMBEDDING_TIMEOUT_SECONDS + 1.0,
    )
    with pytest.raises(ValueError, match="BULK_STRONG_EMBED_TIMEOUT_SECONDS"):
        config_mod.Settings()
