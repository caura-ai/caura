"""CAURA-125: direction-invariance of contradiction detection.

Audit gap A6 — the detector used ``_candidate_is_older`` as a hard
filter at three call sites in ``_detect()``, gating detection on the
candidate-vs-new temporal order. On the asymmetric branch (candidate
is newer than new_memory — happens primarily in deferred-embedding
scenarios), the candidate was silently dropped before any compare /
LLM check could run.

The fix splits detection from attribution:
  - Detection runs symmetrically on every candidate.
  - ``_pick_older(a, b)`` decides which row carries ``outdated`` /
    ``conflicted`` status AFTER the conflict is confirmed.
  - The supersedes_id edge always points newer → older.

These tests assert that semantic — detection symmetric, attribution
direction always newer→older — across all three call sites and at
the helper level.
"""

from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from core_api.constants import VECTOR_DIM
from core_api.services.contradiction_detector import _detect, _pick_older


# ---------------------------------------------------------------------------
# _pick_older helper — pure function, no I/O
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPickOlder:
    """Direction-resolution helper. Replaces the legacy ``_candidate_is_older``
    filter; now used at attribution time only."""

    def test_strictly_older_timestamp_wins(self):
        a = {"id": str(uuid4()), "created_at": "2026-04-29T10:00:00+00:00"}
        b = {"id": str(uuid4()), "created_at": "2026-04-29T11:00:00+00:00"}
        assert _pick_older(a, b) is a
        assert _pick_older(b, a) is a  # symmetric

    def test_tied_timestamp_uses_uuid_tiebreaker(self):
        ts = "2026-04-29T10:00:00+00:00"
        # Construct two UUIDs whose string ordering we control.
        # "00000000-..." sorts before "ffffffff-...".
        a = {"id": "00000000-0000-0000-0000-000000000001", "created_at": ts}
        b = {"id": "ffffffff-ffff-ffff-ffff-ffffffffffff", "created_at": ts}
        assert _pick_older(a, b) is a
        assert _pick_older(b, a) is a

    def test_tied_timestamp_same_uuid_returns_a(self):
        # Edge case: identical input. Deterministic — same arg wins by
        # the ``<=`` in the helper. Not a real production case (we'd
        # never compare a row to itself); included to lock the contract.
        row = {"id": str(uuid4()), "created_at": "2026-04-29T10:00:00+00:00"}
        assert _pick_older(row, row) is row

    def test_unparseable_timestamps_fall_back_to_uuid(self):
        a = {"id": "00000000-0000-0000-0000-000000000001", "created_at": "garbage"}
        b = {"id": "ffffffff-ffff-ffff-ffff-ffffffffffff", "created_at": None}
        assert _pick_older(a, b) is a
        assert _pick_older(b, a) is a

    def test_one_timestamp_parseable_one_not_falls_back_to_uuid(self):
        # Conservative: if we can't compare timestamps reliably,
        # fall back to UUID order. Don't favour the parseable one
        # (would create an asymmetry we can't justify).
        a = {
            "id": "00000000-0000-0000-0000-000000000001",
            "created_at": "2026-04-29T10:00:00+00:00",
        }
        b = {"id": "ffffffff-ffff-ffff-ffff-ffffffffffff", "created_at": "garbage"}
        assert _pick_older(a, b) is a

    def test_returns_one_of_inputs_always(self):
        # Property: the result is always an object identity match with
        # one of the inputs (never a fresh dict).
        for _ in range(20):
            a = {"id": str(uuid4()), "created_at": "2026-04-29T10:00:00+00:00"}
            b = {"id": str(uuid4()), "created_at": "2026-04-29T11:00:00+00:00"}
            result = _pick_older(a, b)
            assert result is a or result is b


# ---------------------------------------------------------------------------
# End-to-end through _detect() — mocked storage client
# ---------------------------------------------------------------------------


def _make_new_memory(
    *, mid: str | UUID, ts: str, object_value: str, supersedes: bool = False
) -> dict:
    """Build a ``new_memory`` dict shaped like what storage hands the detector."""
    return {
        "id": str(mid),
        "tenant_id": "t1",
        "fleet_id": "f1",
        "content": f"X lives in {object_value}",
        "subject_entity_id": "00000000-0000-0000-0000-0000000000aa",
        "predicate": "lives_in",
        "object_value": object_value,
        "deleted_at": None,
        "status": "active",
        "visibility": "scope_team",
        "supersedes_id": str(uuid4()) if supersedes else None,
        "created_at": ts,
    }


