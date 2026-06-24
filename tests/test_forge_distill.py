"""Forge distill tests (Skill Factory SF-104).

Two test surfaces:

  * Prompt construction + response parsing (pure functions in
    :mod:`distill_prompt`).
  * Service orchestration (in :mod:`forge_service`) with the LLM,
    memory fetcher, poison checker, and candidate writer all
    injected as fakes — so the test runs hermetic with no DB and no
    network.

Coverage targets:
  - The prompt carries every required input section.
  - The response parser is robust to JSON variants the LLM commonly
    leaks (code fences, leading prose) and strict on the schema.
  - Clustering by entity Jaccard produces deterministic, separable
    clusters.
  - Auto-gates (min_cluster_size, min_distinct_agents) drop weak clusters.
  - Poison check short-circuits a cluster without writing.
  - Distill-error skip doesn't abort the run.
  - Candidate docs land with status=candidate, source=forge, the
    fingerprint stamped, and the cites list deduped + sorted.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from core_api.services.forge.distill_prompt import (
    DISTILL_SCHEMA_VERSION,
    ClusterPromptInputs,
    DistillParseError,
    TraceSnapshot,
    build_distill_prompt,
    parse_distill_response,
)
from core_api.services.forge.fingerprint import (
    FINGERPRINT_FORMULA_VERSION,
)
from core_api.services.forge.forge_service import (
    ForgeConfig,
    _cluster_by_entity_overlap,
    _gate_clusters,
    run_forge_distill,
)
from core_api.services.session_trace import SessionTraceRow


# ── Helpers ────────────────────────────────────────────────────────


def _trace(
    run_id: str = "r1",
    agent_id: str = "a1",
    outcome: str = "success",
    entity_ids: list[str] | None = None,
    memory_ids: list[str] | None = None,
    started: datetime | None = None,
    ended: datetime | None = None,
) -> SessionTraceRow:
    # `is None` (not `or`) so an explicit empty list survives —
    # tests need to differentiate "no entities supplied, take default"
    # from "explicitly entity-less, exercise the drop path".
    return SessionTraceRow(
        tenant_id="t1",
        fleet_id="f1",
        run_id=run_id,
        agent_id=agent_id,
        outcome_label=outcome,
        memory_ids=memory_ids if memory_ids is not None else [f"{run_id}-m1"],
        entity_ids=entity_ids if entity_ids is not None else ["e1", "e2"],
        signals_summary={},
        started_at=started or datetime(2026, 5, 1, tzinfo=timezone.utc),
        ended_at=ended or datetime(2026, 5, 2, tzinfo=timezone.utc),
        goal_phrase=None,
    )


def _golden_llm_response(**overrides) -> dict[str, Any]:
    base = {
        "schema_version": DISTILL_SCHEMA_VERSION,
        "kind": "create",
        "goal_phrase": "deploy eu-west fallback dns step 7",
        "domain": "devops",
        "step_skeleton": [
            "run preflight check",
            "deploy script v4",
            "switch fallback dns",
            "verify rollout",
        ],
        "name": "Deploy to eu-west · fallback DNS at step 7",
        "slug": "deploy-eu-west-dns",
        "description": "Use fallback DNS resolver when eu-west deploy step 7 hangs.",
        "summary": "Detects a hung step 7 on eu-west deploys and switches to the "
                   "fallback DNS resolver before retrying.",
        "content": "## When to use\n…\n## Steps\n1. …",
        "tags": ["deploy", "eu-west", "dns"],
        "evidence": "4 sessions / 3 agents in Apr 18–28, 100% success when applied.",
        "goal": "Deploy to eu-west without hanging on step 7.",
    }
    base.update(overrides)
    return base


# ── Prompt construction ───────────────────────────────────────────


@pytest.mark.unit
class TestBuildDistillPrompt:
    def test_prompt_contains_all_traces(self):
        snaps = [
            TraceSnapshot(
                run_id=f"r{i}", agent_id=f"a{i}", outcome_label="success",
                memory_excerpts=[f"trace {i} content excerpt"],
                entity_ids=["e1"],
                started_at_iso="2026-05-01T00:00:00+00:00",
                ended_at_iso="2026-05-01T01:00:00+00:00",
            )
            for i in range(3)
        ]
        prompt = build_distill_prompt(ClusterPromptInputs(
            tenant_id="t1", fleet_id="f1", traces=snaps,
        ))
        assert "3 trace(s)" in prompt
        # Each trace appears as a labeled block.
        for i in range(3):
            assert f"Trace {i+1}/3" in prompt
            assert f"r{i}" in prompt
            assert f"a{i}" in prompt

    def test_schema_version_pinned_in_preamble(self):
        prompt = build_distill_prompt(ClusterPromptInputs(
            tenant_id="t1", fleet_id=None, traces=[]
        ))
        assert DISTILL_SCHEMA_VERSION in prompt
        # Sanity: schema_version is mentioned at least twice
        # (preamble + footer).
        assert prompt.count(DISTILL_SCHEMA_VERSION) >= 2

    def test_top_entities_listed(self):
        prompt = build_distill_prompt(ClusterPromptInputs(
            tenant_id="t1", fleet_id="f1", traces=[],
            top_entity_ids=["mysql", "deploy", "step7"],
        ))
        # Section renamed to make the UUID nature of the values explicit.
        assert "Top entity IDs (UUIDs)" in prompt
        # Sorted + deduped.
        assert "deploy" in prompt and "mysql" in prompt and "step7" in prompt

    def test_hint_domain_included_when_present(self):
        prompt = build_distill_prompt(ClusterPromptInputs(
            tenant_id="t1", fleet_id=None, traces=[], hint_domain="security",
        ))
        assert "Suggested domain" in prompt
        assert "security" in prompt

    def test_outcome_label_per_trace(self):
        snap = TraceSnapshot(
            run_id="r1", agent_id="a1", outcome_label="failure",
            memory_excerpts=["..."],
            entity_ids=["e1"],
            started_at_iso="2026-05-01T00:00:00+00:00",
            ended_at_iso="2026-05-01T01:00:00+00:00",
        )
        prompt = build_distill_prompt(ClusterPromptInputs(
            tenant_id="t1", fleet_id=None, traces=[snap]
        ))
        assert "outcome: failure" in prompt

    def test_entity_ids_sorted_in_prompt(self):
        # Sorted to keep the prompt itself deterministic for the
        # same cluster (same prompt → same LLM output assuming
        # temperature=0).
        snap = TraceSnapshot(
            run_id="r1", agent_id="a1", outcome_label="success",
            memory_excerpts=[],
            entity_ids=["z-ent", "a-ent", "m-ent"],
            started_at_iso="2026-05-01T00:00:00+00:00",
            ended_at_iso="2026-05-01T01:00:00+00:00",
        )
        prompt = build_distill_prompt(ClusterPromptInputs(
            tenant_id="t1", fleet_id=None, traces=[snap]
        ))
        line = next(l for l in prompt.splitlines() if l.startswith("entities:"))
        assert "a-ent" in line and "m-ent" in line and "z-ent" in line
        assert line.index("a-ent") < line.index("m-ent") < line.index("z-ent")


# ── Response parsing ──────────────────────────────────────────────


@pytest.mark.unit
class TestParseDistillResponse:
    def test_happy_path(self):
        raw = json.dumps(_golden_llm_response())
        parsed = parse_distill_response(raw)
        assert parsed["goal_phrase"]
        assert parsed["domain"] == "devops"
        assert parsed["kind"] == "create"
        assert isinstance(parsed["step_skeleton"], list)

    def test_strips_code_fence(self):
        raw = "```json\n" + json.dumps(_golden_llm_response()) + "\n```"
        parsed = parse_distill_response(raw)
        assert parsed["domain"] == "devops"

    def test_strips_bare_code_fence(self):
        raw = "```\n" + json.dumps(_golden_llm_response()) + "\n```"
        parsed = parse_distill_response(raw)
        assert parsed["domain"] == "devops"

    def test_strips_leading_prose(self):
        # LLMs sometimes prepend "Here's the JSON:" or similar.
        raw = "Here is the skill:\n" + json.dumps(_golden_llm_response())
        parsed = parse_distill_response(raw)
        assert parsed["domain"] == "devops"

    def test_strips_trailing_prose(self):
        # LLMs ALSO sometimes append "Let me know if you want
        # changes!" or similar after a closing brace. The previous
        # regex-based extractor (anchored on $) failed this case;
        # the JSONDecoder.raw_decode path handles it.
        raw = json.dumps(_golden_llm_response()) + "\n\nLet me know if you want changes!"
        parsed = parse_distill_response(raw)
        assert parsed["domain"] == "devops"

    def test_strips_both_leading_and_trailing_prose(self):
        raw = (
            "Here's your skill:\n"
            + json.dumps(_golden_llm_response())
            + "\nHappy to iterate."
        )
        parsed = parse_distill_response(raw)
        assert parsed["domain"] == "devops"

    def test_extracts_when_response_has_braces_in_strings(self):
        # The greedy regex previously used (``{.*}\s*$``) would
        # incorrectly snap onto a literal ``}`` inside a string
        # value. JSONDecoder.raw_decode handles balanced braces
        # natively.
        body = _golden_llm_response()
        body["content"] = "snippet with a {literal} brace {pair} inside"
        raw = "Prefix prose. " + json.dumps(body) + " More trailing prose."
        parsed = parse_distill_response(raw)
        assert parsed["content"] == "snippet with a {literal} brace {pair} inside"

    def test_empty_raises(self):
        with pytest.raises(DistillParseError):
            parse_distill_response("")
        with pytest.raises(DistillParseError):
            parse_distill_response("   ")

    def test_non_json_raises(self):
        with pytest.raises(DistillParseError):
            parse_distill_response("not json at all")

    def test_not_an_object_raises(self):
        with pytest.raises(DistillParseError):
            parse_distill_response("[1, 2, 3]")

    def test_wrong_schema_version_raises(self):
        bad = _golden_llm_response(schema_version="v999")
        with pytest.raises(DistillParseError) as e:
            parse_distill_response(json.dumps(bad))
        assert "schema_version" in str(e.value)

    def test_missing_required_key_raises(self):
        bad = _golden_llm_response()
        del bad["description"]
        with pytest.raises(DistillParseError) as e:
            parse_distill_response(json.dumps(bad))
        assert "description" in str(e.value)

    def test_string_field_must_be_string(self):
        bad = _golden_llm_response()
        bad["description"] = ["should be a string"]  # type: ignore
        with pytest.raises(DistillParseError):
            parse_distill_response(json.dumps(bad))

    def test_list_field_must_be_list_of_strings(self):
        bad = _golden_llm_response()
        bad["step_skeleton"] = "not a list"  # type: ignore
        with pytest.raises(DistillParseError):
            parse_distill_response(json.dumps(bad))

        bad2 = _golden_llm_response()
        bad2["step_skeleton"] = ["ok", 42]  # type: ignore
        with pytest.raises(DistillParseError):
            parse_distill_response(json.dumps(bad2))

    def test_kind_defaults_to_create_when_absent(self):
        body = _golden_llm_response()
        del body["kind"]
        parsed = parse_distill_response(json.dumps(body))
        assert parsed["kind"] == "create"

    def test_invalid_kind_rejected(self):
        bad = _golden_llm_response(kind="patch")
        with pytest.raises(DistillParseError):
            parse_distill_response(json.dumps(bad))

    def test_kind_update_passes_parser_but_forge_rejects(self):
        # The parser is permissive (kind ∈ {create, update}) because
        # kind=update is a legitimate Phase-4 flow once v2-diff cards
        # land. _distill_cluster has its own Phase-1 invariant that
        # blocks update — covered by TestForgeRunResilience below.
        bad = _golden_llm_response(kind="update")
        parsed = parse_distill_response(json.dumps(bad))
        assert parsed["kind"] == "update"

    @pytest.mark.parametrize(
        "bad_slug",
        [
            "Deploy-Eu-West",          # uppercase
            "deploy eu west",          # spaces
            "-deploy-eu-west",         # leading punctuation
            ".deploy",                 # leading dot
            "deploy/eu/west",          # forward slash (route prefixes that itself)
            "deploy@eu",               # at-sign
            "",                        # empty
            "a" * 101,                 # too long
        ],
    )
    def test_invalid_slug_format_rejected(self, bad_slug):
        """Malformed LLM slugs raise DistillParseError BEFORE the
        candidate_writer gets a chance to fail at the storage layer.
        The route's ``_SKILL_SLUG_RE`` governs the doc_id wrapper;
        this parser-level check governs ``data.slug`` itself."""
        bad = _golden_llm_response(slug=bad_slug)
        with pytest.raises(DistillParseError) as exc:
            parse_distill_response(json.dumps(bad))
        assert "slug" in str(exc.value).lower()

    def test_valid_slug_formats_accepted(self):
        # Lowercase alphanumeric + . _ -, starting with a-z0-9.
        for ok in ("deploy", "deploy-eu-west", "deploy.v4", "deploy_v4", "a", "0deploy"):
            parsed = parse_distill_response(json.dumps(_golden_llm_response(slug=ok)))
            assert parsed["slug"] == ok

    def test_system_preamble_carries_slug_instruction(self):
        # Pin that the prompt itself instructs the model on the slug
        # format. Without this, the LLM has no signal — we'd get an
        # unhelpfully-high rejection rate at parse time downstream.
        from core_api.services.forge.distill_prompt import _SYSTEM_PREAMBLE_FILLED
        assert "slug" in _SYSTEM_PREAMBLE_FILLED
        assert "lowercase-kebab-case" in _SYSTEM_PREAMBLE_FILLED
        assert "[a-z0-9]" in _SYSTEM_PREAMBLE_FILLED


@pytest.mark.unit
class TestForgeDryRunFakeLLMFallback:
    """The ``scripts/forge_dry_run.py`` CLI's docstring promises a
    fake-LLM fallback "for environments without provider keys
    configured". The fallback must (a) trigger on ImportError, (b)
    produce JSON that ``parse_distill_response`` accepts. Without
    this, an operator running the CLI on a packaging variant
    without ``common.llm`` would hit an unhelpful ImportError
    instead of getting placeholder smoke output."""

    @pytest.mark.asyncio
    async def test_fake_llm_produces_parseable_response(self):
        # Import the fake-LLM callable directly from the script
        # (not the wire function, which depends on real or fake
        # branch selection at runtime).
        import importlib.util
        import pathlib

        script_path = pathlib.Path("scripts/forge_dry_run.py")
        spec = importlib.util.spec_from_file_location("forge_dry_run", script_path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        raw = await mod._fake_llm_fn("ignored prompt")
        # Must be a parseable distill response.
        parsed = parse_distill_response(raw)
        assert parsed["kind"] == "create"
        # Required schema keys all present.
        for key in (
            "goal_phrase", "domain", "step_skeleton", "name", "slug",
            "description", "summary", "content", "tags", "evidence", "goal",
        ):
            assert key in parsed, f"fake-LLM missing required key: {key}"

    @pytest.mark.asyncio
    async def test_fake_llm_each_call_distinct_slug(self):
        # Two calls must yield distinct slugs so a multi-cluster run
        # doesn't collide on doc_id.
        import importlib.util
        import pathlib

        script_path = pathlib.Path("scripts/forge_dry_run.py")
        spec = importlib.util.spec_from_file_location("forge_dry_run_2", script_path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # Reset the counter so we have a deterministic starting point.
        mod._FAKE_LLM_COUNTER["n"] = 0

        raw1 = await mod._fake_llm_fn("")
        raw2 = await mod._fake_llm_fn("")
        slug1 = parse_distill_response(raw1)["slug"]
        slug2 = parse_distill_response(raw2)["slug"]
        assert slug1 != slug2
        # Both still match the route's slug regex.
        from core_api.services.forge.distill_prompt import _SLUG_RE
        assert _SLUG_RE.fullmatch(slug1)
        assert _SLUG_RE.fullmatch(slug2)


# ── Clustering ────────────────────────────────────────────────────


@pytest.mark.unit
class TestEntityJaccardClustering:
    def test_no_traces_no_clusters(self):
        assert _cluster_by_entity_overlap([], jaccard_threshold=0.4) == []

    def test_traces_without_entities_dropped(self):
        traces = [_trace(entity_ids=[]) for _ in range(3)]
        assert _cluster_by_entity_overlap(traces, jaccard_threshold=0.4) == []

    def test_high_overlap_traces_same_cluster(self):
        traces = [
            _trace(run_id="r1", entity_ids=["e1", "e2", "e3"]),
            _trace(run_id="r2", entity_ids=["e1", "e2", "e3"]),
            _trace(run_id="r3", entity_ids=["e1", "e2", "e4"]),  # 2/4 = 0.5 overlap
        ]
        clusters = _cluster_by_entity_overlap(traces, jaccard_threshold=0.4)
        assert len(clusters) == 1
        assert len(clusters[0]) == 3

    def test_disjoint_traces_separate_clusters(self):
        traces = [
            _trace(run_id="r1", entity_ids=["e1", "e2"]),
            _trace(run_id="r2", entity_ids=["e3", "e4"]),
        ]
        clusters = _cluster_by_entity_overlap(traces, jaccard_threshold=0.4)
        assert len(clusters) == 2

    def test_threshold_governs_inclusion(self):
        traces = [
            _trace(run_id="r1", entity_ids=["e1", "e2", "e3", "e4"]),
            _trace(run_id="r2", entity_ids=["e1"]),  # 1/4 = 0.25
        ]
        loose = _cluster_by_entity_overlap(traces, jaccard_threshold=0.2)
        strict = _cluster_by_entity_overlap(traces, jaccard_threshold=0.4)
        assert len(loose) == 1  # 0.25 ≥ 0.2 → same cluster
        assert len(strict) == 2  # 0.25 < 0.4 → separate clusters


# ── Auto-gates ────────────────────────────────────────────────────


@pytest.mark.unit
class TestClusterGates:
    def test_min_cluster_size_filter(self):
        clusters = [
            [_trace(run_id=f"r{i}", agent_id=f"a{i}") for i in range(2)],   # too small
            [_trace(run_id=f"r{i}", agent_id=f"a{i}") for i in range(3, 7)],  # 4 traces
        ]
        eligible = _gate_clusters(clusters, min_cluster_size=3, min_distinct_agents=3)
        assert len(eligible) == 1
        assert len(eligible[0]) == 4

    def test_min_distinct_agents_filter(self):
        # 5 traces but all same agent — anti-poison.
        single_agent = [_trace(run_id=f"r{i}", agent_id="alice") for i in range(5)]
        eligible = _gate_clusters([single_agent], min_cluster_size=3, min_distinct_agents=3)
        assert eligible == []

        # 5 traces from 3 agents — OK.
        diverse = [
            _trace(run_id="r1", agent_id="alice"),
            _trace(run_id="r2", agent_id="alice"),
            _trace(run_id="r3", agent_id="bob"),
            _trace(run_id="r4", agent_id="carol"),
            _trace(run_id="r5", agent_id="carol"),
        ]
        eligible = _gate_clusters([diverse], min_cluster_size=3, min_distinct_agents=3)
        assert len(eligible) == 1


# ── Full orchestration with mocked I/O ────────────────────────────


class _MockDb:
    """Minimal stand-in. session_trace builder + memory_fetcher are
    the only callers; we patch both at test time so the DB is
    never actually queried inside run_forge_distill tests."""


def _passing_traces(n: int) -> list[SessionTraceRow]:
    """n traces, all sharing entities, each from a different agent
    so they pass volume + diversity gates."""
    return [
        _trace(
            run_id=f"r{i}",
            agent_id=f"agent{i}",
            entity_ids=["e1", "e2", "e3"],
            memory_ids=[f"m{i}-1", f"m{i}-2"],
        )
        for i in range(n)
    ]


async def _llm_returns_golden(_prompt: str) -> str:
    return json.dumps(_golden_llm_response())


async def _llm_returns_bogus(_prompt: str) -> str:
    return "not json"


async def _memory_fetcher_always(memory_ids: list[str]) -> dict[str, str]:
    return {mid: f"content of {mid}" for mid in memory_ids}


async def _poison_never(_fp: str) -> bool:
    return False


async def _poison_always(_fp: str) -> bool:
    return True


def _capture_writer() -> tuple[list[dict[str, Any]], Any]:
    captured: list[dict[str, Any]] = []

    async def writer(doc: dict[str, Any]) -> None:
        captured.append(doc)

    return captured, writer


@pytest.mark.unit
class TestForgeRunOrchestration:
    def _patch_build(self, monkeypatch, traces: list[SessionTraceRow]):
        """Stub out the session-trace builder so we don't need DB."""
        async def fake_build(*_args, **_kwargs):
            return traces
        import core_api.services.forge.forge_service as svc
        monkeypatch.setattr(svc, "build_session_traces", fake_build)

    @pytest.mark.asyncio
    async def test_no_traces_no_candidates(self, monkeypatch):
        self._patch_build(monkeypatch, [])
        captured, writer = _capture_writer()
        result = await run_forge_distill(            run_label="test-run",
            tenant_id="t1", fleet_id="f1",
            window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 5, 15, tzinfo=timezone.utc),
            llm_fn=_llm_returns_golden,
            memory_fetcher=_memory_fetcher_always,
            poison_checker=_poison_never,
            candidate_writer=writer,
        )
        assert result.total_traces == 0
        assert result.candidates_written == 0
        assert captured == []

    @pytest.mark.asyncio
    async def test_happy_path_writes_one_candidate(self, monkeypatch):
        # 4 traces, all entity-overlapping, 4 distinct agents.
        self._patch_build(monkeypatch, _passing_traces(4))
        captured, writer = _capture_writer()
        result = await run_forge_distill(            run_label="test-run",
            tenant_id="acme", fleet_id="ops",
            window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 5, 15, tzinfo=timezone.utc),
            llm_fn=_llm_returns_golden,
            memory_fetcher=_memory_fetcher_always,
            poison_checker=_poison_never,
            candidate_writer=writer,
        )
        assert result.total_traces == 4
        assert result.labeled_traces == 4
        assert result.clusters_total == 1
        assert result.clusters_eligible == 1
        assert result.candidates_written == 1
        assert len(captured) == 1

        doc = captured[0]
        assert doc["tenant_id"] == "acme"
        assert doc["collection"] == "skills"
        assert doc["doc_id"].startswith("forge/")
        d = doc["data"]
        assert d["status"] == "candidate"          # NEVER auto-promotes in Phase 1
        assert d["source"] == "forge"
        assert d["kind"] == "create"
        assert d["cluster_fingerprint"].startswith(f"fp:{FINGERPRINT_FORMULA_VERSION}:")
        # Cites are deduped + sorted (sorted-set on memory_ids).
        assert d["cites"] == sorted(set(d["cites"]))
        # Provenance.
        assert d["origin"]["agent_id"] == "forge"
        assert d["goal"] and d["evidence"]
        assert d["domain"] == "devops"

    @pytest.mark.asyncio
    async def test_unlabeled_traces_dropped(self, monkeypatch):
        # 4 unknown-outcome traces → nothing to distill.
        traces = [
            _trace(run_id=f"r{i}", agent_id=f"a{i}", outcome="unknown",
                   entity_ids=["e1", "e2"])
            for i in range(4)
        ]
        self._patch_build(monkeypatch, traces)
        captured, writer = _capture_writer()
        result = await run_forge_distill(            run_label="test-run",
            tenant_id="t1", fleet_id=None,
            window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 5, 15, tzinfo=timezone.utc),
            llm_fn=_llm_returns_golden,
            memory_fetcher=_memory_fetcher_always,
            poison_checker=_poison_never,
            candidate_writer=writer,
        )
        assert result.total_traces == 4
        assert result.labeled_traces == 0
        assert result.candidates_written == 0
        assert captured == []

    @pytest.mark.asyncio
    async def test_under_volume_cluster_dropped(self, monkeypatch):
        # 2 traces (below default min_cluster_size=3).
        self._patch_build(monkeypatch, _passing_traces(2))
        captured, writer = _capture_writer()
        result = await run_forge_distill(            run_label="test-run",
            tenant_id="t1", fleet_id=None,
            window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 5, 15, tzinfo=timezone.utc),
            llm_fn=_llm_returns_golden,
            memory_fetcher=_memory_fetcher_always,
            poison_checker=_poison_never,
            candidate_writer=writer,
        )
        assert result.clusters_total == 1
        assert result.clusters_eligible == 0
        assert captured == []

    @pytest.mark.asyncio
    async def test_single_agent_cluster_dropped_by_diversity(self, monkeypatch):
        # 5 traces, all by alice → fails diversity gate.
        traces = [
            _trace(run_id=f"r{i}", agent_id="alice", entity_ids=["e1", "e2"])
            for i in range(5)
        ]
        self._patch_build(monkeypatch, traces)
        captured, writer = _capture_writer()
        result = await run_forge_distill(            run_label="test-run",
            tenant_id="t1", fleet_id=None,
            window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 5, 15, tzinfo=timezone.utc),
            llm_fn=_llm_returns_golden,
            memory_fetcher=_memory_fetcher_always,
            poison_checker=_poison_never,
            candidate_writer=writer,
        )
        assert result.clusters_total == 1
        assert result.clusters_eligible == 0
        assert captured == []

    @pytest.mark.asyncio
    async def test_poison_skip_does_not_write(self, monkeypatch):
        self._patch_build(monkeypatch, _passing_traces(4))
        captured, writer = _capture_writer()
        result = await run_forge_distill(            run_label="test-run",
            tenant_id="t1", fleet_id=None,
            window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 5, 15, tzinfo=timezone.utc),
            llm_fn=_llm_returns_golden,
            memory_fetcher=_memory_fetcher_always,
            poison_checker=_poison_always,        # everything is poisoned
            candidate_writer=writer,
        )
        assert result.candidates_skipped_poisoned == 1
        assert result.candidates_written == 0
        assert captured == []

    @pytest.mark.asyncio
    async def test_distill_error_skip_does_not_abort_run(self, monkeypatch):
        # Two clusters with disjoint entity sets, both eligible.
        traces = (
            [_trace(run_id=f"r{i}", agent_id=f"a{i}", entity_ids=["e1", "e2", "e3"])
             for i in range(4)]
            +
            [_trace(run_id=f"r{i+10}", agent_id=f"b{i}", entity_ids=["x1", "x2", "x3"])
             for i in range(4)]
        )
        self._patch_build(monkeypatch, traces)

        # LLM returns nonsense for the FIRST cluster but a golden
        # response for any subsequent call.
        call_counter = {"n": 0}
        async def flaky_llm(_prompt: str) -> str:
            call_counter["n"] += 1
            if call_counter["n"] == 1:
                return "not json"
            return json.dumps(_golden_llm_response(slug=f"deploy-eu-west-dns-{call_counter['n']}"))

        captured, writer = _capture_writer()
        result = await run_forge_distill(            run_label="test-run",
            tenant_id="t1", fleet_id=None,
            window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 5, 15, tzinfo=timezone.utc),
            llm_fn=flaky_llm,
            memory_fetcher=_memory_fetcher_always,
            poison_checker=_poison_never,
            candidate_writer=writer,
        )
        assert result.clusters_eligible == 2
        assert result.candidates_skipped_distill_error == 1
        assert result.candidates_written == 1

    @pytest.mark.asyncio
    async def test_max_writes_per_run_caps_output(self, monkeypatch):
        # 3 clusters, but config limits writes to 1.
        traces = (
            [_trace(run_id=f"r{i}", agent_id=f"a{i}", entity_ids=["e1", "e2", "e3"])
             for i in range(4)]
            +
            [_trace(run_id=f"r{i+10}", agent_id=f"b{i}", entity_ids=["x1", "x2", "x3"])
             for i in range(4)]
            +
            [_trace(run_id=f"r{i+20}", agent_id=f"c{i}", entity_ids=["y1", "y2", "y3"])
             for i in range(4)]
        )
        self._patch_build(monkeypatch, traces)

        # Distinct slug per call so doc_ids don't collide.
        call_counter = {"n": 0}
        async def llm_unique(_prompt: str) -> str:
            call_counter["n"] += 1
            return json.dumps(_golden_llm_response(slug=f"slug-{call_counter['n']}"))

        captured, writer = _capture_writer()
        cfg = ForgeConfig(max_writes_per_run=1)
        result = await run_forge_distill(            run_label="test-run",
            tenant_id="t1", fleet_id=None,
            window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 5, 15, tzinfo=timezone.utc),
            llm_fn=llm_unique,
            memory_fetcher=_memory_fetcher_always,
            poison_checker=_poison_never,
            candidate_writer=writer,
            config=cfg,
        )
        assert result.clusters_eligible == 3
        assert result.candidates_written == 1
        assert len(captured) == 1


