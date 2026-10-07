"""OSS 09/27 M-03 — a tracked task that gives up must leave a durable record.

``tracked_task`` writes a ``background_task_log`` row ONLY when the coroutine it
wraps raises. A coroutine that catches its own failure, logs it and returns
normally reports success to the only table an operator inspects. 09/02 M-40
split out ``record_task_failure`` for exactly that shape and applied it to one
site; #1733 (``reembed_stranded``) and #1736 (``enrich_stranded``) found two
more by accident. Nothing gated the class. This file is the gate.

What it asserts, and why it is behavioural rather than a scan of the source.
An AST check that "every ``except`` calls ``record_task_failure``" would look
like enforcement and would not be: it cannot tell a deliberate degradation
(config lookup failed, carry on with defaults) from a give-up, nor a retry
hand-off from a swallow. So every wrapped coroutine is RUN, through the real
``tracked_task``, with one dependency forced to fail, and the gate observes:

* ``rows`` — ``background_task_log`` writes, captured at the one storage call
  ``record_task_failure`` makes (so the wrapper's own row and a coroutine's
  explicit one are both seen);
* ``retries`` — tracked hand-offs to a rostered retry task (the bulk re-embed
  reschedules each item as ``reembed``, which is itself gated below);
* ``errors`` — ERROR-level log records: the code's own declaration that
  something failed.

The property: **a run that logged an error must leave a row or a retry.**
A run that logged nothing at ERROR is treated as a degradation it survived.

The call-site roster is the only static part, and it asserts nothing about
correctness: it enumerates every ``tracked_task(...)`` call in the tree so a
NEW wrapped coroutine fails here until someone writes a failure scenario for it
(or rosters an exclusion with its reason). Each scenario then checks at runtime
that the coroutine it drives is one that call site actually wraps.

Known limits, stated so nobody reads more into a green run than it says:

* Only exits the scenarios reach are observed. A new handler in a region no
  scenario faults is invisible until a scenario is added for it.
* A handler that swallows WITHOUT logging at ERROR (a bare ``pass``, or a
  ``warning``) passes. The class M-40 named is "catches, logs and returns";
  quieter swallows need a different oracle.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import pathlib
import uuid
from collections.abc import Callable, Coroutine
from contextlib import ExitStack
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from common.enrichment.schema import AtomicFact, EnrichmentResult
from core_api.constants import VECTOR_DIM
from core_api.services import memory_service, organization_settings, task_tracker
from tests._scoped_module import scoped
from tests.conftest import close_scheduled_coro

pytestmark = pytest.mark.asyncio

REPO = pathlib.Path(__file__).resolve().parents[1]
TENANT = f"test-m03-{uuid.uuid4().hex[:8]}"

#: Tracked hand-offs that ARE the durable answer to a failure: the task they
#: schedule is itself rostered and gated here, so the failure is not dropped,
#: it is moved.
RETRY_TASKS: dict[str, str] = {
    "reembed": "per-item re-embed; wraps _schedule_embed_or_reembed, gated below",
}


# ── observation ───────────────────────────────────────────────────────────


@dataclass
class Observed:
    wrapped: str
    rows: list[dict] = field(default_factory=list)
    retries: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def swallowed(self) -> bool:
        return bool(self.errors) and not self.rows and not self.retries


class _ErrorLog(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(f"{record.name}: {record.getMessage()}")


def _handoff_spy(obs: Observed) -> Callable[[Any], Any]:
    """``track_task`` stand-in: note which tracked task was handed off, then
    close it unstarted — running it would be a second scenario, not this one."""

    def spy(coro: Any) -> Any:
        frame = getattr(coro, "cr_frame", None)
        name = frame.f_locals.get("task_name") if frame is not None else None
        if name in RETRY_TASKS:
            obs.retries.append(name)
        return close_scheduled_coro(coro)

    return spy


async def _observe(
    stack: ExitStack, coro: Coroutine[Any, Any, Any], task_name: str
) -> Observed:
    obs = Observed(wrapped=coro.cr_code.co_qualname)
    sink = MagicMock()
    sink.add_task_failure = AsyncMock(side_effect=lambda row: obs.rows.append(row))
    stack.enter_context(patch.object(task_tracker, "get_storage_client", lambda: sink))
    stack.enter_context(
        patch.object(memory_service, "track_task", side_effect=_handoff_spy(obs))
    )
    handler = _ErrorLog()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        await task_tracker.tracked_task(coro, task_name, uuid.uuid4(), TENANT)
    finally:
        root.removeHandler(handler)
    obs.errors = handler.messages
    return obs


# ── fixtures shared by the scenarios ──────────────────────────────────────


async def _noop_sleep(_secs: float) -> None:
    return None


def _config(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "enrichment_enabled": True,
        "enrichment_provider": "fake",
        "entity_extraction_enabled": False,
        "auto_chunk_enabled": False,
        "atomic_fact_fanout_enabled": True,
        "entity_blocklist": [],
    }
    base.update(over)
    return SimpleNamespace(**base)


def _row(**over: Any) -> dict:
    base = {
        "id": str(uuid.uuid4()),
        "memory_type": "fact",
        "status": "active",
        "weight": 0.5,
        "ts_valid_start": None,
        "ts_valid_end": None,
        "metadata_": {},
        "deleted_at": None,
        "fleet_id": "f1",
        "embedding": None,
        "content": "body",
        "visibility": "scope_team",
    }
    base.update(over)
    return base


def _storage(**raises: BaseException) -> MagicMock:
    sc = MagicMock(name="storage_client")
    sc.get_memory = AsyncMock(return_value=_row())
    for method in ("update_embedding", "update_memory", "update_memory_status"):
        setattr(sc, method, AsyncMock(side_effect=raises.get(method)))
    return sc


def _memsvc_env(
    stack: ExitStack, *, mode: str, storage: MagicMock, config: Any = None
) -> None:
    for ctx in (
        patch.object(memory_service.settings, "deployment_mode", mode),
        patch.object(memory_service, "get_storage_client", lambda: storage),
        patch.object(memory_service, "asyncio", scoped(asyncio, sleep=_noop_sleep)),
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=config if config is not None else _config()),
        ),
    ):
        stack.enter_context(ctx)


def _fault(stack: ExitStack, target: Any, attr: str, mock: Any) -> Any:
    stack.enter_context(patch.object(target, attr, new=mock))
    return mock


def _boom() -> RuntimeError:
    """Fresh per use: a shared instance accumulates every traceback it passes."""
    return RuntimeError("injected fault")


_VEC = [0.0] * VECTOR_DIM


# ── scenarios: one dependency fails per scenario ──────────────────────────
#
# Each returns (coroutine to wrap, the fault mock). The harness asserts the
# fault was actually hit — a scenario that never reaches its fault proves
# nothing and must not pass as if it had.


def _embed_exhausted(stack: ExitStack):
    _memsvc_env(stack, mode="inline", storage=_storage())
    fault = _fault(stack, memory_service, "get_embedding", AsyncMock(return_value=None))
    return memory_service._schedule_embed_or_reembed(uuid.uuid4(), "c", TENANT), fault


def _embed_patch_lost(stack: ExitStack):
    sc = _storage(update_embedding=_boom())
    _memsvc_env(stack, mode="inline", storage=sc)
    _fault(stack, memory_service, "get_embedding", AsyncMock(return_value=_VEC))
    return (
        memory_service._schedule_embed_or_reembed(uuid.uuid4(), "c", TENANT),
        sc.update_embedding,
    )


def _embed_publish(stack: ExitStack):
    _memsvc_env(stack, mode="deferred", storage=_storage())
    fault = _fault(
        stack,
        memory_service,
        "publish_memory_embed_request",
        AsyncMock(side_effect=_boom()),
    )
    return memory_service._schedule_embed_or_reembed(uuid.uuid4(), "c", TENANT), fault


def _enrich(stack: ExitStack, storage: MagicMock, enrich: AsyncMock, cfg: Any = None):
    _memsvc_env(stack, mode="inline", storage=storage, config=cfg)
    stack.enter_context(
        patch("core_api.services.memory_enrichment.enrich_memory", new=enrich)
    )
    return memory_service._schedule_enrich_or_inline(
        uuid.uuid4(), "c", TENANT, "f1", "a", _config()
    )


def _enrich_config(stack: ExitStack):
    coro = _enrich(stack, _storage(), AsyncMock(return_value=EnrichmentResult()))
    fault = AsyncMock(side_effect=_boom())
    stack.enter_context(
        patch("core_api.services.organization_settings.resolve_config", new=fault)
    )
    return coro, fault


def _enrich_provider(stack: ExitStack):
    fault = AsyncMock(side_effect=_boom())
    return _enrich(stack, _storage(), fault), fault


def _enrich_persist(stack: ExitStack):
    sc = _storage(update_memory=_boom())
    coro = _enrich(stack, sc, AsyncMock(return_value=EnrichmentResult(title="t")))
    return coro, sc.update_memory


def _enrich_fanout(stack: ExitStack):
    result = EnrichmentResult(
        title="t", atomic_facts=[AtomicFact(content="a"), AtomicFact(content="b")]
    )
    coro = _enrich(stack, _storage(), AsyncMock(return_value=result))
    fault = _fault(
        stack, memory_service, "fan_out_atomic_facts", AsyncMock(side_effect=_boom())
    )
    return coro, fault


def _fanout_lookup(stack: ExitStack):
    sc = _storage()
    sc.bulk_find_by_content_hashes = AsyncMock(side_effect=_boom())
    coro = memory_service.fan_out_atomic_facts(
        sc,
        atomic_facts=[AtomicFact(content="a"), AtomicFact(content="b")],
        memory_id=uuid.uuid4(),
        tenant_id=TENANT,
        fleet_id="f1",
        agent_id="a",
        parent_metadata={},
        parent_visibility="scope_team",
        parent_weight=0.5,
        parent_ts_start=None,
        tenant_config=_config(),
    )
    return coro, sc.bulk_find_by_content_hashes


def _enrich_publish(stack: ExitStack):
    _memsvc_env(stack, mode="deferred", storage=_storage())
    fault = _fault(
        stack,
        memory_service,
        "publish_memory_enrich_request",
        AsyncMock(side_effect=_boom()),
    )
    return (
        memory_service._schedule_enrich_or_inline(
            uuid.uuid4(), "c", TENANT, "f1", "a", _config()
        ),
        fault,
    )


def _extraction(stack: ExitStack):
    from core_api.services import entity_extraction_worker as w

    stack.enter_context(
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=_config()),
        )
    )
    fault = _fault(
        stack, w, "extract_entities_from_content", AsyncMock(side_effect=_boom())
    )
    return w.process_entity_extraction(
        uuid.uuid4(), TENANT, "f1", "a", "c", "fact"
    ), fault


def _rerun_of(stack: ExitStack, storage: MagicMock):
    from core_api.services import extraction_rerun

    stack.enter_context(
        patch.object(extraction_rerun, "get_storage_client", lambda: storage)
    )
    return extraction_rerun._rerun(_row()["id"], TENANT)


def _rerun_read(stack: ExitStack):
    sc = _storage()
    sc.get_memory = AsyncMock(side_effect=_boom())
    return _rerun_of(stack, sc), sc.get_memory


def _rerun_reset(stack: ExitStack):
    sc = _storage()
    sc.reset_entity_artifacts = AsyncMock(side_effect=_boom())
    return _rerun_of(stack, sc), sc.reset_entity_artifacts


def _rerun_extraction(stack: ExitStack):
    from core_api.services import entity_extraction_worker as w

    stack.enter_context(
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=_config()),
        )
    )
    fault = _fault(
        stack, w, "extract_entities_from_content", AsyncMock(side_effect=_boom())
    )
    sc = _storage()
    sc.reset_entity_artifacts = AsyncMock()
    return _rerun_of(stack, sc), fault


def _bulk(stack: ExitStack, storage: MagicMock, batch: AsyncMock):
    _memsvc_env(stack, mode="inline", storage=storage)
    stack.enter_context(patch.object(memory_service, "get_embeddings_batch", new=batch))
    items = [(uuid.uuid4(), "c1"), (uuid.uuid4(), "c2")]
    return memory_service._reembed_memories_bulk(items, TENANT, "f1")


def _bulk_batch(stack: ExitStack):
    fault = AsyncMock(side_effect=_boom())
    return _bulk(stack, _storage(), fault), fault


def _bulk_patch(stack: ExitStack):
    sc = _storage(update_embedding=_boom())
    return _bulk(stack, sc, AsyncMock(return_value=[_VEC, _VEC])), sc.update_embedding


def _audit_critical(stack: ExitStack):
    from core_api.services import audit_service

    queue = MagicMock()
    queue.enqueue = MagicMock(return_value=False)
    stack.enter_context(patch.object(audit_service, "get_audit_queue", lambda: queue))
    fault = _fault(
        stack, audit_service, "_post_audit_sync", AsyncMock(side_effect=_boom())
    )
    coro = audit_service.log_action(
        tenant_id=TENANT,
        action="governance_reject",
        resource_type="memory",
        critical=True,
    )
    return coro, fault


def _remediation(stack: ExitStack):
    from core_api.services import governance_remediation as gr

    stack.enter_context(patch.object(gr, "get_storage_client", lambda: _storage()))
    fault = _fault(stack, gr, "_pre_verdict_children", AsyncMock(side_effect=_boom()))
    cfg = SimpleNamespace(
        governance_pii=SimpleNamespace(enabled=True, action="drop"),
        governance_non_business=SimpleNamespace(enabled=False, action="drop"),
    )
    memory = {
        "id": str(uuid.uuid4()),
        "content": "c",
        "tenant_id": TENANT,
        "agent_id": "a",
        "metadata_": {"contains_pii": True, "pii_types": ["email"]},
    }
    return gr.remediate_after_enrichment(memory, cfg), fault


def _enrich_publisher(stack: ExitStack):
    from common.events import memory_enrich_publisher as pub

    bus = MagicMock()
    bus.publish = AsyncMock(side_effect=_boom())
    stack.enter_context(patch.object(pub, "get_event_bus", lambda: bus))
    return pub.publish_memory_enrich_request(
        memory_id=uuid.uuid4(), content="c", tenant_id=TENANT
    ), bus.publish


def _ann_shadow(stack: ExitStack):
    from core_api.pipeline.steps.search import execute_scored_search as ess

    sc = MagicMock()
    sc.scored_search = AsyncMock(side_effect=_boom())
    coro = ess._run_ann_pool_shadow(
        sc, {}, [("m1", 0.9)], k=5, tenant_id=TENANT, primary_ms=1.0
    )
    return coro, sc.scored_search


def _contradiction(stack: ExitStack, *, engine: bool, trigger_name: str):
    from core_api.services import contradiction_detector as cd
    from core_api.services.contradiction import Trigger, run_contradiction_detection

    sc = _storage()
    sc.get_memory = AsyncMock(side_effect=_boom())
    stack.enter_context(patch.object(cd, "get_storage_client", lambda: sc))
    stack.enter_context(
        patch.object(memory_service.settings, "contradiction_engine_enabled", engine)
    )
    coro = run_contradiction_detection(
        uuid.uuid4(),
        TENANT,
        "f1",
        trigger=Trigger(trigger_name),
        content="c",
        embedding=_VEC,
    )
    return coro, sc.get_memory


def _reopen_sweep(stack: ExitStack):
    sc = MagicMock()
    sc.reset_dedup_checked = AsyncMock(side_effect=_boom())
    stack.enter_context(
        patch.object(organization_settings, "get_storage_client", lambda: sc)
    )
    return organization_settings._reopen_dedup_sweep(TENANT), sc.reset_dedup_checked


def _replay(stack: ExitStack, system: dict, *, cfg: Any = None, **over: Any):
    """``replay_released_write`` over one released row, under the tenant's ``cfg``."""
    from core_api.services import release_replay

    row = _row(metadata_={**over.pop("metadata_", {}), "_system": system}, **over)
    sc = _storage()
    sc.get_memory = AsyncMock(return_value=row)
    stack.enter_context(patch.object(release_replay, "get_storage_client", lambda: sc))
    stack.enter_context(
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=cfg if cfg is not None else _config()),
        )
    )
    return release_replay.replay_released_write(row["id"], TENANT)