def _make_candidate(*, cid: str | UUID, ts: str, object_value: str) -> dict:
    """Build a candidate row from storage (RDF or semantic path)."""
    return {
        "id": str(cid),
        "content": f"X lives in {object_value}",
        "status": "active",
        "object_value": object_value,
        "created_at": ts,
    }


@pytest.mark.unit
class TestRdfAttributionDirection:
    """RDF path (path 1) — symmetric detection, direction set after."""

    @pytest.mark.asyncio
    async def test_canonical_case_candidate_older(self):
        """Candidate is older than new_memory: today's behaviour preserved.
        Candidate becomes outdated; new_memory gets supersedes_id pointing
        at the candidate."""
        cand = _make_candidate(
            cid=uuid4(), ts="2026-04-29T10:00:00+00:00", object_value="Tel Aviv"
        )
        new = _make_new_memory(
            mid=uuid4(), ts="2026-04-29T12:00:00+00:00", object_value="Haifa"
        )

        mock_sc = AsyncMock()
        mock_sc.find_rdf_conflicts = AsyncMock(return_value=[cand])
        mock_sc.update_memory_status = AsyncMock()

        with patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=mock_sc,
        ):
            await _detect(new, [0.1] * VECTOR_DIM)

        # Candidate marked outdated.
        # new_memory carries supersedes_id pointing at candidate.
        calls = [c.args for c in mock_sc.update_memory_status.call_args_list]
        assert (cand["id"], "outdated") in calls
        # The new memory's status update carries supersedes_id=cand.id.
        # ``update_memory_status`` is called as
        # (memory_id, status, supersedes_id=<id>) — find the call whose
        # first positional matches new.id.
        new_status_calls = [
            (c.args, c.kwargs)
            for c in mock_sc.update_memory_status.call_args_list
            if c.args and c.args[0] == new["id"]
        ]
        assert len(new_status_calls) == 1
        assert new_status_calls[0][1].get("supersedes_id") == cand["id"]

    @pytest.mark.asyncio
    async def test_flipped_case_candidate_newer(self):
        """Candidate is newer than new_memory (previously-unreachable branch).
        new_memory becomes outdated; the candidate carries supersedes_id
        pointing at new_memory."""
        # Candidate is younger — it was created after new_memory.
        cand = _make_candidate(
            cid=uuid4(), ts="2026-04-29T12:00:00+00:00", object_value="Tel Aviv"
        )
        new = _make_new_memory(
            mid=uuid4(), ts="2026-04-29T10:00:00+00:00", object_value="Haifa"
        )

        mock_sc = AsyncMock()
        mock_sc.find_rdf_conflicts = AsyncMock(return_value=[cand])
        mock_sc.update_memory_status = AsyncMock()

        with patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=mock_sc,
        ):
            await _detect(new, [0.1] * VECTOR_DIM)

        # new_memory (the older one this time) marked outdated.
        # The newer candidate carries supersedes_id pointing at new_memory.
        calls = [c.args for c in mock_sc.update_memory_status.call_args_list]
        assert (new["id"], "outdated") in calls
        cand_status_calls = [
            c
            for c in mock_sc.update_memory_status.call_args_list
            if c.args and c.args[0] == cand["id"]
        ]
        # We expect exactly one call that sets supersedes_id on the candidate.
        sup_calls = [
            c for c in cand_status_calls if c.kwargs.get("supersedes_id") == new["id"]
        ]
        assert len(sup_calls) == 1, (
            f"expected exactly one supersedes_id set on candidate; got "
            f"calls={[(c.args, c.kwargs) for c in cand_status_calls]}"
        )

    @pytest.mark.asyncio
    async def test_tied_timestamp_uuid_tiebreaker(self):
        """Exact ``created_at`` tie — older = smaller-UUID wins.
        Previously skipped detection entirely (strict ``<`` on tie)."""
        ts = "2026-04-29T10:00:00+00:00"
        smaller_id = "00000000-0000-0000-0000-000000000001"
        larger_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"

        # new_memory is the larger UUID; candidate is the smaller one.
        # _pick_older should choose the candidate as "older".
        cand = _make_candidate(cid=smaller_id, ts=ts, object_value="Tel Aviv")
        new = _make_new_memory(mid=larger_id, ts=ts, object_value="Haifa")

        mock_sc = AsyncMock()
        mock_sc.find_rdf_conflicts = AsyncMock(return_value=[cand])
        mock_sc.update_memory_status = AsyncMock()

        with patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=mock_sc,
        ):
            await _detect(new, [0.1] * VECTOR_DIM)

        calls = [c.args for c in mock_sc.update_memory_status.call_args_list]
        # Candidate (smaller UUID) is "older" → outdated.
        assert (smaller_id, "outdated") in calls
        # new_memory carries supersedes_id pointing at candidate.
        new_status_calls = [
            c
            for c in mock_sc.update_memory_status.call_args_list
            if c.args and c.args[0] == larger_id
        ]
        assert any(
            c.kwargs.get("supersedes_id") == smaller_id for c in new_status_calls
        )

    @pytest.mark.asyncio
    async def test_supersession_chain_points_newer_to_older(self):
        """Symmetry property: swapping which row is "new_memory" produces
        the same older→outdated, newer→supersedes_id assignment."""
        ts_old = "2026-04-29T10:00:00+00:00"
        ts_new = "2026-04-29T12:00:00+00:00"
        older_id = str(uuid4())
        newer_id = str(uuid4())

        # Forward: newer is new_memory, older is candidate.
        cand_a = _make_candidate(cid=older_id, ts=ts_old, object_value="Tel Aviv")
        new_a = _make_new_memory(mid=newer_id, ts=ts_new, object_value="Haifa")

        # Reverse: older is new_memory, newer is candidate.
        cand_b = _make_candidate(cid=newer_id, ts=ts_new, object_value="Haifa")
        new_b = _make_new_memory(mid=older_id, ts=ts_old, object_value="Tel Aviv")

        for label, (new, cand) in [
            ("forward", (new_a, cand_a)),
            ("reverse", (new_b, cand_b)),
        ]:
            mock_sc = AsyncMock()
            mock_sc.find_rdf_conflicts = AsyncMock(return_value=[cand])
            mock_sc.update_memory_status = AsyncMock()

            with patch(
                "core_api.services.contradiction_detector.get_storage_client",
                return_value=mock_sc,
            ):
                await _detect(new, [0.1] * VECTOR_DIM)

            calls = [c.args for c in mock_sc.update_memory_status.call_args_list]
            assert (older_id, "outdated") in calls, (
                f"{label}: expected older_id={older_id} to be outdated; "
                f"got calls={calls}"
            )

            newer_status_calls = [
                c
                for c in mock_sc.update_memory_status.call_args_list
                if c.args and c.args[0] == newer_id
            ]
            assert any(
                c.kwargs.get("supersedes_id") == older_id for c in newer_status_calls
            ), (
                f"{label}: expected newer.supersedes_id={older_id}; "
                f"got newer_calls={[(c.args, c.kwargs) for c in newer_status_calls]}"
            )