# ── started_at semantics ──────────────────────────────────────────


@pytest.mark.unit
class TestForgeRunResultTimestamps:
    """Regression for the ``started_at`` semantics fix. Previously
    the field used ``default_factory=datetime.now`` which fires at
    dataclass-construction (the END of the run); the result's
    ``started_at`` was indistinguishable from finish time. Fixed by
    stamping the wall clock at the top of ``run_forge_distill``
    BEFORE any await and passing it through explicitly."""

    def _patch_build(self, monkeypatch, traces):
        async def fake_build(*_args, **_kwargs):
            return list(traces)
        import core_api.services.forge.forge_service as svc
        monkeypatch.setattr(svc, "build_session_traces", fake_build)

    @pytest.mark.asyncio
    async def test_started_at_captured_before_work(self, monkeypatch):
        """``started_at`` must be earlier than (or equal to) the time
        immediately AFTER the run returns. The previous
        default-factory bug would still satisfy this trivially —
        what we actually want to assert is that ``started_at`` is
        close to the BEFORE-run timestamp, not the AFTER-run one."""
        self._patch_build(monkeypatch, _passing_traces(4))
        captured, writer = _capture_writer()

        before = datetime.now(timezone.utc)
        result = await run_forge_distill(            run_label="test-run",
            tenant_id="t1", fleet_id="f1",
            window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 5, 15, tzinfo=timezone.utc),
            llm_fn=_llm_returns_golden,
            memory_fetcher=_memory_fetcher_always,
            poison_checker=_poison_never,
            candidate_writer=writer,
        )
        after = datetime.now(timezone.utc)

        # The stamp lies between before and after.
        assert before <= result.started_at <= after

        # Empirically, the run is dominated by I/O — so started_at
        # should be very close to ``before``, not ``after``. We use
        # a generous slack (50 ms) because CI timing isn't precise.
        slack = timedelta(milliseconds=50)
        assert result.started_at - before <= slack, (
            f"started_at ({result.started_at}) is too far from the "
            f"start of the run ({before}) — likely captured at "
            f"dataclass-construction time (the END of the run) "
            f"instead of the beginning."
        )