def _replay_enrich(stack: ExitStack):
    stack.enter_context(
        patch.object(memory_service.settings, "deployment_mode", "deferred")
    )
    fault = _fault(
        stack,
        memory_service,
        "publish_memory_enrich_request",
        AsyncMock(side_effect=_boom()),
    )
    return _replay(stack, {"write_mode": "fast", "enrichment_pending": True}), fault


def _replay_remediation(stack: ExitStack):
    from core_api.services import governance_remediation as gr

    stack.enter_context(patch.object(gr, "get_storage_client", lambda: _storage()))
    fault = _fault(stack, gr, "_pre_verdict_children", AsyncMock(side_effect=_boom()))
    cfg = _config(
        governance_pii=SimpleNamespace(enabled=True, action="drop"),
        governance_non_business=SimpleNamespace(enabled=False, action="drop"),
    )
    coro = _replay(
        stack,
        {"write_mode": "fast"},
        cfg=cfg,
        metadata_={"contains_pii": True, "pii_types": ["email"]},
    )
    return coro, fault


def _replay_cascade(stack: ExitStack):
    from core_api.services import governance_remediation as gr

    cascade = gr.GovernanceCascadeError(
        "injected fault", gr.RemediationOutcome(visibility="scope_agent")
    )
    fault = _fault(
        stack, gr, "remediate_after_enrichment", AsyncMock(side_effect=cascade)
    )
    return _replay(stack, {"write_mode": "fast"}), fault


