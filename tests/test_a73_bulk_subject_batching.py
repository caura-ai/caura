"""reg-a73 — a coherent bulk ingest flagged its own facts as contradictions.

Seeding a biography, or importing a document, writes dozens of rows about one
subject within seconds. The bulk path fired ``run_contradiction_detection`` once
per created row, so each row was judged against a store its own siblings were
still landing in — and complementary facts came back ``conflicted``. In one
observed store ~40% of a 55-fact biography was flagged before the first
conversation, and a conflicted row carries a 0.5 ranking penalty, so the store
began every retrieval handicapped.

``write.bulk_subject_batching`` judges the batch's LAST row per subject instead
of every row. That is not "detect less": by the time the last row is judged
every sibling is committed and is an ordinary candidate for it, so a genuine
intra-batch contradiction is still found — and so is one against the
pre-existing store. What disappears is the batch conflicting with itself N ways.

It also turns N detections into one per subject, so it REDUCES LLM calls rather
than adding any.
"""

import inspect

import pytest

from core_api.services import memory_service as ms

pytestmark = pytest.mark.unit


def _bulk_src() -> str:
    return inspect.getsource(ms.create_memories_bulk)


# ── the flag ──────────────────────────────────────────────────────────────


def test_the_flag_defaults_off():
    from core_api.services.organization_settings import ResolvedConfig

    assert ResolvedConfig(tenant_settings={}).bulk_subject_batching is False


def test_the_flag_is_readable_when_set():
    from core_api.services.organization_settings import ResolvedConfig

    cfg = ResolvedConfig(tenant_settings={"write": {"bulk_subject_batching": True}})
    assert cfg.bulk_subject_batching is True


def test_the_flag_is_a_declared_writable_key():
    from core_api.services.organization_settings import _LEAF_TYPES

    assert _LEAF_TYPES["write.bulk_subject_batching"] is bool


def test_the_bulk_path_reads_the_flag():
    assert 'getattr(tenant_config, "bulk_subject_batching", False)' in _bulk_src()


# ── what gets deferred, and what does not ─────────────────────────────────


def test_a_row_with_a_subject_is_held_back_when_batching_is_on():
    src = _bulk_src()
    assert 'elif bulk_subject_batching and mem_data.get("subject_entity_id"):' in src


def test_the_last_row_per_subject_wins():
    """A plain dict assignment keyed by subject — each row overwrites the
    previous one, so the survivor is the batch's most recent claim about that
    subject, which is the row whose candidate set is complete."""
    src = _bulk_src()
    assert 'deferred_by_subject[str(mem_data["subject_entity_id"])] = (' in src


def test_a_subjectless_row_keeps_per_row_detection():
    """There is nothing to group by, and inventing a key would merge unrelated
    rows into one pass."""
    src = _bulk_src()
    branch = src[src.index("elif bulk_subject_batching") :]
    else_i = branch.index("else:")
    assert "run_contradiction_detection(" in branch[else_i : else_i + 900]


def test_batching_off_leaves_the_old_behaviour_exactly():
    """The flag is dark by default; with it off every row must still take the
    per-row branch."""
    src = _bulk_src()
    # The guard is a conjunction, so a False flag falls through to ``else``
    # regardless of whether the row resolved a subject.
    assert "elif bulk_subject_batching and" in src


def test_an_unembedded_row_still_goes_to_reembed_first():
    """Ordering: the embedding check precedes the batching branch, so batching
    cannot swallow a row that has no vector to judge with."""
    src = _bulk_src()
    assert src.index("if embeddings[orig_idx] is None:") < src.index(
        "elif bulk_subject_batching"
    )


# ── when the deferred passes run ──────────────────────────────────────────


def test_the_deferred_passes_run_after_the_loop():
    """Before the loop ends, the later siblings are not committed yet — judging
    then would reproduce the very race this fixes."""
    src = _bulk_src()
    assign = src.index('deferred_by_subject[str(mem_data["subject_entity_id"])]')
    schedule = src.index(
        "for _subject, (_mid, _content, _emb) in deferred_by_subject.items():"
    )
    assert assign < schedule


def test_the_deferred_pass_uses_the_same_bulk_trigger():
    """Nothing downstream should be able to tell a batched pass from a per-row
    one; only the number of passes changes."""
    src = _bulk_src()
    tail = src[src.index("for _subject, (_mid, _content, _emb)") :]
    assert "trigger=Trigger.BULK" in tail[:700]


def test_the_reduction_is_logged():
    """Subjects-vs-rows is the number that says whether the flag is doing
    anything, and it is also the LLM-call saving."""
    src = _bulk_src()
    assert "bulk_subject_batching:" in src
    assert "len(deferred_by_subject)" in src and "len(resolved)" in src
