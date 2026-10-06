"""lme-0929-m-05 (SIDE-58) — a tenant can switch contradiction detection off.

On the LongMemEval run, contradiction detection marked 4,374 stored source
turns across 500 stores ``outdated``/``conflicted``, which hides them from
default search. A store whose rows are verbatim source records (benchmark
tenants, and customers that want an append-only store) had no way to opt out.

``write.contradiction_detection_enabled`` (default ON = unchanged) is read at
the top of the two detector entries — Path A ``detect_contradictions_async``
and Path C ``detect_contradictions_by_entities_async``. Every trigger reaches
one of them through ``run_contradiction_detection``, in both the legacy and the
engine arch; ``test_contradiction_trigger_coverage`` pins that no production
path goes around them. So the tests below cover:

  * the settings contract (resolver default, the write-path validation that
    #1677 found missing, and a real ``PUT /settings`` round-trip);
  * each detector entry directly, on and off;
  * every ``Trigger`` through the dispatcher, in both arches;
  * the Pub/Sub EMBEDDED back-channel handler, end to end into
    the detector;
  * that a failed settings read keeps detection ON rather than silently
    switching it off.
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core_api.services.contradiction import Trigger, run_contradiction_detection
from core_api.services.organization_settings import ResolvedConfig

_CD = "core_api.services.contradiction_detector"
# ``resolve_config`` is imported function-locally by the detector, so it is
# patched at its SOURCE module (a patch on the detector's namespace is inert).
_RESOLVE = "core_api.services.organization_settings.resolve_config"

_EMB = [0.1, 0.2, 0.3]


def _cfg(value=None) -> ResolvedConfig:
    if value is None:
        return ResolvedConfig({})
    return ResolvedConfig({"write": {"contradiction_detection_enabled": value}})


def _row(memory_id) -> dict:
    return {
        "id": str(memory_id),
        "tenant_id": "t1",
        "fleet_id": "f1",
        "content": "Maya lives in Boston",
        "deleted_at": None,
        "supersedes_id": None,
        "status": "active",
    }


class _Probe:
    """Patches the detector's internals and records what was reached.

    A disabled tenant must reach NONE of: the admission slot, the storage
    client, the idempotency lock, ``_detect`` (Path A's detection body, where
    the judge LLM is called) or ``_attempt_entity_retraction`` (Path C's first
    phase). An enabled tenant must reach the detection body.
    """

    class _Stop(Exception):
        pass

    def __init__(self, cfg: ResolvedConfig, memory_id):
        self.cfg = cfg
        self.sc = MagicMock()
        self.sc.get_memory = AsyncMock(return_value=_row(memory_id))
        self.detect = AsyncMock(return_value=[])
        # Path C: stop right after the first phase is entered — reaching it is
        # the assertion; the rest of Path C is covered by its own suites.
        self.retraction = AsyncMock(side_effect=self._Stop("reached Path C body"))
        self.slot = AsyncMock()
        self.stack = ExitStack()

    def __enter__(self):
        from core_api.services import contradiction_detector as cd

        real_slot = cd._acquire_detection_slot
        self.slot.side_effect = real_slot
        for target, new in (
            (_RESOLVE, AsyncMock(return_value=self.cfg)),
            (f"{_CD}.get_storage_client", MagicMock(return_value=self.sc)),
            (f"{_CD}._acquire_detection_slot", self.slot),
            (f"{_CD}._acquire_content_lock", AsyncMock(return_value=True)),
            (f"{_CD}._acquire_entity_lock", AsyncMock(return_value=True)),
            (f"{_CD}._release_lock", AsyncMock()),
            (f"{_CD}._detect", self.detect),
            (f"{_CD}._attempt_entity_retraction", self.retraction),
            (f"{_CD}._record_detection_lost", AsyncMock()),
        ):
            self.stack.enter_context(patch(target, new))
        return self

    def __exit__(self, *exc):
        self.stack.close()
        return False

    @property
    def reached_anything(self) -> bool:
        return any(
            (
                self.slot.await_count,
                self.sc.get_memory.await_count,
                self.detect.await_count,
                self.retraction.await_count,
            )
        )

    @property
    def reached_detection(self) -> bool:
        return bool(self.detect.await_count or self.retraction.await_count)


# ── the settings contract ────────────────────────────────────────────────


@pytest.mark.unit
def test_detection_is_on_by_default():
    """Today's behaviour for every tenant that never touches the key."""
    assert _cfg().contradiction_detection_enabled is True
    assert ResolvedConfig({"write": {}}).contradiction_detection_enabled is True