# ── Distill loop resilience: transient errors don't abort the run ──


@pytest.mark.unit
class TestForgeRunResilience:
    """All-cluster-fails-still-runs-the-others. The Forge tick must
    survive an LLM timeout, a memory_fetcher DB error, a
    poison_checker failure, or a writer crash on one cluster
    without dragging the rest of the tick down with it."""

    def _patch_build(self, monkeypatch, traces):
        async def fake_build(*_args, **_kwargs):
            return list(traces)
        import core_api.services.forge.forge_service as svc
        monkeypatch.setattr(svc, "build_session_traces", fake_build)

    def _two_eligible_clusters(self):
        return (
            [_trace(run_id=f"r{i}", agent_id=f"a{i}", entity_ids=["e1", "e2", "e3"])
             for i in range(4)]
            +
            [_trace(run_id=f"r{i+10}", agent_id=f"b{i}", entity_ids=["x1", "x2", "x3"])
             for i in range(4)]
        )

    @pytest.mark.asyncio
    async def test_llm_timeout_on_one_cluster_skips_it(self, monkeypatch):
        self._patch_build(monkeypatch, self._two_eligible_clusters())

        call = {"n": 0}
        async def llm(_prompt: str) -> str:
            call["n"] += 1
            if call["n"] == 1:
                raise TimeoutError("simulated LLM timeout")
            return json.dumps(_golden_llm_response(slug=f"s-{call['n']}"))

        captured, writer = _capture_writer()
        result = await run_forge_distill(            run_label="test-run",
            tenant_id="t1", fleet_id=None,
            window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 5, 15, tzinfo=timezone.utc),
            llm_fn=llm,
            memory_fetcher=_memory_fetcher_always,
            poison_checker=_poison_never,
            candidate_writer=writer,
        )
        assert result.clusters_eligible == 2
        # LLM TimeoutError is an I/O failure inside llm_fn, not a
        # parse-shaped problem with the response — so it counts in
        # the I/O bucket. (A previous version bucketed all
        # non-parse, non-poison failures under distill_error;
        # post-review the buckets are split.)
        assert result.candidates_skipped_io_error == 1
        assert result.candidates_skipped_distill_error == 0
        assert result.candidates_written == 1

    @pytest.mark.asyncio
    async def test_memory_fetcher_error_skips_cluster(self, monkeypatch):
        self._patch_build(monkeypatch, self._two_eligible_clusters())

        call = {"n": 0}
        async def fetcher(mids):
            call["n"] += 1
            if call["n"] == 1:
                raise ConnectionError("simulated DB connection drop")
            return {m: f"content {m}" for m in mids}

        captured, writer = _capture_writer()
        result = await run_forge_distill(            run_label="test-run",
            tenant_id="t1", fleet_id=None,
            window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 5, 15, tzinfo=timezone.utc),
            llm_fn=_llm_returns_golden,
            memory_fetcher=fetcher,
            poison_checker=_poison_never,
            candidate_writer=writer,
        )
        # ConnectionError is an I/O failure → io_error bucket.
        assert result.candidates_skipped_io_error == 1
        assert result.candidates_skipped_distill_error == 0
        assert result.candidates_written == 1

    @pytest.mark.asyncio
    async def test_poison_checker_error_skips_cluster(self, monkeypatch):
        self._patch_build(monkeypatch, self._two_eligible_clusters())

        call = {"n": 0}
        async def poisoner(_fp):
            call["n"] += 1
            if call["n"] == 1:
                raise RuntimeError("simulated poison-table query failure")
            return False

        captured, writer = _capture_writer()
        result = await run_forge_distill(            run_label="test-run",
            tenant_id="t1", fleet_id=None,
            window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 5, 15, tzinfo=timezone.utc),
            llm_fn=_llm_returns_golden,
            memory_fetcher=_memory_fetcher_always,
            poison_checker=poisoner,
            candidate_writer=writer,
        )
        # poison-table query failure → io_error bucket.
        assert result.candidates_skipped_io_error == 1
        assert result.candidates_skipped_distill_error == 0
        assert result.candidates_written == 1

    @pytest.mark.asyncio
    async def test_llm_returning_kind_update_is_rejected_as_distill_error(self, monkeypatch):
        """Phase-1 invariant: Forge only mints kind='create'. If the
        model returns kind='update' (with hash-binding to a live
        target), the v2-diff card flow doesn't exist yet — we'd
        smuggle a malformed doc past Sentinel into the inbox.
        Treat as a distill error so the cluster is skipped and
        counted, not silently written."""
        self._patch_build(monkeypatch, self._two_eligible_clusters())

        call = {"n": 0}
        async def llm(_prompt: str) -> str:
            call["n"] += 1
            if call["n"] == 1:
                # First cluster: forbidden kind='update' from the model.
                return json.dumps(_golden_llm_response(kind="update", slug=f"u-{call['n']}"))
            # Second cluster: well-formed.
            return json.dumps(_golden_llm_response(slug=f"c-{call['n']}"))

        captured, writer = _capture_writer()
        result = await run_forge_distill(            run_label="test-run",
            tenant_id="t1", fleet_id=None,
            window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 5, 15, tzinfo=timezone.utc),
            llm_fn=llm,
            memory_fetcher=_memory_fetcher_always,
            poison_checker=_poison_never,
            candidate_writer=writer,
        )
        assert result.candidates_skipped_distill_error == 1
        assert result.candidates_written == 1

    @pytest.mark.asyncio
    async def test_candidate_writer_error_skips_persist(self, monkeypatch):
        # Two eligible clusters; the first WRITE fails (e.g. UNIQUE
        # violation, schema validator hiccup). The second cluster
        # must still get persisted.
        self._patch_build(monkeypatch, self._two_eligible_clusters())

        # Unique slug per LLM call so we can tell them apart in the
        # writer.
        call_counter = {"n": 0}
        async def llm_unique(_prompt):
            call_counter["n"] += 1
            return json.dumps(_golden_llm_response(slug=f"slug-{call_counter['n']}"))

        write_attempts: list[str] = []
        async def writer(doc):
            slug = doc["data"]["slug"]
            write_attempts.append(slug)
            if slug == "slug-1":
                raise RuntimeError("simulated UNIQUE violation")

        result = await run_forge_distill(            run_label="test-run",
            tenant_id="t1", fleet_id=None,
            window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 5, 15, tzinfo=timezone.utc),
            llm_fn=llm_unique,
            memory_fetcher=_memory_fetcher_always,
            poison_checker=_poison_never,
            candidate_writer=writer,
        )
        assert len(write_attempts) == 2
        # UNIQUE violation from candidate_writer → io_error bucket.
        assert result.candidates_skipped_io_error == 1
        assert result.candidates_skipped_distill_error == 0
        assert result.candidates_written == 1


