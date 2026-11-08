"""Extract again the entity graphs the regex heuristic made.

``reextract_heuristic_graphs`` walks each tenant's memories created in a window,
reads their entity links, and with ``--apply`` resets and extracts again those
whose graph has the heuristic's shape. Storage is a small in-memory fake and
``process_entity_extraction`` is patched where the command imports it, so these
pin what it reads, what it picks, and what it calls.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core_api import tasks
from core_api.maintenance import reextract_heuristic_graphs as rx

pytestmark = pytest.mark.unit

_SINCE = datetime(2026, 10, 10, 23, 0, tzinfo=UTC)
_UNTIL = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
_NAMED = "Anna Bergstrom joined Acme Corp."
_MODEL_LINKS = [
    {"entity_id": "e1", "role": "subject"},
    {"entity_id": "e2", "role": "object"},
]
_HEURISTIC_LINKS = [
    {"entity_id": "e1", "role": "mentioned"},
    {"entity_id": "e2", "role": "mentioned"},
]


def _memory(created: datetime, **fields) -> dict:
    row = {
        "id": str(uuid.uuid4()),
        "tenant_id": "t1",
        "fleet_id": "f1",
        "agent_id": "a1",
        "memory_type": "fact",
        "content": _NAMED,
        "status": "active",
        "subject_entity_id": None,
        "deleted_at": None,
        "created_at": created.isoformat(),
    }
    row.update(fields)
    return row


class _Storage:
    """The storage calls the command makes, over rows and links held here."""

    def __init__(self, rows: list[dict], links: dict[str, list[dict]] | None = None):
        self.rows = {r["id"]: r for r in rows}
        self.links = dict(links or {})
        self.list_calls: list[dict] = []
        self.resets: list[tuple[str, str]] = []

    async def admin_list_memories(self, data: dict) -> list[dict]:
        self.list_calls.append(data)
        assert (data["sort"], data["order"]) == ("created_at", "desc")
        rows = sorted(
            (r for r in self.rows.values() if r["tenant_id"] == data["tenant_id"]),
            key=lambda r: (r["created_at"], r["id"]),
            reverse=True,
        )
        if "cursor_ts" in data:
            cursor = (data["cursor_ts"], data["cursor_id"])
            rows = [r for r in rows if (r["created_at"], r["id"]) < cursor]
        return rows[: data["limit"]]

    async def get_entity_links_for_memories(
        self, memory_ids: list[str], tenant_id: str
    ) -> dict:
        return {
            m: self.links[m]
            for m in memory_ids
            if self.links.get(m) and self.rows[m]["tenant_id"] == tenant_id
        }

    async def get_memory(
        self, memory_id: str, tenant_id: str, read: bool = True
    ) -> dict | None:
        row = self.rows.get(memory_id)
        return row if row and row["tenant_id"] == tenant_id else None

    async def reset_entity_artifacts(self, tenant_id: str, memory_id: str) -> dict:
        self.resets.append((tenant_id, memory_id))
        self.links.pop(memory_id, None)
        return {}


@pytest.fixture
def setup(monkeypatch):
    """Install a storage fake, an extraction that leaves ``graph`` behind, and tenant settings."""

    def _install(
        rows, links=None, *, graph=_MODEL_LINKS, enabled=True, provider="vertex"
    ):
        sc = _Storage(rows, links)
        extract = AsyncMock()

        async def _extract(memory_id, tenant_id, *_args):
            await extract(memory_id, tenant_id, *_args)
            if graph:
                sc.links[str(memory_id)] = list(graph)

        monkeypatch.setattr(rx, "get_storage_client", lambda: sc)
        monkeypatch.setattr(rx, "process_entity_extraction", _extract)
        monkeypatch.setattr(
            "core_api.services.organization_settings.resolve_config",
            AsyncMock(
                return_value=SimpleNamespace(
                    entity_extraction_enabled=enabled,
                    entity_extraction_provider=provider,
                )
            ),
        )
        return sc, extract

    return _install


async def _run(*, apply: bool, skip_empty: bool = False, tenants=("t1",)) -> Counter:
    return await rx.run(
        list(tenants),
        since=_SINCE,
        until=_UNTIL,
        apply=apply,
        per_second=1000.0,
        concurrency=2,
        skip_empty=skip_empty,
    )


# ---------------------------------------------------------------------------
# Telling a graph's shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("memory", "links", "outcome"),
    [
        pytest.param({}, _MODEL_LINKS, rx.MODEL, id="a-subject-link"),
        pytest.param(
            {}, [{"entity_id": "e", "role": "object"}], rx.MODEL, id="an-object-link"
        ),
        pytest.param(
            {"subject_entity_id": "e1"},
            _HEURISTIC_LINKS,
            rx.MODEL,
            id="a-subject-on-the-row",
        ),
        pytest.param({}, _HEURISTIC_LINKS, rx.HEURISTIC, id="only-mentions"),
        pytest.param({}, [], rx.EMPTY, id="no-links"),
        pytest.param(
            {"status": "outdated"}, _HEURISTIC_LINKS, rx.NOT_LIVE, id="not-live"
        ),
        pytest.param({"content": "  "}, [], rx.NO_CONTENT, id="no-content"),
    ],
)
def test_each_row_gets_one_outcome(memory, links, outcome):
    assert rx.classify(_memory(_SINCE, **memory), links) == outcome


def test_the_seed_test_matches_the_extractors_prompt():
    """``SEED_PAST_INT32`` counts the prompts Vertex refused before caura#2367:
    the seed is the CRC32 of the prompt the extractor sends."""
    import zlib

    from core_api.services.entity_extraction import EXTRACTION_PROMPT

    for i in range(64):
        memory = _memory(_SINCE, content=f"memory number {i}")
        prompt = EXTRACTION_PROMPT.format(memory_type="fact", content=memory["content"])
        assert rx.seed_past_int32(memory) is (zlib.crc32(prompt.encode()) >= 2**31)


# ---------------------------------------------------------------------------
# The scan
# ---------------------------------------------------------------------------


async def test_a_dry_run_reads_the_window_and_extracts_nothing(
    setup, capsys, monkeypatch
):
    monkeypatch.setattr(rx, "_PAGE", 2)  # several pages
    inside = _SINCE + timedelta(days=1)
    heuristic, empty, model = (_memory(inside + timedelta(minutes=i)) for i in range(3))
    too_old = _memory(_SINCE - timedelta(seconds=1))
    too_new = _memory(_UNTIL)
    other_tenant = _memory(inside, tenant_id="t2")
    rows = [heuristic, empty, model, too_old, too_new, other_tenant]
    links = {
        heuristic["id"]: _HEURISTIC_LINKS,
        model["id"]: _MODEL_LINKS,
        too_old["id"]: _HEURISTIC_LINKS,
        too_new["id"]: _HEURISTIC_LINKS,
        other_tenant["id"]: _HEURISTIC_LINKS,
    }
    sc, extract = setup(rows, links)

    outcomes = await _run(apply=False)

    assert (outcomes[rx.HEURISTIC], outcomes[rx.EMPTY], outcomes[rx.MODEL]) == (1, 1, 1)
    extract.assert_not_awaited()
    assert sc.resets == []
    assert len(sc.list_calls) >= 2
    assert all(c["tenant_id"] == "t1" for c in sc.list_calls)
    out = capsys.readouterr().out
    assert f"t1 {heuristic['id']} heuristic" in out
    assert f"t1 {empty['id']} empty" in out
    for absent in (model, too_old, too_new, other_tenant):
        assert absent["id"] not in out
    assert _NAMED not in out


async def test_the_scan_stops_paging_once_past_the_window(setup, monkeypatch):
    monkeypatch.setattr(rx, "_PAGE", 2)
    rows = [_memory(_SINCE + timedelta(hours=1))] + [
        _memory(_SINCE - timedelta(days=d)) for d in range(1, 20)
    ]
    sc, _extract = setup(rows)

    await _run(apply=False)

    assert len(sc.list_calls) == 1


def test_the_grace_outlasts_the_workers_longest_wait():
    """With failing providers the worker asks again after each delay, jittered,
    before it writes the heuristic's graph; until then the memory has no links."""
    from common.llm.constants import LLM_RETRY_JITTER_FRACTION
    from core_api.constants import ENTITY_EXTRACTION_PROVIDER_RETRY_DELAYS_S

    longest_wait = sum(ENTITY_EXTRACTION_PROVIDER_RETRY_DELAYS_S) * (
        1 + LLM_RETRY_JITTER_FRACTION
    )
    assert rx._IN_FLIGHT_GRACE.total_seconds() >= 2 * longest_wait


