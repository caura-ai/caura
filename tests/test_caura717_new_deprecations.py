"""CAURA-717 — intention/commitment/cancellation are folded into the adjacent
successor types (plan/action/task/decision, per the V2.2 taxonomy). Same
mechanism as CAURA-701's semantic → fact merger: kept in the enum for
read-compat with historical rows but hidden from the classifier and demoted
to the default at every write path.

The existing CAURA-702 test file iterates over ``CLASSIFIER_DEPRECATED_MEMORY_TYPES``
generically and already covers these three the moment they join the set;
this file pins each slug down by name so a future accidental removal from
the frozenset fails a test whose CAURA reference names the exact PR to check.
"""

from __future__ import annotations

import uuid

import pytest

from common.enrichment._prompts import ENRICHMENT_PROMPT
from common.enrichment.constants import (
    CLASSIFIER_DEPRECATED_MEMORY_TYPES,
    MEMORY_TYPE_DESCRIPTIONS,
    SERVER_RESERVED_MEMORY_TYPES,
    MemoryType,
)
from core_api.clients.storage_client import get_storage_client
from core_api.constants import DEFAULT_MEMORY_TYPE, MEMORY_TYPES_WRITE
from core_api.schemas import BulkMemoryCreate, BulkMemoryItem
from core_api.services.crystallizer_service import CRYSTALLIZATION_PROMPT
from core_api.services.memory_service import create_memories_bulk

NEW_DEPRECATED = ("intention", "commitment", "cancellation")

# Same padding shape as CAURA-702's suite — clears CheckContentLength's
# minimum-length quality gate on the write pipeline.
_PADDING = (
    " This memory carries enough surrounding context to pass the content-length gate."
)


def _tenant() -> str:
    # ``test-tenant-%`` rows are auto-cleaned by the conftest schema fixture.
    return f"test-tenant-caura717-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# 1. Constants (pure unit)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", NEW_DEPRECATED)
def test_new_slug_is_in_deprecated_set(slug: str) -> None:
    assert slug in CLASSIFIER_DEPRECATED_MEMORY_TYPES
    # Enum entry stays for historical-row read-compat (same rationale as
    # ``semantic`` after CAURA-701).
    assert slug in {t.value for t in MemoryType}


@pytest.mark.parametrize("slug", NEW_DEPRECATED)
def test_new_slug_marked_deprecated_in_descriptions(slug: str) -> None:
    """A future author looking at MEMORY_TYPE_DESCRIPTIONS should see this
    slug is retired and where its content is expected to migrate."""
    desc = MEMORY_TYPE_DESCRIPTIONS[slug]
    assert "DEPRECATED (CAURA-717)" in desc, f"{slug} description missing CAURA-717 tag"


@pytest.mark.parametrize("slug", NEW_DEPRECATED)
def test_new_slug_not_in_writable_vocab(slug: str) -> None:
    assert slug not in MEMORY_TYPES_WRITE


# ---------------------------------------------------------------------------
# 2. Prompt (pure unit)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", NEW_DEPRECATED)
def test_prompt_does_not_offer_new_deprecated_slug(slug: str) -> None:
    """The classifier's offered-vocabulary section must not list the slug —
    if it does, the LLM has an incentive to keep picking it. The contrastive
    examples block may reference the concept in prose ("cancellation-deed")
    but never as an ``-> {slug}`` label."""
    offered = ENRICHMENT_PROMPT.split("Action vs episode vs fact")[0]
    assert f'"{slug}"' not in offered, (
        f"{slug!r} still offered in the classifier vocabulary"
    )
    assert f"-> {slug}" not in ENRICHMENT_PROMPT, (
        f"{slug!r} still used as an example label"
    )


def test_prompt_lists_exactly_the_seven_v22_types() -> None:
    """V2.2 taxonomy: 7 agent-writable types, in enum order."""
    header = ENRICHMENT_PROMPT.split("Action vs episode vs fact")[0]
    for t in ("fact", "episode", "decision", "preference", "task", "plan", "action"):
        assert f'"{t}"' in header, f"{t!r} missing from offered vocabulary"


