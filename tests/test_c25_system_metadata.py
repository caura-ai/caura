"""C25 — the platform/caller metadata boundary (MemoryImpact C9 / AX-audit N8).

Enrichment used to write its telemetry straight into the caller's metadata
dict: a caller's own ``metadata.summary`` was silently overwritten, and a
caller-supplied ``llm_ms`` survived as fake telemetry when enrichment didn't
run. Now: platform values land in ``metadata["_system"]`` (mirrored to the
legacy top-level keys for one release unless caller-owned), forgeable
telemetry keys are stripped from caller input, and ``MemoryOut.system_metadata``
exposes the platform view for new AND historical rows.
"""

import pytest

from core_api.services.system_metadata import (
    CALLER_OWNABLE_KEYS,
    PLATFORM_ONLY_KEYS,
    SYSTEM_NAMESPACE,
    extract_system_metadata,
    sanitize_caller_metadata,
    set_system_value,
)

pytestmark = pytest.mark.unit


# --- sanitize: forgeable keys stripped, caller keys kept -----------------------


def test_sanitize_strips_platform_only_keys():
    dirty = {"llm_ms": 9999, "write_latency_ms": 1, "note": "mine", "_system": {"x": 1}}
    clean = sanitize_caller_metadata(dirty)
    assert clean == {"note": "mine"}


def test_sanitize_keeps_caller_ownable_keys():
    clean = sanitize_caller_metadata({"summary": "MINE", "tags": ["a"], "k": 1})
    assert clean == {"summary": "MINE", "tags": ["a"], "k": 1}


def test_sanitize_none_and_empty():
    assert sanitize_caller_metadata(None) == {}
    assert sanitize_caller_metadata({}) == {}


# --- set_system_value: dual-write + clobber fix --------------------------------


def test_platform_key_dual_written():
    md: dict = {}
    set_system_value(md, "llm_ms", 123)
    assert md["llm_ms"] == 123  # legacy mirror (one release)
    assert md[SYSTEM_NAMESPACE]["llm_ms"] == 123


def test_caller_owned_summary_not_clobbered():
    md: dict = {"summary": "MINE"}
    set_system_value(md, "summary", "platform version", caller_keys=frozenset(md))
    assert md["summary"] == "MINE"  # the N8 clobber fix
    assert md[SYSTEM_NAMESPACE]["summary"] == "platform version"


def test_summary_fills_top_level_when_caller_did_not_set_it():
    md: dict = {}
    set_system_value(md, "summary", "platform version", caller_keys=frozenset())
    assert md["summary"] == "platform version"
    assert md[SYSTEM_NAMESPACE]["summary"] == "platform version"


# --- extract: read-side view for new and historical rows -----------------------


def test_extract_from_historical_row_legacy_keys_only():
    md = {"summary": "s", "llm_ms": 42, "custom": "keep-out"}
    sysm = extract_system_metadata(md)
    assert sysm == {"summary": "s", "llm_ms": 42}


def test_extract_prefers_namespace_over_legacy():
    md = {"summary": "CALLER", SYSTEM_NAMESPACE: {"summary": "PLATFORM"}}
    assert extract_system_metadata(md)["summary"] == "PLATFORM"


def test_extract_none_when_nothing_platform_written():
    assert extract_system_metadata({"custom": 1}) is None
    assert extract_system_metadata(None) is None
    assert extract_system_metadata({}) is None


# --- merge step end-to-end ------------------------------------------------------


class _Enrichment:
    memory_type = "fact"
    weight = 0.5
    title = "t"
    summary = "PLATFORM SUMMARY"
    tags = ["p1"]
    llm_ms = 77
    ts_valid_start = None
    ts_valid_end = None
    contains_pii = False
    pii_types = None
    business_relevance = "business"
    status = None


class _Input:
    memory_type = None
    weight = None
    ts_valid_start = None
    ts_valid_end = None
    status = None

    def __init__(self, metadata):
        self.metadata = metadata


async def test_merge_step_preserves_caller_summary():
    """Input arrives PRE-SANITIZED (create_memory chokepoint strips forgeries
    before the governance gate); the merge step's job is the clobber fix."""
    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.write.merge_enrichment_fields import MergeEnrichmentFields

    ctx = PipelineContext(
        data={
            "input": _Input(sanitize_caller_metadata({"summary": "MINE", "llm_ms": 9999, "custom": "kept"})),
            "enrichment": _Enrichment(),
            "resolved_write_mode": "strong",
        }
    )
    await MergeEnrichmentFields().execute(ctx)
    md = ctx.data["memory_fields"]["metadata"]
    assert md["summary"] == "MINE"  # caller wins at top level
    assert md[SYSTEM_NAMESPACE]["summary"] == "PLATFORM SUMMARY"
    assert md["llm_ms"] == 77  # forged 9999 stripped at entry; real telemetry recorded
    assert md[SYSTEM_NAMESPACE]["llm_ms"] == 77
    assert md["custom"] == "kept"
    assert md["tags"] == ["p1"]  # caller didn't own tags → legacy mirror filled


async def test_merge_step_does_not_clobber_upstream_gate_flags():
    """The governance gate writes PII flags into the input metadata BEFORE the
    merge step — the regression that moved sanitize to the entry chokepoint."""
    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.write.merge_enrichment_fields import MergeEnrichmentFields

    gate_written = {"contains_pii": True, "pii_types": ["email"], SYSTEM_NAMESPACE: {"contains_pii": True, "pii_types": ["email"]}}
    ctx = PipelineContext(
        data={"input": _Input(dict(gate_written)), "enrichment": None, "resolved_write_mode": "fast"}
    )
    await MergeEnrichmentFields().execute(ctx)
    md = ctx.data["memory_fields"]["metadata"]
    assert md["contains_pii"] is True
    assert md["pii_types"] == ["email"]


def test_registries_are_disjoint():
    assert not (PLATFORM_ONLY_KEYS & CALLER_OWNABLE_KEYS)