@pytest.mark.unit
def test_a_tenant_can_switch_it_off():
    assert _cfg(False).contradiction_detection_enabled is False


@pytest.mark.unit
def test_an_explicit_true_is_honoured():
    assert _cfg(True).contradiction_detection_enabled is True


@pytest.mark.unit
def test_the_switch_survives_the_settings_write_validation():
    """#1677's lesson: a knob only on ``ResolvedConfig`` is unsettable, because
    ``_check_keys`` validates writes against ``DEFAULT_SETTINGS``."""
    from core_api.services.organization_settings import (
        DEFAULT_SETTINGS,
        _check_keys,
        _validate_leaf_types,
    )

    payload = {"write": {"contradiction_detection_enabled": False}}
    _check_keys(payload, DEFAULT_SETTINGS)
    _validate_leaf_types(payload)

    # A string "false" is TRUTHY: without the type check it would resolve to ON
    # while the tenant believed they had switched detection off.
    with pytest.raises(ValueError, match="contradiction_detection_enabled"):
        _validate_leaf_types({"write": {"contradiction_detection_enabled": "false"}})

    with pytest.raises(ValueError, match="Unknown settings key"):
        _check_keys(
            {"write": {"contradiction_detection_enable": False}}, DEFAULT_SETTINGS
        )


async def test_the_switch_round_trips_through_a_real_settings_put(client):
    """Through the route, not a hand-built ``ResolvedConfig`` — the c-04 defect
    was invisible to every test that built the config object directly."""
    from core_api.services.organization_settings import resolve_config
    from tests.conftest import get_test_auth
    from tests.conftest import uid as _uid

    tenant_id, headers = get_test_auth(tenant_id=f"test-tenant-{_uid()}")

    unset = await client.get(f"/api/v1/settings?tenant_id={tenant_id}", headers=headers)
    assert unset.status_code == 200, unset.text
    assert unset.json()["write"]["contradiction_detection_enabled"] is None

    resp = await client.put(
        f"/api/v1/settings?tenant_id={tenant_id}",
        json={"write": {"contradiction_detection_enabled": False}},
        headers=headers,
    )
    assert resp.status_code == 200, f"PUT failed — the knob is unsettable: {resp.text}"

    reloaded = await client.get(
        f"/api/v1/settings?tenant_id={tenant_id}", headers=headers
    )
    assert reloaded.status_code == 200, reloaded.text
    assert reloaded.json()["write"]["contradiction_detection_enabled"] is False

    config = await resolve_config(tenant_id)
    assert config.contradiction_detection_enabled is False


# ── each detector entry, directly ────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_path_a_entry_runs_when_enabled():
    from core_api.services.contradiction_detector import detect_contradictions_async

    mid = uuid4()
    with _Probe(_cfg(), mid) as probe:
        await detect_contradictions_async(mid, "t1", "f1", "Maya lives in Boston", _EMB)
    probe.detect.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_path_a_entry_is_skipped_when_disabled():
    """Nothing past the settings read: no slot, no storage GET, no lock, no
    detection body (and so no judge LLM call)."""
    from core_api.services.contradiction_detector import detect_contradictions_async

    mid = uuid4()
    with _Probe(_cfg(False), mid) as probe:
        await detect_contradictions_async(mid, "t1", "f1", "Maya lives in Boston", _EMB)
    assert not probe.reached_anything


@pytest.mark.unit
@pytest.mark.asyncio
async def test_path_a_entry_is_skipped_when_disabled_even_with_a_prefetched_row():
    """The back-channel consumers hand the row in (``new_memory``), which skips
    the storage GET — the gate must not depend on that GET happening."""
    from core_api.services.contradiction_detector import detect_contradictions_async

    mid = uuid4()
    with _Probe(_cfg(False), mid) as probe:
        await detect_contradictions_async(
            mid, "t1", "f1", "Maya lives in Boston", _EMB, new_memory=_row(mid)
        )
    assert not probe.reached_anything


@pytest.mark.unit
@pytest.mark.asyncio
async def test_path_c_entry_runs_when_enabled():
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    mid = uuid4()
    with _Probe(_cfg(), mid) as probe:
        await detect_contradictions_by_entities_async(mid, "t1", "f1")
    probe.retraction.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_path_c_entry_is_skipped_when_disabled():
    """Covers the retraction phase and the post-extraction RDF pass as well —
    both live behind this entry."""
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    mid = uuid4()
    with _Probe(_cfg(False), mid) as probe:
        await detect_contradictions_by_entities_async(mid, "t1", "f1")
    assert not probe.reached_anything