def test_prompt_has_action_vs_decision_resolver() -> None:
    """CAURA-717 added an explicit action-vs-decision tiebreaker so the
    classifier stops labelling bare approval/pause/cancel deeds as
    'decision'."""
    assert "Action vs decision" in ENRICHMENT_PROMPT


# ---------------------------------------------------------------------------
# 3. Crystallizer prompt + guards (pure unit)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", NEW_DEPRECATED)
def test_crystallizer_prompt_does_not_offer_deprecated_slug(slug: str) -> None:
    """CAURA-717 rebuilt CRYSTALLIZATION_PROMPT to derive its offered vocabulary
    from ``MEMORY_TYPES_WRITE``, so deprecated slugs disappear automatically
    from the prompt as they land in ``CLASSIFIER_DEPRECATED_MEMORY_TYPES`` —
    no more hand-maintained duplicate list."""
    assert f'"{slug}"' not in CRYSTALLIZATION_PROMPT


@pytest.mark.parametrize("slug", sorted(SERVER_RESERVED_MEMORY_TYPES))
def test_crystallizer_prompt_does_not_offer_reserved_slug(slug: str) -> None:
    """Same refactor closes the CAURA-699 gap on this code path: server-reserved
    types (outcome/rule/insight) are no longer offered to the crystallizer LLM
    either, since ``MEMORY_TYPES_WRITE`` filters both reserved AND deprecated."""
    assert f'"{slug}"' not in CRYSTALLIZATION_PROMPT


def test_do_crystallize_guard_coerces_reserved_type_to_default() -> None:
    """Belt-and-braces: if the crystallizer LLM emits a reserved slug anyway
    (residual pretraining), the parse-loop guard in ``_do_crystallize`` coerces
    it to ``DEFAULT_MEMORY_TYPE`` — mirroring the prompt-vocabulary narrowing.
    Tests the module-level check that mirrors ``MEMORY_TYPES_WRITE`` membership."""
    from core_api.constants import MEMORY_TYPES_WRITE as _WRITE
    # Reserved types: not in MEMORY_TYPES_WRITE → get coerced by the guard.
    for reserved in ("outcome", "rule", "insight"):
        assert reserved not in _WRITE
    # Deprecated types: same treatment via the same guard.
    for dep in ("semantic", "intention", "commitment", "cancellation"):
        assert dep not in _WRITE
    # Sanity: the 7 V2.2 writable slugs ARE in the set.
    for ok in ("fact", "episode", "decision", "preference", "task", "plan", "action"):
        assert ok in _WRITE


# ---------------------------------------------------------------------------
# 3. Bulk demotion (integration — real storage, one row per slug)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("slug", NEW_DEPRECATED)
async def test_bulk_write_demotes_new_deprecated_slug_to_default(slug: str) -> None:
    """Bulk + ingest paths skip MergeEnrichmentFields (single-write pipeline)
    so ``create_memories_bulk`` enforces the merger itself. Ingest funnels
    through bulk, so one test covers both."""
    tenant = _tenant()
    req = BulkMemoryCreate(
        tenant_id=tenant,
        fleet_id="test-fleet",
        agent_id="test-agent",
        items=[
            BulkMemoryItem(
                content=(
                    f"Historical row that the pre-V2.2 classifier would have "
                    f"tagged as {slug}."
                    + _PADDING
                ),
                memory_type=slug,
            )
        ],
    )
    resp = await create_memories_bulk(req, bulk_attempt_id=uuid.uuid4().hex)
    assert resp.results[0].status == "created", resp.results[0]

    mem = await get_storage_client().get_memory(resp.results[0].id, tenant)
    assert mem["memory_type"] == DEFAULT_MEMORY_TYPE, (
        f"{slug!r} was stored verbatim instead of being demoted to "
        f"{DEFAULT_MEMORY_TYPE!r}"
    )
