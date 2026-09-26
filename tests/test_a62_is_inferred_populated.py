"""reg-a62 — a guard that could never fire, because nothing wrote its input.

Migration 036 (A55) added ``memories.is_inferred``. Nothing ever set it, so every
row in every tenant read ``False`` = "directly stated". That silently disabled
the invariant in ``resolution.resolve``::

    if is_inferred and action in _DESTRUCTIVE:

which exists so a memory the SYSTEM materialised cannot destructively overturn
one a user actually stated. With the column permanently False, a crystallized
merge or a generated insight could retire a fact a person wrote, and the branch
guarding against it had never executed once.

``confidence`` is deliberately NOT populated here. The column documents NULL as
"unknown/legacy", and neither writer has an honest per-claim confidence to
report — the crystallizer's ``weight`` is salience, not confidence in the claim.
Writing a number nobody measured would be worse than NULL, because the resolver
would then act on a fabricated signal rather than skip an absent one.
"""

import inspect

import pytest

pytestmark = pytest.mark.unit


# ── it reaches the row ────────────────────────────────────────────────────


def test_the_write_step_persists_the_column():
    from core_api.pipeline.steps.write import write_memory_row as w

    src = inspect.getsource(w)
    assert '"is_inferred": bool(ctx.data.get("is_inferred", False))' in src


def test_it_defaults_to_explicit():
    """Absent means "a person stated this" — the safe reading, and what every
    ordinary write is."""
    from core_api.pipeline.steps.write import write_memory_row as w

    assert 'ctx.data.get("is_inferred", False)' in inspect.getsource(w)


def test_the_pipeline_runner_seeds_it():
    from core_api.services import memory_service as ms

    src = inspect.getsource(ms._run_write_pipeline)
    assert src.count('"is_inferred": is_inferred') == 2, (
        "both persisting pipeline contexts must carry it"
    )


def test_the_stm_path_is_not_seeded():
    """STM writes through ``WriteSTMNote``, not ``WriteMemoryRow`` — there is no
    row with this column to set, so seeding it there would be cargo cult."""
    from core_api.services import memory_service as ms

    src = inspect.getsource(ms._run_write_pipeline)
    stm = src[
        src.index("build_stm_write_pipeline()") - 600 : src.index(
            "build_stm_write_pipeline()"
        )
    ]
    assert '"is_inferred"' not in stm


# ── it cannot be claimed by a caller ──────────────────────────────────────


def test_it_is_keyword_only_and_internal_on_the_single_write():
    from core_api.services.memory_service import create_memory

    p = inspect.signature(create_memory).parameters["is_inferred"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY
    assert p.default is False


def test_it_is_keyword_only_and_internal_on_the_bulk_write():
    from core_api.services.memory_service import create_memories_bulk

    p = inspect.signature(create_memories_bulk).parameters["is_inferred"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY
    assert p.default is False


def test_it_is_absent_from_the_public_create_schemas():
    """The whole point of the invariant is that the writer does not choose which
    side of it they sit on. On the wire it would be an opt-out, not a guard."""
    from core_api.schemas import MemoryCreate

    assert "is_inferred" not in MemoryCreate.model_fields


def test_it_stays_visible_on_read():
    """Readers still need to know — the resolver reads it off the fetched row."""
    from core_api.schemas import MemoryOut

    assert "is_inferred" in MemoryOut.model_fields


# ── the two writers that materialise memories ─────────────────────────────


def test_the_crystallizer_marks_its_facts_inferred():
    """A crystallized fact is an LLM re-extraction that merges a cluster into a
    claim nobody stated in those words."""
    from core_api.services import crystallizer_service as cs

    src = inspect.getsource(cs)
    i = src.index('agent_id="crystallizer"')
    assert "is_inferred=True" in src[i : i + 900]


def test_insights_mark_their_batch_inferred():
    """An insight is the system's own conclusion drawn ACROSS memories."""
    from core_api.services import insights_service as ins

    src = inspect.getsource(ins)
    assert "is_inferred=True" in src


def test_the_atomic_fact_fanout_is_not_marked_inferred():
    """Deliberate: a fan-out child is a claim EXTRACTED from content the user
    wrote, not one the system inferred. Marking it would wrongly demote a
    user-stated fact below every other user-stated fact."""
    from core_api.services import memory_service as ms

    src = inspect.getsource(ms.fan_out_atomic_facts)
    assert "is_inferred" not in src


# ── the invariant this restores ───────────────────────────────────────────


def test_the_guard_it_feeds_still_exists():
    """If this branch is ever removed, populating the column stops meaning
    anything and this test should be the thing that notices."""
    from core_api.services.contradiction import resolution

    src = inspect.getsource(resolution.resolve)
    assert "if is_inferred and action in _DESTRUCTIVE:" in src


def test_confidence_is_left_null_on_purpose():
    """Neither writer has an honest per-claim confidence; the column documents
    NULL as unknown. A fabricated number would make the resolver act on a signal
    nobody measured."""
    from core_api.pipeline.steps.write import write_memory_row as w

    assert '"confidence"' not in inspect.getsource(w)
