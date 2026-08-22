"""Routing-parity test for the contradiction-engine flag (A55).

Proves the flag is behaviourally a no-op in Phase 1: for every trigger,
``run_contradiction_detection`` reaches the SAME legacy detector function with
the SAME arguments whether ``contradiction_engine_enabled`` is False (legacy
direct call) or True (routed through ``ContradictionEngine``). Together with the
golden differential test (which freezes the detection *outcomes*), this pins
"engine ON == legacy OFF" for the consolidation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from core_api.services.contradiction import Trigger, run_contradiction_detection

pytestmark = pytest.mark.unit

_MID = uuid4()
_EMB = [0.1, 0.2, 0.3]

# (trigger, kwargs) -> which legacy fn should be called and with what args.
_PATH_A_CASES = [
    Trigger.WRITE,
    Trigger.EMBED,
    Trigger.UPDATE,
    Trigger.BULK,
    Trigger.REEMBED,
]


async def _run(flag: bool, trigger: Trigger, **kwargs):
    """Call the router with the flag set to ``flag``; return the recorded calls
    to both legacy detector entry points."""
    path_a = AsyncMock()
    path_c = AsyncMock()
    with (
        patch(
            "core_api.services.contradiction_detector.detect_contradictions_async",
            path_a,
        ),
        patch(
            "core_api.services.contradiction_detector.detect_contradictions_by_entities_async",
            path_c,
        ),
        patch("core_api.config.settings.contradiction_engine_enabled", flag),
    ):
        await run_contradiction_detection(_MID, "t1", "f1", trigger=trigger, **kwargs)
    return path_a.call_args_list, path_c.call_args_list


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", _PATH_A_CASES)
async def test_path_a_routing_identical_flag_on_off(trigger):
    kwargs = {"content": "X lives in Haifa", "embedding": _EMB, "new_memory": None}
    off_a, off_c = await _run(False, trigger, **kwargs)
    on_a, on_c = await _run(True, trigger, **kwargs)

    # Path A fn called once, identically, in both; Path C never.
    assert off_c == [] and on_c == []
    assert len(off_a) == 1 and len(on_a) == 1
    assert off_a[0].args == on_a[0].args
    assert off_a[0].kwargs == on_a[0].kwargs
    # And it carried the real args through to the detector.
    assert on_a[0].args == (_MID, "t1", "f1", "X lives in Haifa", _EMB)
    assert on_a[0].kwargs == {"new_memory": None}


@pytest.mark.asyncio
async def test_entity_routing_identical_flag_on_off():
    off_a, off_c = await _run(False, Trigger.ENTITY)
    on_a, on_c = await _run(True, Trigger.ENTITY)
    assert off_a == [] and on_a == []
    assert len(off_a) == 0 and len(off_c) == 1 and len(on_c) == 1
    assert off_c[0].args == on_c[0].args == (_MID, "t1", "f1")


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [False, True])
async def test_path_a_without_embedding_is_noop(flag):
    """A Path A trigger with no embedding is a no-op in both arches (deferred to
    the EMBEDDED back-channel), so neither detector fn is called."""
    a, c = await _run(flag, Trigger.WRITE, content="X", embedding=None)
    assert a == [] and c == []