def _replay_fanout(stack: ExitStack):
    from core_api import consumer

    stack.enter_context(
        patch.object(consumer, "resolve_config", new=AsyncMock(return_value=_config()))
    )
    fault = _fault(
        stack, consumer, "fan_out_atomic_facts", AsyncMock(side_effect=_boom())
    )
    coro = _replay(
        stack,
        {"write_mode": "strong"},
        metadata_={"atomic_facts": [{"content": "a"}, {"content": "b"}]},
    )
    return coro, fault


def _replay_extraction(stack: ExitStack):
    from core_api.services import entity_extraction_worker as w

    fault = _fault(
        stack, w, "process_entity_extraction", AsyncMock(side_effect=_boom())
    )
    cfg = _config(entity_extraction_enabled=True)
    return _replay(stack, {"write_mode": "strong"}, cfg=cfg), fault


def _replay_contradiction(stack: ExitStack):
    from core_api.services import contradiction_detector as cd

    sc = _storage()
    sc.get_memory = AsyncMock(side_effect=_boom())
    stack.enter_context(patch.object(cd, "get_storage_client", lambda: sc))
    stack.enter_context(
        patch.object(memory_service.settings, "contradiction_engine_enabled", False)
    )
    return _replay(stack, {"write_mode": "strong"}, embedding=_VEC), sc.get_memory