# ── run_label thread-through (audit handle) ───────────────────────


@pytest.mark.unit
class TestForgeRunLabel:
    """Every Forge tick carries an explicit ``run_label`` that
    propagates onto every candidate's ``origin.run_id`` so an
    operator inspecting the Inbox can trace a card back to the
    invocation that minted it."""

    def _patch_build(self, monkeypatch, traces):
        async def fake_build(*_args, **_kwargs):
            return list(traces)
        import core_api.services.forge.forge_service as svc
        monkeypatch.setattr(svc, "build_session_traces", fake_build)

    @pytest.mark.asyncio
    async def test_run_label_lands_on_result(self, monkeypatch):
        self._patch_build(monkeypatch, _passing_traces(4))
        captured, writer = _capture_writer()
        result = await run_forge_distill(            run_label="forge-cron-20260607T1845",
            tenant_id="t1", fleet_id=None,
            window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 5, 15, tzinfo=timezone.utc),
            llm_fn=_llm_returns_golden,
            memory_fetcher=_memory_fetcher_always,
            poison_checker=_poison_never,
            candidate_writer=writer,
        )
        assert result.run_label == "forge-cron-20260607T1845"

    @pytest.mark.asyncio
    async def test_run_label_stamped_on_every_candidate(self, monkeypatch):
        self._patch_build(monkeypatch, _passing_traces(4))
        captured, writer = _capture_writer()
        await run_forge_distill(            run_label="forge-cron-acme-20260607T1845",
            tenant_id="acme", fleet_id="ops",
            window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 5, 15, tzinfo=timezone.utc),
            llm_fn=_llm_returns_golden,
            memory_fetcher=_memory_fetcher_always,
            poison_checker=_poison_never,
            candidate_writer=writer,
        )
        assert len(captured) == 1
        # Every candidate carries the label on origin.run_id — NOT None.
        assert captured[0]["data"]["origin"]["run_id"] == "forge-cron-acme-20260607T1845"