@pytest.mark.unit
class TestSemanticAttributionDirection:
    """Path 2 (semantic+LLM). Same symmetry contract as the RDF path,
    using ``find_similar_candidates`` and an LLM stub that always
    returns True (genuine conflict)."""

    @pytest.mark.asyncio
    async def test_canonical_case_candidate_older(self):
        """Older candidate → conflicted; new_memory points at it."""
        cand = _make_candidate(
            cid=uuid4(), ts="2026-04-29T10:00:00+00:00", object_value="Tel Aviv"
        )
        new = _make_new_memory(
            mid=uuid4(), ts="2026-04-29T12:00:00+00:00", object_value="Haifa"
        )

        mock_sc = AsyncMock()
        # Force the semantic path: no RDF conflicts → path 1 short-circuits.
        mock_sc.find_rdf_conflicts = AsyncMock(return_value=[])
        mock_sc.find_similar_candidates = AsyncMock(return_value=[cand])
        mock_sc.update_memory_status = AsyncMock()

        with (
            patch(
                "core_api.services.contradiction_detector.get_storage_client",
                return_value=mock_sc,
            ),
            patch(
                "core_api.services.contradiction_detector._llm_contradiction_check",
                # A4 #12 — judge returns (verdict, confidence) tuple.
                AsyncMock(return_value=(True, 0.90)),
            ),
        ):
            await _detect(new, [0.1] * VECTOR_DIM)

        calls = [c.args for c in mock_sc.update_memory_status.call_args_list]
        assert (cand["id"], "conflicted") in calls
        new_status_calls = [
            c
            for c in mock_sc.update_memory_status.call_args_list
            if c.args and c.args[0] == new["id"]
        ]
        assert any(
            c.kwargs.get("supersedes_id") == cand["id"] for c in new_status_calls
        )

    @pytest.mark.asyncio
    async def test_flipped_case_candidate_newer(self):
        """Newer candidate → new_memory becomes conflicted; candidate
        carries supersedes_id pointing at new_memory."""
        cand = _make_candidate(
            cid=uuid4(), ts="2026-04-29T12:00:00+00:00", object_value="Tel Aviv"
        )
        new = _make_new_memory(
            mid=uuid4(), ts="2026-04-29T10:00:00+00:00", object_value="Haifa"
        )

        mock_sc = AsyncMock()
        mock_sc.find_rdf_conflicts = AsyncMock(return_value=[])
        mock_sc.find_similar_candidates = AsyncMock(return_value=[cand])
        mock_sc.update_memory_status = AsyncMock()

        with (
            patch(
                "core_api.services.contradiction_detector.get_storage_client",
                return_value=mock_sc,
            ),
            patch(
                "core_api.services.contradiction_detector._llm_contradiction_check",
                # A4 #12 — judge returns (verdict, confidence) tuple.
                AsyncMock(return_value=(True, 0.90)),
            ),
        ):
            await _detect(new, [0.1] * VECTOR_DIM)

        calls = [c.args for c in mock_sc.update_memory_status.call_args_list]
        assert (new["id"], "conflicted") in calls
        cand_status_calls = [
            c
            for c in mock_sc.update_memory_status.call_args_list
            if c.args and c.args[0] == cand["id"]
        ]
        assert any(
            c.kwargs.get("supersedes_id") == new["id"] for c in cand_status_calls
        )


