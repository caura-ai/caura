"""Two unrelated low-severity findings, grouped only by size.

oss-0902-l-29: the Path C retraction wrote the literal "active" over whatever
  status the candidate held at that moment, including one this path never set.

oss-0814-l-48: three entity-linking pipeline builders with no reference anywhere
  in the repo.

(oss-0902-l-09, the interviewer CLI clamp, is covered in the client package's
own suite — ``clients/python/tests/test_interviewer_cli_clamp.py`` — because
``caura_client`` is a separate distribution and is not importable from here.)
"""

import inspect

import pytest

pytestmark = pytest.mark.unit


# ── oss-0902-l-29 ─────────────────────────────────────────────────────────


def _revert_block() -> str:
    from core_api.services import contradiction_detector as cd

    src = inspect.getsource(cd._attempt_entity_retraction)
    i = src.index('cand_status = candidate.get("status")')
    return src[i : i + 900]


def test_revert_only_touches_a_status_detection_set():
    """Detection marks a loser "outdated" or "conflicted". Anything else means
    another writer moved the row, and stamping "active" over that undoes their
    decision to undo ours."""
    b = _revert_block()
    assert 'if cand_status in ("outdated", "conflicted"):' in b


def test_the_unconditional_write_is_gone():
    from core_api.services import contradiction_detector as cd

    src = inspect.getsource(cd._attempt_entity_retraction)
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert (
        'update_memory_status(str(candidate.get("id")), "active", tenant_id=cand_tenant)'
        in code
    )
    # ...but only inside the guard, never at the function's own indent level.
    for line in code.splitlines():
        if "update_memory_status(str(candidate.get" in line:
            assert line.startswith("        "), "revert is still unconditional"


def test_a_skipped_revert_is_logged_with_the_status_it_found():
    """Silence would make "another writer owns this row" indistinguishable from
    "the retraction never ran"."""
    b = _revert_block()
    assert "candidate_revert_skipped" in b and "status=%s" in b


def test_it_matches_the_content_edit_reset_guard():
    """The two paths clear the same state and had no business disagreeing — the
    content-edit reset has always been guarded, which is why only this site was
    losing statuses."""
    from core_api.services import memory_service as ms

    src = inspect.getsource(ms)
    assert 'if mem.get("status") in ("outdated", "conflicted"):' in src


# ── oss-0814-l-48 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "build_quick_entity_linking_pipeline",
        "build_link_discovery_pipeline",
        "build_relation_inference_pipeline",
    ],
)
def test_the_unreachable_builders_are_gone(name):
    from core_api.pipeline.compositions import entity_linking

    assert not hasattr(entity_linking, name)


def test_the_one_live_builder_survives():
    """``lifecycle_audit`` calls this nightly — deleting the wrong one of the
    four would take entity linking down entirely."""
    from core_api.pipeline.compositions.entity_linking import (
        build_full_entity_linking_pipeline,
    )

    p = build_full_entity_linking_pipeline()
    assert p._name == "entity_linking_full"
    assert len(p._steps) == 4


def test_the_live_builder_still_has_its_caller():
    from core_api.services import lifecycle_audit

    assert "build_full_entity_linking_pipeline" in inspect.getsource(lifecycle_audit)