@dataclass(frozen=True)
class Scenario:
    id: str
    task_name: str
    build: Callable[[ExitStack], tuple[Coroutine[Any, Any, Any], Any]]
    #: Set while the swallow is real and unfixed: the gate then asserts it is
    #: STILL swallowed, so whoever fixes it has to delete this and cannot
    #: forget to. The value says where it is tracked.
    known_open: str | None = None
    #: A swallow that is deliberate and cannot honestly be recorded. The run
    #: is still driven — the fault must be reached and the ERROR logged, since
    #: that log line is what the reason promises an operator will see.
    excluded: str | None = None


#: Shared by the critical-overflow audit scenario; kept beside the roster so it
#: is read with the rest of the exclusions.
_AUDIT_LOST_REASON = (
    "log_action's critical-overflow fallback runs only when the audit queue is "
    "full AND the synchronous storage POST has just failed. A "
    "background_task_log row is also a storage write, so it would fail the "
    "same way and could only ever be recorded when it was not needed. No "
    "non-storage durable signal exists to use instead: AuditQueue's "
    "dropped/failed counters are in-process and have no reader. What an "
    "operator sees is the ERROR line 'critical %r event LOST'."
)

#: Keyed by the callee as it is spelled at the ``tracked_task(...)`` call site.
#: ``wraps`` is what the coroutine's code object must be at runtime.
ROSTER: dict[str, dict[str, Any]] = {
    "_schedule_embed_or_reembed": {
        "wraps": {"_schedule_embed_or_reembed"},
        "scenarios": [
            Scenario("embed-exhausted", "embed_or_publish", _embed_exhausted),
            Scenario("embed-patch-lost", "embed_or_publish", _embed_patch_lost),
            Scenario("embed-publish", "embed_or_publish", _embed_publish),
        ],
    },
    "_schedule_enrich_or_inline": {
        "wraps": {"_schedule_enrich_or_inline"},
        "scenarios": [
            Scenario("enrich-config", "enrich_or_publish", _enrich_config),
            Scenario("enrich-provider", "enrich_or_publish", _enrich_provider),
            Scenario("enrich-persist", "enrich_or_publish", _enrich_persist),
            Scenario(
                "enrich-fanout",
                "enrich_or_publish",
                _enrich_fanout,
            ),
            Scenario("enrich-publish", "enrich_or_publish", _enrich_publish),
        ],
    },
    "fan_out_atomic_facts": {
        # Wrapped directly after a strong write (L-117). The fast path reaches
        # it inside ``_schedule_enrich_or_inline``: ``enrich-fanout`` above.
        "wraps": {"fan_out_atomic_facts"},
        "scenarios": [
            Scenario("fanout-dedup-lookup", "atomic_fact_fanout", _fanout_lookup)
        ],
    },
    "process_entity_extraction": {
        "wraps": {"process_entity_extraction"},
        "scenarios": [Scenario("extraction", "entity_extraction", _extraction)],
    },
    "_rerun": {
        # The re-run sweep's read, reset, then extraction (services.extraction_rerun).
        "wraps": {"_rerun"},
        "scenarios": [
            Scenario("rerun-read", "entity_extraction", _rerun_read),
            Scenario("rerun-reset", "entity_extraction", _rerun_reset),
            Scenario("rerun-extraction", "entity_extraction", _rerun_extraction),
        ],
    },
    "_reembed_memories_bulk": {
        "wraps": {"_reembed_memories_bulk"},
        "scenarios": [
            Scenario("bulk-batch", "reembed_bulk[2]", _bulk_batch),
            Scenario("bulk-patch", "reembed_bulk[2]", _bulk_patch),
        ],
    },
    "_hooks.audit_log": {
        # The OSS implementation ``app.py`` configures into the hook.
        "wraps": {"log_action"},
        "scenarios": [
            Scenario(
                "audit-critical-lost",
                "audit_log",
                _audit_critical,
                excluded=_AUDIT_LOST_REASON,
            )
        ],
    },
    "remediate_after_enrichment": {
        "wraps": {"remediate_after_enrichment"},
        "scenarios": [Scenario("remediation", "governance_remediation", _remediation)],
    },
    "publish_memory_enrich_request": {
        "wraps": {"publish_memory_enrich_request"},
        "scenarios": [
            Scenario("enrich-publisher", "enrich_publish", _enrich_publisher)
        ],
    },
    "_run_ann_pool_shadow": {
        "wraps": {"_run_ann_pool_shadow"},
        "scenarios": [Scenario("ann-shadow", "ann_pool_shadow", _ann_shadow)],
    },
    "_reopen_dedup_sweep": {
        "wraps": {"_reopen_dedup_sweep"},
        "scenarios": [
            Scenario("reopen-sweep", "crystallizer_reopen_sweep", _reopen_sweep)
        ],
    },
    "run_contradiction_detection": {
        # A sync function returning the detector coroutine, so what tracked_task
        # actually awaits is one of these.
        "wraps": {
            "detect_contradictions_async",
            "detect_contradictions_by_entities_async",
            "ContradictionEngine.evaluate_async",
        },
        "scenarios": [
            Scenario(
                "contradiction-path-a",
                "contradiction_detection",
                lambda s: _contradiction(s, engine=False, trigger_name="write"),
            ),
            Scenario(
                "contradiction-path-c",
                "contradiction_detection",
                lambda s: _contradiction(s, engine=False, trigger_name="entity"),
            ),
            Scenario(
                "contradiction-engine-path-a",
                "contradiction_detection",
                lambda s: _contradiction(s, engine=True, trigger_name="write"),
            ),
        ],
    },
    "replay_released_write": {
        # g2.8. The replay goes on past a failed step, so one scenario per step:
        # none of these raises to the wrapper, and each must still leave a row.
        "wraps": {"replay_released_write"},
        "scenarios": [
            Scenario("release-replay-enrich", "release_replay", _replay_enrich),
            Scenario(
                "release-replay-remediation", "release_replay", _replay_remediation
            ),
            Scenario("release-replay-cascade", "release_replay", _replay_cascade),
            Scenario("release-replay-fanout", "release_replay", _replay_fanout),
            Scenario("release-replay-extraction", "release_replay", _replay_extraction),
            Scenario(
                "release-replay-contradiction",
                "release_replay",
                _replay_contradiction,
            ),
        ],
    },
}