@pytest.mark.unit
class TestNoConflictNoSupersession:
    """Sanity-check the gate: when the LLM returns False or RDF finds
    no conflict, no status updates fire — direction is moot."""

    @pytest.mark.asyncio
    async def test_no_rdf_conflict_no_writes(self):
        new = _make_new_memory(
            mid=uuid4(), ts="2026-04-29T10:00:00+00:00", object_value="Haifa"
        )
        mock_sc = AsyncMock()
        mock_sc.find_rdf_conflicts = AsyncMock(return_value=[])
        mock_sc.find_similar_candidates = AsyncMock(return_value=[])
        mock_sc.update_memory_status = AsyncMock()

        with patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=mock_sc,
        ):
            await _detect(new, [0.1] * VECTOR_DIM)

        mock_sc.update_memory_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_llm_says_no_conflict_no_writes(self):
        cand = _make_candidate(
            cid=uuid4(), ts="2026-04-29T10:00:00+00:00", object_value="Tel Aviv"
        )
        new = _make_new_memory(
            mid=uuid4(), ts="2026-04-29T12:00:00+00:00", object_value="Haifa"
        )

        mock_sc = AsyncMock()
        mock_sc.find_rdf_conflicts = AsyncMock(return_value=[])
        mock_sc.find_similar_candidates = AsyncMock(return_value=[cand])
        mock_sc.update_memory_status = AsyncMock()

        with (
            patch(
                "core_api.services.contradiction_detector.get_storage_client",
                return_value=mock_sc,
            ),
            patch(
                "core_api.services.contradiction_detector._llm_contradiction_check",
                # A4 #12 — judge returns (verdict, confidence) tuple.
                AsyncMock(return_value=(False, 0.90)),
            ),
        ):
            await _detect(new, [0.1] * VECTOR_DIM)

        mock_sc.update_memory_status.assert_not_called()