# ── No-overwrite guard (status_checker) ────────────────────────────


@pytest.mark.unit
class TestForgeStatusCheckerGuard:
    """``status_checker`` is the no-overwrite guard. When the target
    slug already exists with a status that ISN'T ``candidate``,
    Forge skips the write (operator-curated state must not be
    silently clobbered by a re-mining run).

    Re-mining over an existing ``candidate`` (or no doc at all) IS
    allowed — that's the legitimate refine-the-candidate path."""

    def _patch_build(self, monkeypatch, traces):
        async def fake_build(*_args, **_kwargs):
            return list(traces)
        import core_api.services.forge.forge_service as svc
        monkeypatch.setattr(svc, "build_session_traces", fake_build)

    def _kw(self):
        return dict(
            run_label="test-run",
            tenant_id="t1", fleet_id=None,
            window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 5, 15, tzinfo=timezone.utc),
            llm_fn=_llm_returns_golden,
            memory_fetcher=_memory_fetcher_always,
            poison_checker=_poison_never,
        )

    @pytest.mark.asyncio
    async def test_active_target_skipped(self, monkeypatch):
        self._patch_build(monkeypatch, _passing_traces(4))
        captured, writer = _capture_writer()
        # status_checker reports the target is already 'active' —
        # operator-approved. Don't clobber.
        async def existing_active(_t, _c, _d): return "active"
        result = await run_forge_distill(            candidate_writer=writer,
            status_checker=existing_active,
            **self._kw(),
        )
        assert result.candidates_skipped_existing == 1
        assert result.candidates_written == 0
        assert captured == []

    @pytest.mark.parametrize("status", ["rejected", "quarantined", "stale", "deprecated"])
    @pytest.mark.asyncio
    async def test_terminal_states_skipped(self, monkeypatch, status):
        # Any non-candidate status counts: rejected (poison-flagged
        # by operator), quarantined (Sentinel), stale (drift),
        # deprecated. None of these should be re-overwritten by Forge.
        self._patch_build(monkeypatch, _passing_traces(4))
        captured, writer = _capture_writer()
        async def existing(*_): return status
        result = await run_forge_distill(            candidate_writer=writer,
            status_checker=existing,
            **self._kw(),
        )
        assert result.candidates_skipped_existing == 1
        assert captured == []

    @pytest.mark.asyncio
    async def test_existing_candidate_is_re_minable(self, monkeypatch):
        # Re-mining over an existing ``candidate`` IS the legitimate
        # refine path — Forge should write.
        self._patch_build(monkeypatch, _passing_traces(4))
        captured, writer = _capture_writer()
        async def existing_candidate(*_): return "candidate"
        result = await run_forge_distill(            candidate_writer=writer,
            status_checker=existing_candidate,
            **self._kw(),
        )
        assert result.candidates_skipped_existing == 0
        assert result.candidates_written == 1
        assert len(captured) == 1

    @pytest.mark.asyncio
    async def test_no_existing_doc_writes_normally(self, monkeypatch):
        # status_checker returns None → no existing doc → write.
        self._patch_build(monkeypatch, _passing_traces(4))
        captured, writer = _capture_writer()
        async def no_existing(*_): return None
        result = await run_forge_distill(            candidate_writer=writer,
            status_checker=no_existing,
            **self._kw(),
        )
        assert result.candidates_written == 1
        assert result.candidates_skipped_existing == 0

    @pytest.mark.asyncio
    async def test_status_checker_optional_disables_guard(self, monkeypatch):
        # status_checker=None (default) means no guard — every
        # candidate gets written. Eval harness path.
        self._patch_build(monkeypatch, _passing_traces(4))
        captured, writer = _capture_writer()
        result = await run_forge_distill(            candidate_writer=writer,
            # status_checker omitted entirely
            **self._kw(),
        )
        assert result.candidates_written == 1
        assert result.candidates_skipped_existing == 0

    @pytest.mark.asyncio
    async def test_candidate_doc_carries_content_hash(self, monkeypatch):
        """The Forge writer bypasses the SF-002 validator (Forge runs
        through the storage client directly, not the HTTP route), so
        ``_distill_cluster`` must stamp ``content_hash`` itself.
        Downstream consumers (kind=update hash-binding, lifecycle
        audits, Phase-4 v2-diff cards) rely on the field being
        present and matching ``sha256(content.encode('utf-8'))``."""
        import hashlib
        self._patch_build(monkeypatch, _passing_traces(4))
        captured, writer = _capture_writer()
        await run_forge_distill(            candidate_writer=writer,
            **self._kw(),
        )
        assert len(captured) == 1
        doc = captured[0]["data"]
        assert "content_hash" in doc
        assert doc["content_hash"].startswith("sha256:")
        # Hash matches the content byte-for-byte.
        expected = "sha256:" + hashlib.sha256(doc["content"].encode("utf-8")).hexdigest()
        assert doc["content_hash"] == expected

    @pytest.mark.asyncio
    async def test_candidate_doc_carries_scan_result(self, monkeypatch):
        """Sentinel pre-scan result must be on every Forge candidate —
        the Inbox card renders ``data.scan.findings`` in Phase 2 and
        a missing field would crash the render. Phase-0 stub returns
        ``state='clean'`` so candidates pass through without flag."""
        self._patch_build(monkeypatch, _passing_traces(4))
        captured, writer = _capture_writer()
        await run_forge_distill(            candidate_writer=writer,
            **self._kw(),
        )
        doc = captured[0]["data"]
        assert "scan" in doc
        # Phase-0 stub returns clean / 0 / 0 / 0 / [].
        assert doc["scan"]["state"] == "clean"
        assert doc["scan"]["critical"] == 0
        assert doc["scan"]["warn"] == 0
        assert doc["scan"]["info"] == 0
        assert doc["scan"]["findings"] == []

    @pytest.mark.asyncio
    async def test_sentinel_fatal_finding_increments_sentinel_counter(self, monkeypatch):
        """A fatal Sentinel finding (path violation / hard size cap)
        must abort the write — but increment the dedicated
        ``candidates_skipped_sentinel`` counter, NOT
        ``candidates_skipped_poisoned``. The two failure modes look
        identical from "did the write happen?" angle but represent
        different operator concerns: poisoned = rejected-fingerprint
        cooloff memory working as designed; sentinel = the corpus
        is producing content that fails the content-shape scanner.
        Conflating them in the run summary blinds operators to
        whichever signal is climbing."""
        from core_api.services.forge import sentinel_scan
        from core_api.services.forge.sentinel_scan import (
            ScanFinding,
            ScanResult,
            _now_iso,
        )

        async def fatal_scan(_data, *, mode, **_kw):
            return ScanResult(
                state="failed",
                scanned_at=_now_iso(),
                critical=1,
                warn=0,
                info=0,
                findings=(
                    ScanFinding(
                        code="path_violation",
                        severity="critical",
                        message="absolute path in support_files",
                        fatal=True,
                    ),
                ),
            )

        self._patch_build(monkeypatch, _passing_traces(4))
        captured, writer = _capture_writer()
        monkeypatch.setattr(sentinel_scan, "scan_skill_doc", fatal_scan)
        # forge_service imported the symbol — also monkeypatch the
        # module's local binding.
        import core_api.services.forge.forge_service as svc
        monkeypatch.setattr(svc, "scan_skill_doc", fatal_scan)
        result = await run_forge_distill(            candidate_writer=writer,
            **self._kw(),
        )
        # Sentinel-fatal lands in its OWN bucket — poisoned-fingerprint
        # bucket is untouched.
        assert result.candidates_skipped_sentinel == 1
        assert result.candidates_skipped_poisoned == 0
        assert result.candidates_written == 0
        assert captured == []

    @pytest.mark.asyncio
    async def test_candidate_doc_ids_contains_namespaced_doc_id(self, monkeypatch):
        """``ForgeRunResult.candidate_doc_ids`` must carry the full
        ``forge/<slug>`` doc_id, not the bare slug. The slug alone
        wouldn't match the (tenant, collection, doc_id) primary key
        in ``documents`` for follow-up lookups (eval harness, Phase 2
        inbox query, etc.)."""
        self._patch_build(monkeypatch, _passing_traces(4))
        captured, writer = _capture_writer()
        result = await run_forge_distill(            candidate_writer=writer,
            **self._kw(),
        )
        assert result.candidates_written == 1
        assert len(result.candidate_doc_ids) == 1
        # Carries the namespace prefix.
        assert result.candidate_doc_ids[0].startswith("forge/")
        # AND matches the doc that was actually written.
        assert result.candidate_doc_ids[0] == captured[0]["doc_id"]
        # Defensive: NOT just the bare slug.
        assert result.candidate_doc_ids[0] != captured[0]["data"]["slug"]

    @pytest.mark.asyncio
    async def test_status_checker_exception_skips_cluster(self, monkeypatch):
        # If the checker itself crashes (e.g. transient DB error
        # talking to the storage layer), DON'T treat that as
        # permission to write — skip the cluster + increment the
        # error counter. Belt-and-braces: 'unknown existing state'
        # is a safer default than 'assume nothing exists'.
        self._patch_build(monkeypatch, _passing_traces(4))
        captured, writer = _capture_writer()
        async def boom(*_): raise RuntimeError("simulated storage hiccup")
        result = await run_forge_distill(            candidate_writer=writer,
            status_checker=boom,
            **self._kw(),
        )
        assert result.candidates_written == 0
        # status_checker exception is a storage-layer failure →
        # io_error bucket, NOT distill_error.
        assert result.candidates_skipped_io_error == 1
        assert result.candidates_skipped_distill_error == 0