@pytest.mark.parametrize(
    "until_ahead", [None, timedelta(hours=1)], ids=["no-until", "until-ahead"]
)
async def test_a_memory_whose_first_extraction_may_still_run_is_left(
    setup, until_ahead
):
    """Written minutes ago, it reads ``empty`` while the worker extracts it.
    Extracting it again would run beside the worker and reset under it."""
    now = datetime.now(UTC)
    in_flight = _memory(now - timedelta(minutes=5))
    settled = _memory(now - timedelta(minutes=20))
    sc, _extract = setup([in_flight, settled])

    outcomes = await rx.run(
        ["t1"],
        since=now - timedelta(days=1),
        until=None if until_ahead is None else now + until_ahead,
        apply=True,
        per_second=1000.0,
        concurrency=2,
        skip_empty=False,
    )

    assert sc.resets == [("t1", settled["id"])]
    assert outcomes[rx.EMPTY] == 1


async def test_skip_empty_leaves_memories_with_no_graph(setup):
    inside = _SINCE + timedelta(days=1)
    heuristic, empty = _memory(inside), _memory(inside + timedelta(minutes=1))
    sc, _extract = setup([heuristic, empty], {heuristic["id"]: _HEURISTIC_LINKS})

    outcomes = await _run(apply=True, skip_empty=True)

    assert sc.resets == [("t1", heuristic["id"])]
    assert outcomes[rx.EMPTY] == 1
    assert outcomes[rx.EXTRACTED] == 1