@pytest.mark.unit
class TestMixedDirectionStateGuard:
    """Regression: when a single ``_detect()`` run sees both older and
    newer candidates, the flipped iteration must not be silently undone
    by a later canonical iteration re-asserting ``new_memory``'s
    pre-detection status."""

    @pytest.mark.asyncio
    async def test_flipped_then_canonical_preserves_outdated(self):
        """``rdf_conflicts = [newer_cand, older_cand]`` — without the
        guard, the canonical iteration runs
        ``update_memory_status(new_id, "active", supersedes_id=...)``
        and reverts the flipped iteration's ``"outdated"``."""
        new_id = "55555555-5555-5555-5555-555555555555"
        newer_cand = _make_candidate(
            cid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            ts="2026-04-29T12:00:00+00:00",
            object_value="Newer Place",
        )
        older_cand = _make_candidate(
            cid="00000000-0000-0000-0000-000000000001",
            ts="2026-04-29T08:00:00+00:00",
            object_value="Older Place",
        )
        new = _make_new_memory(
            mid=new_id, ts="2026-04-29T10:00:00+00:00", object_value="Haifa"
        )

        mock_sc = AsyncMock()
        # Order matters: flipped iteration must fire FIRST so the
        # later canonical iteration would (without the guard) clobber
        # the just-set "outdated" status.
        mock_sc.find_rdf_conflicts = AsyncMock(return_value=[newer_cand, older_cand])
        mock_sc.update_memory_status = AsyncMock()

        with patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=mock_sc,
        ):
            await _detect(new, [0.1] * VECTOR_DIM)

        # No call may set ``new_memory``'s status back to "active"
        # with a supersedes_id once the flipped iteration has set it
        # to outdated. ``update_memory_status(new_id, ...)`` may exist
        # at most once: the flipped iteration's own outdated-write,
        # which carries no supersedes_id kwarg.
        new_calls = [
            c
            for c in mock_sc.update_memory_status.call_args_list
            if c.args and c.args[0] == new_id
        ]
        # The flipped branch marks new_id "outdated" via
        # update_memory_status(str(older_id), "outdated") where older
        # = new_memory. That's the only call whose first arg is
        # new_id — the canonical iteration's
        # ``update_memory_status(new_id, "active", supersedes_id=...)``
        # must NOT fire.
        for c in new_calls:
            # Either explicit "outdated", or no supersedes_id kwarg.
            assert c.args[1] == "outdated" or "supersedes_id" not in c.kwargs, (
                f"State corruption: canonical iteration re-wrote "
                f"new_memory's status. Call: args={c.args} kwargs={c.kwargs}"
            )

    @pytest.mark.asyncio
    async def test_mixed_conflicts_complete_three_way_chain(self):
        """``rdf_conflicts = [newer_cand, older_cand]`` — after the
        flipped iteration marks new_memory ``"outdated"``, the
        canonical iteration must still wire
        ``new_memory.supersedes_id = older_cand.id``. Otherwise the
        older canonical candidate is left orphaned (outdated, but no
        chain edge pointing to it). Expected chain:
        ``newer_cand → new_memory → older_cand`` — all three edges
        present.

        Pre-fix behaviour: the canonical branch's guard
        ``not new_memory_is_outdated`` skipped the chain write
        entirely. This test pins the post-fix behaviour where the
        guard is split: the chain edge fires; the status used is
        the idempotent ``"outdated"`` rather than reverting to
        ``"active"``.
        """
        new_id = "55555555-5555-5555-5555-555555555555"
        newer_cand = _make_candidate(
            cid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            ts="2026-04-29T12:00:00+00:00",
            object_value="Newer Place",
        )
        older_cand = _make_candidate(
            cid="00000000-0000-0000-0000-000000000001",
            ts="2026-04-29T08:00:00+00:00",
            object_value="Older Place",
        )
        new = _make_new_memory(
            mid=new_id, ts="2026-04-29T10:00:00+00:00", object_value="Haifa"
        )

        mock_sc = AsyncMock()
        # Order matters: flipped iteration fires FIRST.
        mock_sc.find_rdf_conflicts = AsyncMock(return_value=[newer_cand, older_cand])
        mock_sc.update_memory_status = AsyncMock()

        with patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=mock_sc,
        ):
            await _detect(new, [0.1] * VECTOR_DIM)

        calls = mock_sc.update_memory_status.call_args_list

        # Chain edge 1: newer_cand.supersedes_id = new_memory.id.
        newer_cand_calls = [
            c for c in calls if c.args and c.args[0] == newer_cand["id"]
        ]
        assert any(c.kwargs.get("supersedes_id") == new_id for c in newer_cand_calls), (
            f"Missing edge newer_cand → new_memory; "
            f"newer_cand_calls={[(c.args, c.kwargs) for c in newer_cand_calls]}"
        )

        # Chain edge 2: new_memory.supersedes_id = older_cand.id.
        # THIS is the edge that was missing pre-fix (the orphaning bug).
        new_calls = [c for c in calls if c.args and c.args[0] == new_id]
        chain_calls = [
            c for c in new_calls if c.kwargs.get("supersedes_id") == older_cand["id"]
        ]
        assert chain_calls, (
            f"Missing edge new_memory → older_cand (orphan bug); "
            f"new_calls={[(c.args, c.kwargs) for c in new_calls]}"
        )
        # And the status used for that chain-edge write must be
        # ``"outdated"`` (idempotent with the flipped iteration's
        # earlier write), NOT ``"active"`` (which would silently
        # revert the row).
        for c in chain_calls:
            assert c.args[1] == "outdated", (
                f"Chain edge wrote status={c.args[1]!r}; expected 'outdated' "
                f"to preserve the flipped iteration's state."
            )

        # Older_cand explicitly marked outdated.
        older_cand_calls = [
            c for c in calls if c.args and c.args[0] == older_cand["id"]
        ]
        assert any(c.args[1] == "outdated" for c in older_cand_calls)

    @pytest.mark.asyncio
    async def test_canonical_then_flipped_preserves_supersedes_id(self):
        """``rdf_conflicts = [older_cand, newer_cand]`` — canonical
        iteration sets new_memory.supersedes_id; flipped iteration
        then marks new_memory outdated. Both updates must land.
        Symmetric regression to the test above."""
        new_id = "55555555-5555-5555-5555-555555555555"
        older_cand = _make_candidate(
            cid="00000000-0000-0000-0000-000000000001",
            ts="2026-04-29T08:00:00+00:00",
            object_value="Older Place",
        )
        newer_cand = _make_candidate(
            cid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            ts="2026-04-29T12:00:00+00:00",
            object_value="Newer Place",
        )
        new = _make_new_memory(
            mid=new_id, ts="2026-04-29T10:00:00+00:00", object_value="Haifa"
        )

        mock_sc = AsyncMock()
        mock_sc.find_rdf_conflicts = AsyncMock(return_value=[older_cand, newer_cand])
        mock_sc.update_memory_status = AsyncMock()

        with patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=mock_sc,
        ):
            await _detect(new, [0.1] * VECTOR_DIM)

        # Canonical iteration: new_memory.supersedes_id = older_cand.id.
        # Flipped iteration: new_memory marked "outdated".
        # Both writes against new_id must be present in some order.
        new_calls = [
            c
            for c in mock_sc.update_memory_status.call_args_list
            if c.args and c.args[0] == new_id
        ]
        # The canonical call carries supersedes_id; the flipped call
        # is a status-only update. Asserting both exist is enough —
        # final status in DB is "outdated" because storage applies
        # status updates last-write-wins on the same row.
        assert any(c.kwargs.get("supersedes_id") == older_cand["id"] for c in new_calls)
        assert any(c.args[1] == "outdated" for c in new_calls)


