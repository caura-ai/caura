"""C20 — "Path A" / "Path C" were opaque (and there is no Path B).

Code identifiers now say what the route actually does:

    Path A -> content   (semantic + RDF similarity)
    Path C -> entity    (entity-overlap, after extraction resolves subjects)

Two families of names deliberately KEEP the old spelling, because they are
observed from outside this process:

  * Redis lock keys ``contradiction:path_a:`` / ``contradiction:path_c:`` are
    live runtime state. Renaming them orphans every in-flight lock for its
    remaining TTL, so during a deploy the old and new key spaces coexist and the
    same memory can be processed twice. A cosmetic rename is not worth a
    double-detection window.
  * ``path_a_completed`` / ``path_c_completed`` are log EVENT names that saved
    log searches and the contradiction-quality dashboards match on (see D4).

This test pins both, so a later "finish the rename" pass has to make that
call deliberately rather than by tidy-up.
"""

import inspect

import pytest

from core_api.services import contradiction_detector as cd

pytestmark = pytest.mark.unit


def test_code_identifiers_use_the_meaningful_names():
    for name in (
        "_attempt_entity_retraction",
        "_acquire_content_lock",
        "_acquire_entity_lock",
        "_content_lock_key",
        "_entity_lock_key",
    ):
        assert hasattr(cd, name), f"{name} missing — rename incomplete"


def test_no_path_a_or_path_c_identifiers_remain():
    src = inspect.getsource(cd)
    leftovers = [
        ln
        for ln in src.splitlines()
        if ("path_a" in ln or "path_c" in ln)
        # the deliberate exceptions: lock-key literals, log event names, prose
        and "contradiction:path_" not in ln
        and "_completed" not in ln
        and not ln.strip().startswith("#")
    ]
    assert not leftovers, f"unrenamed identifiers: {leftovers}"


def test_redis_lock_keys_are_unchanged():
    """Runtime state — see the module docstring. If this ever changes, it must
    be a deliberate migration, not a rename."""
    src = inspect.getsource(cd)
    assert 'f"contradiction:path_a:' in src
    assert 'f"contradiction:path_c:' in src


def test_log_event_names_are_unchanged():
    """Dashboards and saved searches match these strings."""
    src = inspect.getsource(cd)
    assert '"path_a_completed for memory' in src
    assert '"path_c_completed for memory' in src


def test_the_two_lock_keys_stay_distinct():
    """The routes must never share a lock: one taking the other's key would
    suppress a whole detection route for the TTL."""
    a = cd._content_lock_key("m1", "content")
    c = cd._entity_lock_key("m1", "content")
    assert a != c and a.startswith("contradiction:") and c.startswith("contradiction:")
