"""H-06 (#816) — the detection idempotency lock must not outlive its purpose.

The A4 #14 lock exists to collapse the two back-channel deliveries (ENRICHED
and EMBEDDED) that both fire Path A for one memory. It was keyed on
``memory_id`` alone, taken before detection, and never released, with a 1h TTL.
Two consequences, both fixed here:

1. **An edit within the hour was never checked.** ``update_memory`` clears
   supersession state and re-fires detection on a content change — the
   documented "P1-2: Re-check contradictions after content update" — but the
   write-time lock was still held, so the UPDATE run exited at the lock and the
   edited text was never examined. Since the update also resets ``status`` to
   ``active`` and ``supersedes_id`` to ``None``, a memory edited into direct
   contradiction with another active memory stayed unflagged, and nothing
   re-fired later. Same on Path C via entity re-extraction.

2. **One transient failure blocked re-detection for an hour.** The lock is
   taken before detection and was kept even when detection threw, with no
   retry scheduled.

The fix keys the lock on ``(memory_id, content fingerprint)`` and releases it
on any exit that is not a completed run. What must NOT change is the dedup
itself: a COMPLETED run keeps its lock, so the second delivery still skips.
Several tests below exist to pin exactly that.

Locks are modelled with a real in-memory SETNX/DEL store rather than by
patching the ``_acquire_*`` helpers, so the assertions run against the actual
key derivation — which is the thing that changed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from tests._contradiction_batch_compat import install_batch_status_replay_shim

pytestmark = pytest.mark.unit


class _FakeLocks:
    """Faithful model of Redis ``SET NX`` plus the compare-and-delete release.

    Values are modelled, not just key presence, because the release is
    ownership-checked: a run may only drop the lock it took itself.
    """

    def __init__(self) -> None:
        self.keys: dict[str, str] = {}
        self.acquired: list[str] = []
        self.released: list[str] = []

    async def set_nx(self, key: str, value: str, ttl: int) -> bool:  # noqa: ARG002
        if key in self.keys:
            return False
        self.keys[key] = value
        self.acquired.append(key)
        return True

    async def delete_if(self, key: str, expected: str) -> bool:
        if self.keys.get(key) != expected:
            return False
        del self.keys[key]
        self.released.append(key)
        return True

    def expire(self, key: str) -> None:
        """Model the TTL lapsing while a run is still in flight."""
        self.keys.pop(key, None)


def _locks(fake: _FakeLocks):
    """Patch both cache primitives the detector uses onto ``fake``."""
    return (
        patch("core_api.services.contradiction_detector.cache_set_nx", new=fake.set_nx),
        patch("core_api.services.contradiction_detector.cache_delete_if", new=fake.delete_if),
    )


def _memory(mid, *, content: str, deleted: bool = False) -> dict:
    return {
        "id": str(mid),
        "tenant_id": "t1",
        "fleet_id": "f1",
        "agent_id": "a1",
        "content": content,
        "status": "active",
        "visibility": "scope_team",
        "supersedes_id": None,
        "deleted_at": "2026-08-21T00:00:00+00:00" if deleted else None,
        "created_at": "2026-08-21T10:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# Consequence 1 — an edit is a different lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_path_a_edit_within_the_ttl_is_still_checked():
    """The whole point. Two deliveries of the SAME text collapse to one run;
    the EDIT that follows must still be examined.

    Pre-fix the third call skipped: same ``memory_id``, lock still held from
    the write-time run, edited content never checked.
    """
    from core_api.services.contradiction_detector import detect_contradictions_async

    mid = uuid4()
    fake = _FakeLocks()
    original, edited = "the sky is blue", "the sky is green"

    a, d = _locks(fake)
    with (
        a,
        d,
        patch(
            "core_api.services.contradiction_detector._detect",
            new_callable=AsyncMock,
            return_value=[],
        ) as detect_mock,
    ):
        # ENRICHED then EMBEDDED — same content, must collapse to one run.
        await detect_contradictions_async(
            mid, "t1", "f1", original, [0.1] * 10, new_memory=_memory(mid, content=original)
        )
        await detect_contradictions_async(
            mid, "t1", "f1", original, [0.1] * 10, new_memory=_memory(mid, content=original)
        )
        assert detect_mock.call_count == 1, "duplicate delivery must still be deduped"

        # The user corrects the memory; update_memory re-fires with new content.
        await detect_contradictions_async(
            mid, "t1", "f1", edited, [0.2] * 10, new_memory=_memory(mid, content=edited)
        )

    assert detect_mock.call_count == 2, "the edited content must be checked"


@pytest.mark.asyncio
async def test_path_c_edit_within_the_ttl_is_still_checked():
    """Path C has the identical defect, reached via entity re-extraction after
    an edit. Its content comes from the row it fetches, not from its caller."""
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    mid = uuid4()
    fake = _FakeLocks()
    sc = AsyncMock()
    sc.find_entity_overlap_candidates = AsyncMock(return_value=[])
    sc.update_memory_status = AsyncMock()
    install_batch_status_replay_shim(sc)

    sc.get_memory = AsyncMock(return_value=_memory(mid, content="the sky is blue"))

    a, d = _locks(fake)
    with a, d, patch("core_api.services.contradiction_detector.get_storage_client", return_value=sc):
        await detect_contradictions_by_entities_async(mid, "t1", "f1")
        await detect_contradictions_by_entities_async(mid, "t1", "f1")
        assert sc.find_entity_overlap_candidates.call_count == 1, "re-extraction must dedupe"

        # Content edited; entity extraction re-runs and fires Path C again.
        sc.get_memory = AsyncMock(return_value=_memory(mid, content="the sky is green"))
        await detect_contradictions_by_entities_async(mid, "t1", "f1")

    assert sc.find_entity_overlap_candidates.call_count == 2


# ---------------------------------------------------------------------------
# Consequence 2 — a failed run must not hold the lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_path_a_releases_the_lock_when_detection_raises():
    """One transient LLM/storage failure must not suppress every later trigger
    for the rest of the hour. Pre-fix the retry skipped at the lock."""
    from core_api.services.contradiction_detector import detect_contradictions_async

    mid = uuid4()
    fake = _FakeLocks()
    content = "the sky is blue"
    mem = _memory(mid, content=content)

    a, d = _locks(fake)
    with (
        a,
        d,
        patch(
            "core_api.services.contradiction_detector._detect",
            new_callable=AsyncMock,
            side_effect=RuntimeError("provider timeout"),
        ) as detect_mock,
    ):
        # The detector swallows the failure — the consumers rely on that to ack.
        await detect_contradictions_async(mid, "t1", "f1", content, [0.1] * 10, new_memory=mem)
        assert detect_mock.call_count == 1
        assert fake.released, "a failed run must drop its lock"

        # A later trigger for the same memory and the same content can retry.
        detect_mock.side_effect = None
        detect_mock.return_value = []
        await detect_contradictions_async(mid, "t1", "f1", content, [0.1] * 10, new_memory=mem)

    assert detect_mock.call_count == 2


@pytest.mark.asyncio
async def test_path_c_releases_the_lock_when_detection_raises():
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    mid = uuid4()
    fake = _FakeLocks()
    sc = AsyncMock()
    sc.get_memory = AsyncMock(return_value=_memory(mid, content="the sky is blue"))
    sc.update_memory_status = AsyncMock()
    install_batch_status_replay_shim(sc)
    sc.find_entity_overlap_candidates = AsyncMock(side_effect=RuntimeError("storage down"))

    a, d = _locks(fake)
    with a, d, patch("core_api.services.contradiction_detector.get_storage_client", return_value=sc):
        await detect_contradictions_by_entities_async(mid, "t1", "f1")
        assert fake.released, "a failed Path C run must drop its lock"

        sc.find_entity_overlap_candidates = AsyncMock(return_value=[])
        await detect_contradictions_by_entities_async(mid, "t1", "f1")

    assert sc.find_entity_overlap_candidates.call_count == 1, "the retry must reach detection"


@pytest.mark.asyncio
async def test_path_c_releases_the_lock_when_it_throws_mid_detection():
    """A failure AFTER the candidate search must release too.

    The obvious shortcut is one "we got past the candidate search" marker set
    early — but then a throw in the judging loop or the status write keeps the
    lock, which is the very half of Consequence 2 the issue describes. The
    conclusion markers therefore sit at each legitimate exit instead.
    """
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    mid = uuid4()
    fake = _FakeLocks()
    sc = AsyncMock()
    sc.get_memory = AsyncMock(return_value=_memory(mid, content="the sky is blue"))
    sc.update_memory_status = AsyncMock()
    install_batch_status_replay_shim(sc)
    # Candidates come back fine; the failure lands downstream of them, in the
    # subject preflight — which runs for every candidate immediately after the
    # search and before any of the "concluded" exits.
    sc.find_entity_overlap_candidates = AsyncMock(
        return_value=[_memory(uuid4(), content="the sky is red")]
    )

    a, d = _locks(fake)
    with (
        a,
        d,
        patch("core_api.services.contradiction_detector.get_storage_client", return_value=sc),
        patch(
            "core_api.services.contradiction_detector._subjects_differ_with_certainty",
            side_effect=RuntimeError("preflight exploded"),
        ) as preflight,
    ):
        await detect_contradictions_by_entities_async(mid, "t1", "f1")

    assert preflight.called, "the failure must land AFTER the candidate search"
    assert fake.released, "a throw after the lock must drop it wherever it happens"


@pytest.mark.asyncio
async def test_a_gone_row_never_takes_a_lock_at_all():
    """A soft-deleted row is resolved before the lock, so there is nothing to
    hold and nothing to release — and a later trigger (an undelete inside the
    window) still gets a real run rather than an hour of silence."""
    from core_api.services.contradiction_detector import detect_contradictions_async

    mid = uuid4()
    fake = _FakeLocks()
    content = "the sky is blue"

    a, d = _locks(fake)
    with (
        a,
        d,
        patch(
            "core_api.services.contradiction_detector._detect",
            new_callable=AsyncMock,
            return_value=[],
        ) as detect_mock,
    ):
        await detect_contradictions_async(
            mid, "t1", "f1", content, [0.1] * 10, new_memory=_memory(mid, content=content, deleted=True)
        )
        detect_mock.assert_not_called()
        assert fake.acquired == [], "a gone row must not consume a lock"

        await detect_contradictions_async(
            mid, "t1", "f1", content, [0.1] * 10, new_memory=_memory(mid, content=content)
        )

    detect_mock.assert_called_once()


@pytest.mark.asyncio
async def test_path_a_keys_on_the_row_that_will_be_examined_not_the_caller_copy():
    """``_detect`` reads ``new_memory``; the ``content`` parameter is only the
    caller's copy of it, and the back-channel consumers pass the write-time
    payload — which a mid-flight update leaves stale. The lock must describe
    the text that was actually looked at, so the fix cannot be undone by a
    caller that lets the two drift apart.
    """
    from core_api.services.contradiction_detector import (
        _path_a_lock_key,
        detect_contradictions_async,
    )

    mid = uuid4()
    fake = _FakeLocks()
    stale, current = "the sky is blue", "the sky is green"

    a, d = _locks(fake)
    with (
        a,
        d,
        patch(
            "core_api.services.contradiction_detector._detect",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        await detect_contradictions_async(
            mid, "t1", "f1", stale, [0.1] * 10, new_memory=_memory(mid, content=current)
        )

    assert fake.acquired == [_path_a_lock_key(mid, current)]


# ---------------------------------------------------------------------------
# What must NOT change — these pass before the fix too, by design
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_completed_run_keeps_its_lock():
    """Guard, not a bug demonstration.

    Releasing on success would have been the obvious reading of the issue's
    "release the lock in a finally block", and it would have silently destroyed
    the dedup the lock exists for: the two back-channel deliveries are often
    further apart than one detection takes, so the second would re-run every
    LLM judgement. Pinned so that reading cannot be reintroduced.

    It does not discriminate against pre-fix ``main`` — the module had no
    ``cache_delete`` to patch there, so it errors rather than failing on
    behaviour. The five tests above are the ones that demonstrate the bug.
    """
    from core_api.services.contradiction_detector import detect_contradictions_async

    mid = uuid4()
    fake = _FakeLocks()
    content = "the sky is blue"
    mem = _memory(mid, content=content)

    a, d = _locks(fake)
    with (
        a,
        d,
        patch(
            "core_api.services.contradiction_detector._detect",
            new_callable=AsyncMock,
            return_value=[],
        ) as detect_mock,
    ):
        await detect_contradictions_async(mid, "t1", "f1", content, [0.1] * 10, new_memory=mem)
        assert fake.released == [], "a completed run must NOT release its lock"
        await detect_contradictions_async(mid, "t1", "f1", content, [0.1] * 10, new_memory=mem)

    assert detect_mock.call_count == 1


@pytest.mark.asyncio
async def test_a_late_release_cannot_take_a_successor_s_lock():
    """The release is ownership-checked, not a bare DEL.

    Introducing a release introduced this risk with it: a run whose work
    outlives the 3600s TTL no longer owns the key, and by then a NEW run may
    hold it. An unconditional delete would hand that successor a duplicate
    detection — destroying the guarantee the lock exists for, which is worse
    than the bug being fixed.

    Modelled here by expiring the key mid-run and letting a second run take it
    before the first one's ``finally`` fires.
    """
    from core_api.services.contradiction_detector import (
        _path_a_lock_key,
        detect_contradictions_async,
    )

    mid = uuid4()
    fake = _FakeLocks()
    content = "the sky is blue"
    mem = _memory(mid, content=content)
    key = _path_a_lock_key(mid, content)

    async def _detect_then_lose_the_lock(*_a, **_kw):
        # The TTL lapses while this run is still working, and a fresh run
        # acquires the same key before we reach the finally block.
        fake.expire(key)
        await fake.set_nx(key, "successor-token", 3600)
        raise RuntimeError("slow provider finally gave up")

    a, d = _locks(fake)
    with (
        a,
        d,
        patch(
            "core_api.services.contradiction_detector._detect",
            new=_detect_then_lose_the_lock,
        ),
    ):
        await detect_contradictions_async(mid, "t1", "f1", content, [0.1] * 10, new_memory=mem)

    assert fake.keys.get(key) == "successor-token", (
        "the late release must not delete the lock a successor now holds"
    )
    assert fake.released == [], "nothing was owned by the time release ran"


@pytest.mark.asyncio
async def test_a_row_with_no_content_still_gets_a_usable_key():
    """``_content_fingerprint`` hashes a str. A row with empty content and a
    ``None`` from the caller must still produce a key rather than an
    AttributeError — which the outer handler would swallow, silently costing
    that memory its detection on this trigger."""
    from core_api.services.contradiction_detector import detect_contradictions_async

    mid = uuid4()
    fake = _FakeLocks()
    mem = _memory(mid, content="")
    mem["content"] = None

    a, d = _locks(fake)
    with (
        a,
        d,
        patch(
            "core_api.services.contradiction_detector._detect",
            new_callable=AsyncMock,
            return_value=[],
        ) as detect_mock,
    ):
        await detect_contradictions_async(mid, "t1", "f1", None, [0.1] * 10, new_memory=mem)

    detect_mock.assert_called_once()
    assert len(fake.acquired) == 1


@pytest.mark.asyncio
async def test_an_empty_row_content_is_a_real_value_not_a_missing_one():
    """``""`` on the row is content, and ``_detect`` reads it as content
    (``new_memory.get("content", "")``). Falling back to the caller's copy for
    it — which an ``or`` chain does — would key the lock on text detection
    never looks at, undoing the guarantee two tests above.
    """
    from core_api.services.contradiction_detector import (
        _path_a_lock_key,
        detect_contradictions_async,
    )

    mid = uuid4()
    fake = _FakeLocks()
    mem = _memory(mid, content="")

    a, d = _locks(fake)
    with (
        a,
        d,
        patch(
            "core_api.services.contradiction_detector._detect",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        await detect_contradictions_async(
            mid, "t1", "f1", "a stale caller copy", [0.1] * 10, new_memory=mem
        )

    assert fake.acquired == [_path_a_lock_key(mid, "")]


@pytest.mark.asyncio
async def test_paths_keep_independent_locks_per_content():
    """Path A and Path C must still not block each other, now per content."""
    from core_api.services.contradiction_detector import (
        _path_a_lock_key,
        _path_c_lock_key,
    )

    mid = uuid4()
    assert _path_a_lock_key(mid, "x") != _path_c_lock_key(mid, "x")
    assert _path_a_lock_key(mid, "x") != _path_a_lock_key(mid, "y")
    # Same inputs must be stable across calls, or the dedup never fires at all.
    assert _path_a_lock_key(mid, "x") == _path_a_lock_key(mid, "x")
    assert str(mid) in _path_a_lock_key(mid, "x")


@pytest.mark.asyncio
async def test_cache_delete_if_is_total():
    """The release runs in a ``finally`` on a code path whose never-raises
    contract is what lets the Pub/Sub consumers ack unconditionally. That rests
    on ``cache_delete_if`` swallowing a failing Redis rather than propagating."""
    from core_api.cache import cache_delete_if

    class _Boom:
        async def eval(self, *a, **kw):
            raise RuntimeError("connection reset")

    with patch("core_api.cache._get_redis", new_callable=AsyncMock, return_value=_Boom()):
        assert await cache_delete_if("k", "tok") is False

    with patch("core_api.cache._get_redis", new_callable=AsyncMock, return_value=None):
        assert await cache_delete_if("k", "tok") is False


@pytest.mark.asyncio
async def test_cache_delete_if_only_deletes_a_matching_value():
    """The compare and the delete must be one server-side step, and must not
    fire when the value has moved on."""
    from core_api.cache import cache_delete_if

    class _Redis:
        def __init__(self):
            self.store = {"k": "mine"}
            self.evals = 0

        async def eval(self, script, numkeys, key, arg):  # noqa: ARG002
            self.evals += 1
            if self.store.get(key) == arg:
                del self.store[key]
                return 1
            return 0

    r = _Redis()
    with patch("core_api.cache._get_redis", new_callable=AsyncMock, return_value=r):
        assert await cache_delete_if("k", "someone-else") is False
        assert r.store == {"k": "mine"}, "a non-matching release must not delete"
        assert await cache_delete_if("k", "mine") is True
        assert r.store == {}
    assert r.evals == 2, "compare+delete must be a single round trip each time"