@pytest.mark.unit
class TestFlippedSkipsExistingSupersedesId:
    """Flipped-case write must not overwrite a candidate's existing
    ``supersedes_id`` — doing so would orphan whatever the candidate
    previously pointed at. Storage CAS is the last-line guard; this
    is the application-level explicit guard with a warning log."""

    @pytest.mark.asyncio
    async def test_rdf_flipped_skips_when_candidate_already_supersedes(self, caplog):
        """Newer candidate with pre-existing supersedes_id: no write,
        warning logged."""
        already_supersedes = "deadbeef-0000-0000-0000-000000000099"
        cand = _make_candidate(
            cid=uuid4(), ts="2026-04-29T12:00:00+00:00", object_value="Tel Aviv"
        )
        cand["supersedes_id"] = already_supersedes  # pre-existing edge
        new = _make_new_memory(
            mid=uuid4(), ts="2026-04-29T10:00:00+00:00", object_value="Haifa"
        )

        mock_sc = AsyncMock()
        mock_sc.find_rdf_conflicts = AsyncMock(return_value=[cand])
        mock_sc.update_memory_status = AsyncMock()

        with patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=mock_sc,
        ):
            with caplog.at_level("WARNING"):
                await _detect(new, [0.1] * VECTOR_DIM)

        # No call on the candidate may carry supersedes_id (the
        # would-be-overwrite). The candidate may still be touched
        # for other reasons (none expected here).
        cand_calls = [
            c
            for c in mock_sc.update_memory_status.call_args_list
            if c.args and c.args[0] == cand["id"]
        ]
        assert not any(c.kwargs.get("supersedes_id") for c in cand_calls), (
            f"Expected NO supersedes_id overwrite on candidate; "
            f"got cand_calls={[(c.args, c.kwargs) for c in cand_calls]}"
        )
        # And the warning fires with both ids visible for debugging.
        assert any(
            "skipped supersedes_id overwrite" in r.message
            and already_supersedes in r.message
            and cand["id"] in r.message
            for r in caplog.records
        ), (
            f"Missing expected warning; records={[(r.levelname, r.message) for r in caplog.records]}"
        )

    @pytest.mark.asyncio
    async def test_semantic_flipped_skips_when_candidate_already_supersedes(
        self, caplog
    ):
        """Same contract on the semantic path."""
        already_supersedes = "deadbeef-0000-0000-0000-000000000099"
        cand = _make_candidate(
            cid=uuid4(), ts="2026-04-29T12:00:00+00:00", object_value="Tel Aviv"
        )
        cand["supersedes_id"] = already_supersedes
        new = _make_new_memory(
            mid=uuid4(), ts="2026-04-29T10:00:00+00:00", object_value="Haifa"
        )

        mock_sc = AsyncMock()
        mock_sc.find_rdf_conflicts = AsyncMock(return_value=[])  # force semantic path
        mock_sc.find_similar_candidates = AsyncMock(return_value=[cand])
        mock_sc.update_memory_status = AsyncMock()

        with (
            patch(
                "core_api.services.contradiction_detector.get_storage_client",
                return_value=mock_sc,
            ),
            patch(
                "core_api.services.contradiction_detector._llm_contradiction_check",
                # A4 #12 — judge returns (verdict, confidence) tuple.
                AsyncMock(return_value=(True, 0.90)),
            ),
        ):
            with caplog.at_level("WARNING"):
                await _detect(new, [0.1] * VECTOR_DIM)

        cand_calls = [
            c
            for c in mock_sc.update_memory_status.call_args_list
            if c.args and c.args[0] == cand["id"]
        ]
        assert not any(c.kwargs.get("supersedes_id") for c in cand_calls)
        assert any(
            "skipped supersedes_id overwrite" in r.message for r in caplog.records
        )