# ── Determinism: same lake state → same fingerprint ───────────────


@pytest.mark.unit
class TestForgeDeterminism:
    """The whole Phase-1 stability story depends on the Forge run
    being reproducible against an unchanged lake. This test pins
    that the candidate's cluster_fingerprint is identical across
    two back-to-back runs over the same inputs."""

    def _patch_build(self, monkeypatch, traces):
        async def fake_build(*_args, **_kwargs):
            return list(traces)
        import core_api.services.forge.forge_service as svc
        monkeypatch.setattr(svc, "build_session_traces", fake_build)

    @pytest.mark.asyncio
    async def test_two_runs_same_fingerprint(self, monkeypatch):
        traces = _passing_traces(4)
        self._patch_build(monkeypatch, traces)
        cap1, w1 = _capture_writer()
        cap2, w2 = _capture_writer()
        kw = dict(
            run_label="test-determinism-run",
            tenant_id="t1", fleet_id="f1",
            window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 5, 15, tzinfo=timezone.utc),
            llm_fn=_llm_returns_golden,
            memory_fetcher=_memory_fetcher_always,
            poison_checker=_poison_never,
        )
        await run_forge_distill(candidate_writer=w1, **kw)
        await run_forge_distill(candidate_writer=w2, **kw)
        assert len(cap1) == 1 and len(cap2) == 1
        fp1 = cap1[0]["data"]["cluster_fingerprint"]
        fp2 = cap2[0]["data"]["cluster_fingerprint"]
        assert fp1 == fp2  # cluster identity is stable across runs