#: Call-site callees that are deliberately NOT driven, each with its reason.
EXCLUDED: dict[str, str] = {}

_ALL = [(key, s) for key, entry in ROSTER.items() for s in entry["scenarios"]]


# ── the roster source: every tracked_task call site ───────────────────────


def _tracked_task_callees() -> dict[str, list[str]]:
    """Every ``tracked_task(...)`` call in shipped code → where it is."""
    found: dict[str, list[str]] = {}
    for root in ("core-api/src", "common"):
        for path in sorted((REPO / root).rglob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and getattr(node.func, "id", None) == "tracked_task"
                    and node.args
                ):
                    continue
                arg = node.args[0]
                callee = ast.unparse(arg.func if isinstance(arg, ast.Call) else arg)
                where = f"{path.relative_to(REPO)}:{node.lineno}"
                found.setdefault(callee, []).append(where)
    return found


def test_every_wrapped_coroutine_is_rostered() -> None:
    """Roster completeness only — the property is asserted by the runs below.

    A new ``tracked_task(...)`` call wrapping a coroutine not named here fails
    until it gets a failure scenario (or an exclusion with a reason)."""
    callees = _tracked_task_callees()
    rostered = set(ROSTER) | set(EXCLUDED)
    unrostered = {c: callees[c] for c in callees.keys() - rostered}
    assert not unrostered, (
        f"tracked_task wraps coroutines with no failure scenario: {unrostered}"
    )
    stale = rostered - callees.keys()
    assert not stale, f"rostered but no longer wrapped anywhere: {sorted(stale)}"
    assert all(ROSTER[k]["scenarios"] for k in ROSTER)
    assert all(reason.strip() for reason in EXCLUDED.values())