@pytest.mark.unit
@pytest.mark.asyncio
async def test_in_session_api_honours_a_disabled_config():
    from core_api.services.contradiction_detector import detect_contradictions

    mid = uuid4()
    with _Probe(_cfg(False), mid) as probe:
        assert await detect_contradictions(_row(mid), _EMB, _cfg(False)) == []
    probe.detect.assert_not_awaited()

    with _Probe(_cfg(), mid) as probe:
        await detect_contradictions(_row(mid), _EMB, _cfg())
    probe.detect.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["content", "entity"])
async def test_a_failed_settings_read_keeps_detection_on(entry):
    """Fail OPEN: an outage in the settings read must not silently switch a
    tenant's detection off. The run proceeds to the error handling it always
    had (its own ``resolve_config`` call)."""
    from core_api.services import contradiction_detector as cd

    mid = uuid4()
    with _Probe(_cfg(), mid) as probe:
        with patch(
            _RESOLVE, AsyncMock(side_effect=[RuntimeError("storage down"), _cfg()])
        ):
            if entry == "content":
                await cd.detect_contradictions_async(mid, "t1", "f1", "x", _EMB)
            else:
                await cd.detect_contradictions_by_entities_async(mid, "t1", "f1")
    assert probe.reached_detection


# ── every trigger, through the dispatcher, both arches ───────────────────


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("engine_arch", [False, True], ids=["legacy", "engine"])
@pytest.mark.parametrize("trigger", list(Trigger), ids=lambda t: t.name)
@pytest.mark.parametrize("enabled", [None, False], ids=["default_on", "switched_off"])
async def test_every_trigger_respects_the_switch(trigger, engine_arch, enabled):
    """The trigger sites (write fast+strong, bulk both A73 branches, update,
    re-embed x3, ENRICHED/EMBEDDED consumers, entity extraction) all call
    ``run_contradiction_detection`` with one of these triggers — pinned
    statically by ``test_contradiction_trigger_coverage``. So this matrix is
    every entry point, in either arch."""
    mid = uuid4()
    with (
        patch("core_api.config.settings.contradiction_engine_enabled", engine_arch),
        _Probe(_cfg(enabled), mid) as probe,
    ):
        await run_contradiction_detection(
            mid,
            "t1",
            "f1",
            trigger=trigger,
            content="Maya lives in Boston",
            embedding=_EMB,
        )
    if enabled is False:
        assert not probe.reached_anything, (
            f"{trigger.name} reached detection while switched off"
        )
    else:
        assert probe.reached_detection, (
            f"{trigger.name} did not reach detection by default"
        )


@pytest.mark.unit
def test_every_trigger_kind_is_in_the_matrix():
    """A new ``Trigger`` is covered by the parametrized test above automatically
    (it iterates the enum); this guards against someone narrowing that list."""
    assert {t.name for t in Trigger} >= {
        "WRITE",
        "EMBED",
        "UPDATE",
        "BULK",
        "REEMBED",
        "ENTITY",
    }


# ── the Pub/Sub back-channel handlers, end to end ────────────────────────


def _event(payload: dict):
    ev = MagicMock()
    ev.payload = payload
    ev.event_type = "test"
    ev.event_id = uuid4()
    return ev


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [None, False], ids=["default_on", "switched_off"])
async def test_embedded_back_channel_respects_the_switch(enabled):
    """``memory-embedded`` is the SOLE Path A trigger on the deferred-embed path,
    so it is the one a benchmark tenant on caura.dev (staging) actually hits."""
    from core_api import consumer

    mid = uuid4()
    row = {**_row(mid), "embedding": _EMB}
    with _Probe(_cfg(enabled), mid) as probe:
        consumer_sc = MagicMock()
        consumer_sc.get_memory = AsyncMock(return_value=row)
        with patch.object(consumer, "get_storage_client", return_value=consumer_sc):
            await consumer.handle_memory_embedded(
                _event(
                    {
                        "memory_id": str(mid),
                        "tenant_id": "t1",
                        "content": row["content"],
                    }
                )
            )
    if enabled is False:
        assert not probe.reached_anything
    else:
        probe.detect.assert_awaited_once()