@pytest.mark.parametrize(
    ("enabled", "provider"),
    [
        pytest.param(False, "vertex", id="extraction-off"),
        pytest.param(True, "fake", id="heuristic-only"),
    ],
)
async def test_a_tenant_whose_extraction_would_make_the_same_graph_is_skipped(
    setup, capsys, enabled, provider
):
    memory = _memory(_SINCE + timedelta(days=1))
    sc, extract = setup(
        [memory], {memory["id"]: _HEURISTIC_LINKS}, enabled=enabled, provider=provider
    )

    outcomes = await _run(apply=True)

    assert sc.list_calls == []
    extract.assert_not_awaited()
    assert sum(outcomes.values()) == 0
    assert '"skipped": "extraction_off"' in capsys.readouterr().out


# ---------------------------------------------------------------------------
# --apply
# ---------------------------------------------------------------------------


async def test_apply_resets_and_extracts_each_picked_memory_again(setup, capsys):
    inside = _SINCE + timedelta(days=1)
    heuristic = _memory(inside, memory_type="episode", fleet_id=None)
    empty = _memory(inside + timedelta(minutes=1))
    model = _memory(inside + timedelta(minutes=2))
    sc, extract = setup(
        [heuristic, empty, model],
        {heuristic["id"]: _HEURISTIC_LINKS, model["id"]: _MODEL_LINKS},
    )

    outcomes = await _run(apply=True)

    assert sorted(sc.resets) == sorted([("t1", heuristic["id"]), ("t1", empty["id"])])
    assert extract.await_count == 2
    [call] = [c for c in extract.await_args_list if str(c.args[0]) == heuristic["id"]]
    assert call.args[1:] == ("t1", None, "a1", _NAMED, "episode")
    assert outcomes[rx.EXTRACTED] == 2
    assert sc.links[model["id"]] == _MODEL_LINKS  # left alone
    assert f"t1 {heuristic['id']} extracted" in capsys.readouterr().out


async def test_a_graph_still_all_mentions_counts_against_the_run(setup):
    """The providers failed again: the extraction wrote the heuristic's graph."""
    memory = _memory(_SINCE + timedelta(days=1))
    _sc, _extract = setup(
        [memory], {memory["id"]: _HEURISTIC_LINKS}, graph=_HEURISTIC_LINKS
    )

    outcomes = await _run(apply=True)

    assert outcomes[rx.HEURISTIC_AGAIN] == 1