@pytest.mark.parametrize(("key", "scenario"), _ALL, ids=[s.id for _, s in _ALL])
async def test_a_failure_leaves_a_record(key: str, scenario: Scenario) -> None:
    with ExitStack() as stack:
        coro, fault = scenario.build(stack)
        obs = await _observe(stack, coro, scenario.task_name)

    assert obs.wrapped in ROSTER[key]["wraps"], (
        f"{scenario.id}: drove {obs.wrapped}, which {key} call sites do not wrap"
    )
    assert fault.called, f"{scenario.id}: the injected fault was never reached"
    assert obs.errors, (
        f"{scenario.id}: no ERROR was logged, so this run cannot tell a give-up "
        "from a survived degradation — pick a fault the code treats as failure"
    )

    if scenario.excluded:
        assert any("LOST" in m for m in obs.errors), (
            f"{scenario.id}: excluded on the promise of an ERROR an operator "
            f"can see, and none was logged ({scenario.excluded})"
        )
        return

    if scenario.known_open:
        assert obs.swallowed, (
            f"{scenario.id} is recorded now — delete its known_open entry "
            f"({scenario.known_open})"
        )
        return

    assert not obs.swallowed, (
        f"tracked task {scenario.task_name!r} ({key} -> {obs.wrapped}, scenario "
        f"{scenario.id}) logged a failure and returned normally with no "
        f"background_task_log row and no retry: {obs.errors}"
    )