@pytest.mark.unit
class TestContradictionInfoSemantics:
    """``ContradictionInfo.old_memory_id`` is the pre-existing candidate
    (never ``new_memory``) regardless of direction. The new ``direction``
    field disambiguates the two cases for the API consumer."""

    @pytest.mark.asyncio
    async def test_canonical_direction_field_and_old_id(self):
        cand = _make_candidate(
            cid=uuid4(), ts="2026-04-29T08:00:00+00:00", object_value="Tel Aviv"
        )
        new = _make_new_memory(
            mid=uuid4(), ts="2026-04-29T10:00:00+00:00", object_value="Haifa"
        )

        mock_sc = AsyncMock()
        mock_sc.find_rdf_conflicts = AsyncMock(return_value=[cand])
        mock_sc.update_memory_status = AsyncMock()

        with patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=mock_sc,
        ):
            contradictions = await _detect(new, [0.1] * VECTOR_DIM)

        assert len(contradictions) == 1
        info = contradictions[0]
        assert str(info.old_memory_id) == cand["id"]
        assert info.direction == "canonical"
        assert info.old_status == "outdated"

    @pytest.mark.asyncio
    async def test_flipped_direction_field_and_old_id(self):
        cand = _make_candidate(
            cid=uuid4(), ts="2026-04-29T12:00:00+00:00", object_value="Tel Aviv"
        )
        new = _make_new_memory(
            mid=uuid4(), ts="2026-04-29T08:00:00+00:00", object_value="Haifa"
        )

        mock_sc = AsyncMock()
        mock_sc.find_rdf_conflicts = AsyncMock(return_value=[cand])
        mock_sc.update_memory_status = AsyncMock()

        with patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=mock_sc,
        ):
            contradictions = await _detect(new, [0.1] * VECTOR_DIM)

        assert len(contradictions) == 1
        info = contradictions[0]
        # ``old_memory_id`` is the pre-existing candidate's ID even in
        # the flipped case — NOT new_memory's ID. This is the contract
        # the schema docstring nails down so API consumers don't have
        # to infer.
        assert str(info.old_memory_id) == cand["id"]
        assert info.direction == "flipped"
        # In the flipped case, the candidate's own status didn't
        # change — surface its actual current state rather than a
        # misleading "outdated".
        assert info.old_status == cand["status"]