async def test_an_empty_graph_again_is_not_a_failure(setup):
    """What a model finds in text that names nothing."""
    memory = _memory(
        _SINCE + timedelta(days=1), content="a note with nothing named in it"
    )
    _sc, _extract = setup([memory], graph=[])

    outcomes = await _run(apply=True)

    assert outcomes[rx.EMPTY_AGAIN] == 1
    assert outcomes[rx.HEURISTIC_AGAIN] == 0


async def test_apply_stops_after_five_failures_in_a_row(setup):
    """Providers that still fail would write the heuristic's graph over every
    memory in the list; five in a row is enough to stop and look."""
    rows = [_memory(_SINCE + timedelta(minutes=i)) for i in range(12)]
    _sc, extract = setup(
        rows, {r["id"]: _HEURISTIC_LINKS for r in rows}, graph=_HEURISTIC_LINKS
    )

    outcomes = await _run(apply=True, tenants=("t1", "t1"))

    # Two at once: the fifth failure lands while the sixth may be in flight.
    assert 5 <= extract.await_count <= 6
    assert outcomes[rx.HEURISTIC_AGAIN] == extract.await_count
    assert outcomes[rx.HEURISTIC] == 12  # the second tenant was not scanned


async def test_a_memory_gone_since_the_scan_is_not_reset(setup):
    memory = _memory(_SINCE + timedelta(days=1))
    sc, extract = setup([memory], {memory["id"]: _HEURISTIC_LINKS})
    original_get = sc.get_memory

    async def _deleted(memory_id, tenant_id, read=True):
        row = await original_get(memory_id, tenant_id, read)
        return {**row, "deleted_at": "2026-11-08T12:00:00+00:00"}

    sc.get_memory = _deleted

    outcomes = await _run(apply=True)

    assert outcomes[rx.GONE] == 1
    assert sc.resets == []
    extract.assert_not_awaited()


async def test_a_storage_error_is_counted_not_raised(setup):
    memory = _memory(_SINCE + timedelta(days=1))
    sc, extract = setup([memory], {memory["id"]: _HEURISTIC_LINKS})
    sc.reset_entity_artifacts = AsyncMock(side_effect=RuntimeError("storage 503"))

    outcomes = await _run(apply=True)

    assert outcomes[rx.ERROR] == 1
    extract.assert_not_awaited()


async def test_the_run_waits_for_the_contradiction_detection_its_extractions_start(
    setup, monkeypatch
):
    """The worker starts contradiction detection in a tracked task; a one-off
    process that returned before it finished would lose it at exit."""
    finished: list[str] = []
    memory = _memory(_SINCE + timedelta(days=1))
    _sc, _extract = setup([memory], {memory["id"]: _HEURISTIC_LINKS})
    extract_and_detect = rx.process_entity_extraction

    async def _detection(memory_id):
        await asyncio.sleep(0.05)  # longer than the run takes without it
        finished.append(memory_id)

    async def _extract(memory_id, *args):
        await extract_and_detect(memory_id, *args)
        tasks.track_task(_detection(str(memory_id)))

    monkeypatch.setattr(rx, "process_entity_extraction", _extract)

    await _run(apply=True)

    assert finished == [memory["id"]]


async def test_waiting_for_tracked_tasks_includes_the_tasks_they_start():
    order: list[str] = []

    async def _child():
        await asyncio.sleep(0)
        order.append("child")

    async def _parent():
        await asyncio.sleep(0)
        tasks.track_task(_child())
        order.append("parent")

    tasks.track_task(_parent())
    await tasks.wait_for_tracked_tasks()

    assert order == ["parent", "child"]


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--since", "2026-10-10T23:00:00Z", "--per-second", "0"],
        ["--since", "2026-10-10T23:00:00Z", "--concurrency", "0"],
        ["--since", "2026-11-08", "--until", "2026-10-10"],
        ["--until", "2026-11-08"],
    ],
    ids=["no-rate", "no-concurrency", "until-before-since", "no-since"],
)
def test_bad_arguments_are_refused(argv):
    with pytest.raises(SystemExit) as caught:
        rx.main(argv)
    assert caught.value.code == 2


def test_a_time_without_a_zone_is_utc():
    assert rx._timestamp("2026-10-10T23:00:00") == _SINCE
    assert rx._timestamp("2026-10-10T23:00:00Z") == _SINCE
