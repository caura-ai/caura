"""TrackRecalls should bump recall_count only for recalls with a caller agent
identity — agentless /search (health probes, monitoring, dashboard/admin) must
not inflate recall_count / recall_boost (gap A26).
"""

from types import SimpleNamespace
from uuid import uuid4

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.search import track_recalls as tr


def _row():
    return SimpleNamespace(Memory=SimpleNamespace(id=uuid4()))


def _ctx(caller_agent_id):
    return PipelineContext(
        data={"caller_agent_id": caller_agent_id, "filtered_rows": [_row(), _row()]}
    )


async def _run(monkeypatch, caller_agent_id):
    calls = []

    def fake_track_task(coro):
        calls.append(coro)
        coro.close()  # never actually run the background bump; avoid "never awaited"

    monkeypatch.setattr(tr, "track_task", fake_track_task)
    await tr.TrackRecalls().execute(_ctx(caller_agent_id))
    return calls


async def test_counts_for_real_agent(monkeypatch):
    calls = await _run(monkeypatch, "brandclaw")
    assert len(calls) == 1  # genuine agent recall → recall_count bumped


async def test_skips_when_agent_id_none(monkeypatch):
    calls = await _run(monkeypatch, None)
    assert calls == []  # agentless (probe/monitoring) → no bump


async def test_skips_when_agent_id_empty(monkeypatch):
    calls = await _run(monkeypatch, "")
    assert calls == []  # empty identity → no bump


async def test_no_rows_no_bump(monkeypatch):
    calls = []
    monkeypatch.setattr(tr, "track_task", lambda c: (calls.append(c), c.close()))
    ctx = PipelineContext(data={"caller_agent_id": "brandclaw", "filtered_rows": []})
    await tr.TrackRecalls().execute(ctx)
    assert calls == []
