"""Unit tests for the EmitMemoryTriple pipeline step (CAURA-123).

No DB required. Verifies the deterministic triple-emission contract:
- Disabled flag → SKIPPED, fields untouched
- Caller-supplied triples → SKIPPED, fields untouched
- Subject must be exactly one entity_link with role="subject"
- Predicate must come from SINGLE_VALUE_PREDICATES
- Ambiguous predicate → SKIPPED (never guess)
- Happy path populates all three fields
- Unexpected errors degrade to SKIPPED (never raise)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from common.constants import SINGLE_VALUE_PREDICATES
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome
from core_api.pipeline.steps.write.emit_memory_triple import EmitMemoryTriple
from core_api.schemas import EntityLinkIn, MemoryCreate

TENANT_ID = "test-tenant-triple"
FLEET_ID = "test-fleet"
AGENT_ID = "test-agent"


def _input(content: str, subject_id=None, extra_links=None, **kwargs) -> MemoryCreate:
    links = []
    if subject_id is not None:
        links.append(EntityLinkIn(entity_id=subject_id, role="subject"))
    if extra_links:
        links.extend(extra_links)
    return MemoryCreate(
        tenant_id=TENANT_ID,
        fleet_id=FLEET_ID,
        agent_id=AGENT_ID,
        content=content,
        entity_links=links,
        **kwargs,
    )


def _ctx(data: MemoryCreate, *, flag: bool = True) -> PipelineContext:
    return PipelineContext(
                data={"input": data, "memory_fields": {"metadata": {}}},
        tenant_config=SimpleNamespace(triple_emission_enabled=flag),
    )


@pytest.mark.unit
class TestEmitMemoryTriple:
    async def test_flag_off_skips_and_leaves_fields_untouched(self):
        sid = uuid4()
        data = _input("Ran lives in NYC", subject_id=sid)
        ctx = _ctx(data, flag=False)

        result = await EmitMemoryTriple().execute(ctx)

        assert result is not None and result.outcome == StepOutcome.SKIPPED
        assert result.detail["reason"] == "flag_off"
        assert data.subject_entity_id is None
        assert data.predicate is None
        assert data.object_value is None

    async def test_already_set_is_skipped_and_not_overwritten(self):
        sid = uuid4()
        preset_subject = uuid4()
        data = _input(
            "Ran lives in NYC",
            subject_id=sid,
            subject_entity_id=preset_subject,
            predicate="lives_in",
            object_value="tel aviv",
        )
        ctx = _ctx(data)

        result = await EmitMemoryTriple().execute(ctx)

        assert result is not None and result.outcome == StepOutcome.SKIPPED
        assert result.detail["reason"] == "already_set"
        assert data.subject_entity_id == preset_subject
        assert data.predicate == "lives_in"
        assert data.object_value == "tel aviv"

    async def test_partial_supply_is_skipped_not_overwritten(self):
        # Any partial caller-supply (just subject, just predicate, just
        # object) must short-circuit. Otherwise the step would derive a
        # different subject from entity_links and silently overwrite
        # the caller's choice.
        sid = uuid4()
        link_subject = uuid4()
        for kwargs, untouched in (
            ({"subject_entity_id": link_subject}, "subject_entity_id"),
            ({"predicate": "lives_in"}, "predicate"),
            ({"object_value": "tel aviv"}, "object_value"),
        ):
            data = _input("Ran lives in NYC", subject_id=sid, **kwargs)
            preset = getattr(data, untouched)
            result = await EmitMemoryTriple().execute(_ctx(data))
            assert result.outcome == StepOutcome.SKIPPED
            assert result.detail["reason"] == "already_set"
            # The supplied field stays exactly what the caller passed.
            assert getattr(data, untouched) == preset
            # The other two fields must NOT have been written by us.
            other_fields = {"subject_entity_id", "predicate", "object_value"} - {
                untouched
            }
            for f in other_fields:
                assert getattr(data, f) is None, f"step wrote {f} on partial-supply"

    async def test_happy_path_lives_in(self):
        sid = uuid4()
        data = _input("Ran lives in New York", subject_id=sid)
        ctx = _ctx(data)

        result = await EmitMemoryTriple().execute(ctx)

        assert result is None  # implicit success
        assert data.subject_entity_id == sid
        assert data.predicate == "lives_in"
        assert data.object_value == "new york"
        assert ctx.data["memory_fields"]["metadata"]["triple_emission_ms"] >= 0

    async def test_happy_path_reports_to(self):
        sid = uuid4()
        data = _input("Alice reports to Bob.", subject_id=sid)
        await EmitMemoryTriple().execute(_ctx(data))
        assert data.predicate == "reports_to"
        assert data.object_value == "bob"

    async def test_no_subject_link_skips(self):
        data = _input("lives in NYC")  # no subject link
        ctx = _ctx(data)
        result = await EmitMemoryTriple().execute(ctx)
        assert result.outcome == StepOutcome.SKIPPED
        assert result.detail["reason"] == "no_subject"

    async def test_multiple_subject_links_skip(self):
        sid = uuid4()
        extra = EntityLinkIn(entity_id=uuid4(), role="subject")
        data = _input("Ran lives in NYC", subject_id=sid, extra_links=[extra])
        ctx = _ctx(data)
        result = await EmitMemoryTriple().execute(ctx)
        assert result.outcome == StepOutcome.SKIPPED
        assert result.detail["reason"] == "ambiguous_subject"
        assert data.subject_entity_id is None

    async def test_no_predicate_match_skips(self):
        sid = uuid4()
        data = _input("Ran likes pizza on weekends", subject_id=sid)
        ctx = _ctx(data)
        result = await EmitMemoryTriple().execute(ctx)
        assert result.outcome == StepOutcome.SKIPPED
        assert result.detail["reason"] == "no_predicate_match"

    async def test_ambiguous_predicate_skips(self):
        # Two phrases that match different predicates in the same content.
        sid = uuid4()
        data = _input(
            "Acme is headquartered in Paris and is based in Lyon",
            subject_id=sid,
        )
        ctx = _ctx(data)
        result = await EmitMemoryTriple().execute(ctx)
        assert result.outcome == StepOutcome.SKIPPED
        assert result.detail["reason"] == "ambiguous_predicate"

    async def test_object_unparseable_skips(self):
        # Matched phrase but nothing after it.
        sid = uuid4()
        data = _input("Ran lives in", subject_id=sid)
        ctx = _ctx(data)
        result = await EmitMemoryTriple().execute(ctx)
        assert result.outcome == StepOutcome.SKIPPED
        assert result.detail["reason"] == "object_unparseable"

    async def test_object_bounded_to_current_sentence(self):
        # Trailing clauses must not bleed into object_value.
        sid = uuid4()
        data = _input(
            "Ran lives in New York. He also enjoys long walks.", subject_id=sid
        )
        await EmitMemoryTriple().execute(_ctx(data))
        assert data.object_value == "new york"

    async def test_abbreviation_period_not_treated_as_sentence_end(self):
        sid = uuid4()
        data = _input("Alice reports to Dr. Smith.", subject_id=sid)
        await EmitMemoryTriple().execute(_ctx(data))
        assert data.object_value == "dr. smith"

    async def test_trailing_punctuation_stripped(self):
        sid = uuid4()
        data = _input("Ran lives in New York!", subject_id=sid)
        await EmitMemoryTriple().execute(_ctx(data))
        assert data.object_value == "new york"

    async def test_case_insensitive_and_article_strip(self):
        sid = uuid4()
        data = _input("Acme IS BASED IN the United Kingdom.", subject_id=sid)
        await EmitMemoryTriple().execute(_ctx(data))
        assert data.predicate == "based_in"
        assert data.object_value == "united kingdom"

    async def test_emitted_predicate_is_in_allowlist(self):
        # Every populate path must produce a predicate the detector accepts.
        # Covers original CAURA-123 cluster + the four CAURA-126 tiers.
        sid = uuid4()
        for content, expected in [
            # -- Original CAURA-123 cluster --
            ("X lives in Y", "lives_in"),
            ("X is located in Y", "located_in"),
            ("X is based in Y", "based_in"),
            ("X is headquartered in Y", "headquartered_in"),
            ("X reports to Y", "reports_to"),
            ("X is managed by Y", "managed_by"),
            ("X is owned by Y", "owned_by"),
            ("X is assigned to Y", "assigned_to"),
            ("X is employed by Y", "employed_by"),
            ("X is the CEO of Y", "ceo_of"),
            ("X is the CTO of Y", "cto_of"),
            ("X is the CFO of Y", "cfo_of"),
            ("X is renamed to Y", "renamed_to"),
            # -- Tier 1: dates --
            ("X has release date 2027-05-01", "release_date"),
            ("X release date is 2027-05-01", "release_date"),
            ("X has launch date 2027-05-01", "launch_date"),
            ("X launch date is 2027-05-01", "launch_date"),
            ("X has go-live date 2027-05-01", "go_live_date"),
            ("X has target date 2027-05-01", "target_date"),
            ("X has due date 2027-05-01", "due_date"),
            ("X is due on 2027-05-01", "due_date"),
            ("X is due by 2027-05-01", "due_date"),
            ("X has start date 2027-05-01", "start_date"),
            ("X has end date 2027-05-01", "end_date"),
            ("X expires on 2027-05-01", "expiry_date"),
            ("X has deadline 2027-05-01", "deadline"),
            ("X ETA is 2027-05-01", "eta"),
            ("X has ETA of 2027-05-01", "eta"),
            ("X is scheduled for 2027-05-01", "scheduled_for"),
            ("X is rescheduled to 2027-05-01", "rescheduled_to"),
            ("X was born on 1990-01-01", "birthdate"),
            # -- Tier 2: status / state / role --
            ("X status is in_progress", "status"),
            ("X current status is in_progress", "status"),
            ("X phase is beta", "phase"),
            ("X current phase is beta", "phase"),
            ("X state is open", "state"),
            ("X mode is debug", "mode"),
            ("X priority is high", "priority"),
            ("X has priority high", "priority"),
            ("X severity is critical", "severity"),
            ("X has severity critical", "severity"),
            ("X role is engineer", "role"),
            ("X has role engineer", "role"),
            ("X title is VP", "title"),
            ("X job title is VP", "title"),
            ("X sprint is Sprint-7", "sprint"),
            ("X is in sprint Sprint-7", "sprint"),
            ("X milestone is GA", "milestone"),
            ("X epic is checkout-redesign", "epic"),
            ("X is in epic checkout-redesign", "epic"),
            # -- Tier 3: money / metrics / versioning --
            ("X is priced at 100", "price"),
            ("X price is 100", "price"),
            ("X has price of 100", "price"),
            ("X cost is 50", "cost"),
            ("X salary is 100000", "salary"),
            ("X has budget of 1M", "budget"),
            ("X budget is 1M", "budget"),
            ("X revenue is 5M", "revenue"),
            ("X annual revenue is 5M", "revenue"),
            ("X has revenue of 5M", "revenue"),
            ("X is valued at 10M", "valuation"),
            ("X valuation is 10M", "valuation"),
            ("X funding is 2M", "funding"),
            ("X total funding is 2M", "funding"),
            ("X score is 95", "score"),
            ("X has score of 95", "score"),
            ("X rating is A", "rating"),
            ("X has rating of A", "rating"),
            ("X rank is 3", "rank"),
            ("X is ranked 3", "rank"),
            ("X confidence is 0.9", "confidence"),
            ("X confidence score is 0.9", "confidence_score"),
            ("X potential score is 7", "potential_score"),
            ("X risk score is 0.4", "risk_score"),
            ("X quality score is 95", "quality_score"),
            ("X health score is 8", "health_score"),
            ("X sentiment score is 0.7", "sentiment_score"),
            ("X f1 score is 0.92", "f1_score"),
            ("X version is 2.4.0", "current_version"),
            ("X current version is 2.4.0", "current_version"),
            ("X is on version 2.4.0", "current_version"),
            # -- Tier 4: infra / contact / hierarchy / license --
            ("X hostname is host-01", "hostname"),
            ("X has hostname host-01", "hostname"),
            ("X cluster is prod-us", "cluster"),
            ("X is in cluster prod-us", "cluster"),
            ("X namespace is default", "namespace"),
            ("X is in namespace default", "namespace"),
            ("X zone is us-east-1a", "zone"),
            ("X is in availability zone us-east-1a", "zone"),
            ("X region is us-east-1", "region"),
            ("X country is Germany", "country"),
            ("X city is Berlin", "city"),
            ("X email is a@b.com", "email"),
            ("X email address is a@b.com", "email"),
            ("X has email a@b.com", "email"),
            ("X phone is 555-0100", "phone"),
            ("X phone number is 555-0100", "phone"),
            ("X website is example.com", "website"),
            ("X is led by Alice", "led_by"),
            ("X is headed by Alice", "headed_by"),
            ("X is maintained by Alice", "maintained_by"),
            ("X is supervised by Alice", "supervised_by"),
            ("X is licensed under MIT", "license"),
            ("X license is MIT", "license"),
            ("X is on the plan Pro", "subscription_plan"),
            ("X subscription plan is Pro", "subscription_plan"),
            ("X tier is gold", "tier"),
        ]:
            data = _input(content, subject_id=sid)
            await EmitMemoryTriple().execute(_ctx(data))
            assert data.predicate is not None, f"Failed to emit for: {content!r}"
            assert data.predicate == expected, (
                f"Wrong predicate for {content!r}: got {data.predicate!r}, "
                f"expected {expected!r}"
            )
            assert data.predicate in SINGLE_VALUE_PREDICATES, (
                f"Emitted predicate {data.predicate!r} not in SINGLE_VALUE_PREDICATES"
            )

    async def test_intra_predicate_double_match_not_ambiguous(self):
        # CAURA-126 follow-up: when two patterns in the table match
        # but they map to the SAME canonical predicate (e.g. the
        # ``\bhas\s+release\s+date\b`` and ``\brelease\s+date\s+is\b``
        # rows both fire on "has release date is 2027"), the step must
        # NOT treat this as ambiguous and must emit. Ambiguity is on
        # the predicate, not the number of phrase hits. The match
        # whose ``end()`` is furthest right wins, so ``object_value``
        # excludes interstitial words like "is".
        sid = uuid4()
        data = _input("Atlas has release date is 2027-05-01", subject_id=sid)
        result = await EmitMemoryTriple().execute(_ctx(data))
        assert result is None, (
            f"Same-predicate double-match must emit (not skip); got {result}"
        )
        assert data.predicate == "release_date"
        assert data.object_value == "2027-05-01", (
            f"object_value must use the furthest-right match's tail "
            f"to skip 'is'; got {data.object_value!r}"
        )

    async def test_whitespace_normalised_before_lookbehind_check(self):
        # CAURA-126 follow-up: the score lookbehinds are fixed-width
        # one-character (``(?<!confidence\s)``). Content with a tab
        # between "confidence" and "score" would bypass the lookbehind
        # and route to the bare ``score`` predicate instead of
        # ``confidence_score``. Normalising whitespace at the top of
        # ``execute()`` keeps the lookbehinds load-bearing.
        sid = uuid4()
        data = _input("Atlas confidence\tscore is 0.9", subject_id=sid)
        await EmitMemoryTriple().execute(_ctx(data))
        assert data.predicate == "confidence_score", (
            f"Tab whitespace must still route to confidence_score; "
            f"got predicate={data.predicate!r}"
        )

    async def test_unexpected_error_degrades_to_skip(self):
        # A malformed input object that breaks attribute access mid-step
        # must not bubble up and break the write pipeline.
        sid = uuid4()
        data = _input("Ran lives in NYC", subject_id=sid)
        ctx = _ctx(data)

        # Force an error by replacing entity_links with a non-iterable object
        # AFTER the flag/already-set checks pass.
        class _Bomb:
            def __iter__(self):
                raise RuntimeError("boom")

        data.entity_links = _Bomb()  # type: ignore[assignment]
        result = await EmitMemoryTriple().execute(ctx)
        assert result.outcome == StepOutcome.SKIPPED
        assert result.detail["reason"] == "error"


@pytest.mark.unit
class TestSubjectInference:
    """CAURA-127 — identifier-token heuristic + upsert for the bare-POST
    shape. When no ``role="subject"`` entity_link is supplied, the
    step looks at ``content[:match.start()]`` and emits a subject
    from a deterministic identifier-token regex. Proper-noun shapes
    deliberately skip — they're left to the background entity-
    extraction worker. Each test below mocks ``upsert_entity`` (it
    would otherwise touch the real storage client)."""

    @staticmethod
    def _patch_upsert(monkeypatch, returned_id):
        """Replace ``upsert_entity`` with an AsyncMock returning an
        object whose ``.id`` attribute is ``returned_id``."""
        from unittest.mock import AsyncMock as _AM

        fake = _AM(return_value=SimpleNamespace(id=returned_id))
        monkeypatch.setattr(
            "core_api.pipeline.steps.write.emit_memory_triple.upsert_entity",
            fake,
        )
        return fake

    async def test_identifier_token_subject_upserts_and_emits(self, monkeypatch):
        """TOKEN-shaped subject without entity_links → heuristic infers
        ``TOKEN-XYZ``, calls upsert, populates triple."""
        upserted_id = uuid4()
        fake = self._patch_upsert(monkeypatch, upserted_id)
        data = _input("TOKEN-736C57D0 has release date 2027-05-01")
        result = await EmitMemoryTriple().execute(_ctx(data))
        assert result is None, f"Expected emit; got {result}"
        assert data.subject_entity_id == upserted_id
        assert data.predicate == "release_date"
        assert data.object_value == "2027-05-01"
        # The upsert call carries the inferred canonical_name and the
        # canonical ``identifier`` entity_type. Under the CAURA-127
        # signature cleanup, ``data`` is positional[0] (was [1] when
        # the legacy ``db`` parameter still came first).
        fake.assert_called_once()
        entity_upsert = fake.call_args.args[0]
        assert entity_upsert.entity_type == "identifier"
        assert entity_upsert.canonical_name == "TOKEN-736C57D0"

    async def test_uuid_subject_infers(self, monkeypatch):
        """Canonical UUID subjects are identifier-shaped → infer + emit."""
        self._patch_upsert(monkeypatch, uuid4())
        data = _input("12345678-1234-1234-1234-123456789abc status is open")
        await EmitMemoryTriple().execute(_ctx(data))
        assert data.predicate == "status"
        assert data.object_value == "open"

    async def test_dotted_identifier_subject_infers(self, monkeypatch):
        """Dotted service names: ``api.user.create`` → identifier."""
        self._patch_upsert(monkeypatch, uuid4())
        data = _input("api.user.create status is deprecated")
        await EmitMemoryTriple().execute(_ctx(data))
        assert data.predicate == "status"
        assert data.subject_entity_id is not None

    async def test_build_ref_subject_infers(self, monkeypatch):
        """Build references like ``build #4521`` and ``build-734``."""
        self._patch_upsert(monkeypatch, uuid4())
        data = _input("build #4521 status is failed")
        await EmitMemoryTriple().execute(_ctx(data))
        assert data.predicate == "status"
        assert data.subject_entity_id is not None

    async def test_proper_noun_subject_skips(self, monkeypatch):
        """``Alice`` is a proper noun, not an identifier — skip and
        leave it to background entity extraction. Mirrors the
        skip-on-doubt contract for the object side."""
        fake = self._patch_upsert(monkeypatch, uuid4())
        data = _input("Alice has release date 2027-05-01")
        result = await EmitMemoryTriple().execute(_ctx(data))
        assert result.outcome == StepOutcome.SKIPPED
        assert result.detail["reason"] == "no_subject"
        fake.assert_not_called()

    async def test_stopword_subject_skips(self, monkeypatch):
        """``She`` / ``They`` / ``Today`` / ``Q4`` all skip."""
        fake = self._patch_upsert(monkeypatch, uuid4())
        for content in [
            "She has release date 2027",
            "They deadline is Friday",
            "Today status is open",
            "Q4 has release date 2027",
        ]:
            data = _input(content)
            result = await EmitMemoryTriple().execute(_ctx(data))
            assert result.outcome == StepOutcome.SKIPPED, content
            assert result.detail["reason"] == "no_subject", content
        fake.assert_not_called()

    async def test_empty_head_skips(self, monkeypatch):
        """Content with no text before the predicate phrase → skip."""
        fake = self._patch_upsert(monkeypatch, uuid4())
        data = _input("has release date 2027-05-01")
        result = await EmitMemoryTriple().execute(_ctx(data))
        assert result.outcome == StepOutcome.SKIPPED
        assert result.detail["reason"] == "no_subject"
        fake.assert_not_called()

    async def test_sentence_boundary_trims_subject_head(self, monkeypatch):
        """Subject inference must take the identifier from the CURRENT
        clause, not from a previous sentence. ``"Foo. TOKEN-X has …"``
        should yield ``TOKEN-X``, not ``Foo. TOKEN-X``."""
        self._patch_upsert(monkeypatch, uuid4())
        data = _input("Foo. TOKEN-X9 has release date 2027-05-01")
        await EmitMemoryTriple().execute(_ctx(data))
        assert data.predicate == "release_date"
        assert data.subject_entity_id is not None

    async def test_all_dash_suffix_identifier_not_captured(self, monkeypatch):
        """``TOKEN---`` (post-dash group consists ONLY of dashes) used
        to match because ``[A-Z0-9-]+`` permitted any combination of
        alphanumerics and dashes. The tightened first alternative
        ``[A-Z][A-Z0-9]{1,}-[A-Z0-9][A-Z0-9-]*`` now requires the
        post-dash group to START with an alphanumeric, so all-dash
        suffixes can't form a canonical name.

        NOTE: the regex still permits a single trailing dash on a
        non-empty suffix (e.g. ``TOKEN-XYZ-``) because the
        ``[A-Z0-9-]*`` continuation is greedy. That's the smaller
        sibling case the reviewer accepted; fully forbidding any
        trailing dash would need ``(?:[A-Z0-9-]*[A-Z0-9])?`` which
        is out of scope here."""
        fake = self._patch_upsert(monkeypatch, uuid4())
        data = _input("TOKEN--- has release date 2027-05-01")
        result = await EmitMemoryTriple().execute(_ctx(data))
        assert result.outcome == StepOutcome.SKIPPED
        assert result.detail["reason"] == "no_subject"
        fake.assert_not_called()

    async def test_subject_inferred_from_leftmost_match(self, monkeypatch):
        """When multiple phrase rows match the same predicate (e.g.
        ``"TOKEN-XYZ has release date is 2027-05-01"`` hits both
        ``\\bhas\\s+release\\s+date\\b`` and ``\\brelease\\s+date\\s+is\\b``),
        the subject must be sliced from BEFORE the leftmost hit —
        otherwise we'd cut inside the predicate chain and miss the
        identifier. The object still uses the rightmost end for a
        clean tail."""
        upserted_id = uuid4()
        fake = self._patch_upsert(monkeypatch, upserted_id)
        data = _input("TOKEN-XYZ has release date is 2027-05-01")
        result = await EmitMemoryTriple().execute(_ctx(data))
        assert result is None, f"Expected emit; got {result}"
        assert data.predicate == "release_date"
        assert data.object_value == "2027-05-01"
        fake.assert_called_once()
        eu = fake.call_args.args[0]
        assert eu.canonical_name == "TOKEN-XYZ"

    async def test_uppercase_uuid_subject_infers(self, monkeypatch):
        """UUIDs in the wild come in both cases. The UUID alternative
        uses an explicit ``[0-9a-fA-F]`` class so uppercase
        ``DEADBEEF-…`` matches without re-enabling global
        ``re.IGNORECASE`` (which would re-introduce the full-stack
        regression)."""
        self._patch_upsert(monkeypatch, uuid4())
        data = _input("DEADBEEF-1234-5678-9ABC-DEF012345678 status is shipped")
        await EmitMemoryTriple().execute(_ctx(data))
        assert data.predicate == "status"
        assert data.subject_entity_id is not None

    async def test_upsert_deferred_until_after_object_extraction(self, monkeypatch):
        """If object extraction skips (``object_unparseable``), the
        upsert MUST NOT have fired — otherwise we'd leak orphan
        Entity rows for memories that never persisted their triple.
        Content "TOKEN-NOPE has release date" with empty tail after
        the predicate fails ``_normalize_object`` and skips."""
        fake = self._patch_upsert(monkeypatch, uuid4())
        # Trailing "has release date" with no date after → unparseable.
        data = _input("TOKEN-NOPE has release date")
        result = await EmitMemoryTriple().execute(_ctx(data))
        assert result.outcome == StepOutcome.SKIPPED
        assert result.detail["reason"] == "object_unparseable"
        # Upsert must NOT have been called — that's the whole point of
        # deferring it until after object extraction succeeds.
        fake.assert_not_called()

    async def test_upsert_failure_degrades_to_skip(self, monkeypatch):
        """Upsert raising must not break the write pipeline."""
        from unittest.mock import AsyncMock as _AM

        fake = _AM(side_effect=RuntimeError("storage unavailable"))
        monkeypatch.setattr(
            "core_api.pipeline.steps.write.emit_memory_triple.upsert_entity", fake
        )
        data = _input("TOKEN-FAILS has release date 2027")
        result = await EmitMemoryTriple().execute(_ctx(data))
        assert result.outcome == StepOutcome.SKIPPED
        assert result.detail["reason"] == "subject_upsert_failed"

    async def test_lowercase_hyphenated_word_does_not_match(self, monkeypatch):
        """``IGNORECASE`` was removed from ``_IDENTIFIER_TOKEN`` so the
        first alternative ``[A-Z][A-Z0-9]{2,}-[A-Z0-9-]+`` no longer
        matches ordinary English words like ``full-stack``,
        ``long-term``, ``pre-release``. The heuristic must skip rather
        than create a spurious ``full-stack`` Entity."""
        fake = self._patch_upsert(monkeypatch, uuid4())
        for content in [
            "full-stack has release date 2027",
            "long-term status is ongoing",
            "pre-release priority is high",
        ]:
            data = _input(content)
            result = await EmitMemoryTriple().execute(_ctx(data))
            assert result.outcome == StepOutcome.SKIPPED, content
            assert result.detail["reason"] == "no_subject", content
        fake.assert_not_called()

    async def test_short_capitalised_prefix_id_infers(self, monkeypatch):
        """``PR-1234`` is the canonical 2-letter prefix shape used by
        GitHub / Jira / Linear. Earlier ``{2,}`` quantifier required 3+
        letters before the dash; ``{1,}`` lets ``PR``, ``OP``, ``QA``
        through while still rejecting ``A-1`` (only 1 char)."""
        self._patch_upsert(monkeypatch, uuid4())
        for content, expected_subject in [
            ("PR-1234 status is merged", "PR-1234"),
            ("OP-87 priority is high", "OP-87"),
        ]:
            data = _input(content)
            await EmitMemoryTriple().execute(_ctx(data))
            assert data.predicate is not None, content
            assert data.subject_entity_id is not None, content

    async def test_bare_decimal_is_not_an_identifier(self, monkeypatch):
        """``0.9``, ``1.5``, ``4.5`` are metric literals, not version
        IDs. The version alternative now requires either a ``v`` prefix
        (v2.4) or a 3-part dotted form (2.4.0) so bare decimals can't
        accidentally become Entity rows."""
        fake = self._patch_upsert(monkeypatch, uuid4())
        for content in [
            "0.9 status is shipped",
            "1.5 status is open",
            "4.5 priority is high",
        ]:
            data = _input(content)
            result = await EmitMemoryTriple().execute(_ctx(data))
            assert result.outcome == StepOutcome.SKIPPED, content
            assert result.detail["reason"] == "no_subject", content
        fake.assert_not_called()

    async def test_v_prefixed_and_three_part_versions_infer(self, monkeypatch):
        """Counter-test to ``test_bare_decimal_is_not_an_identifier``:
        the canonical version shapes ``v2.4`` and ``2.4.0`` ARE
        identifier-like and must still infer."""
        self._patch_upsert(monkeypatch, uuid4())
        for content in [
            "v2.4 status is shipped",
            "v1.0 status is GA",
            "2.4.0 status is shipped",
            "12.5.1 status is failed",
        ]:
            data = _input(content)
            result = await EmitMemoryTriple().execute(_ctx(data))
            assert result is None, f"Expected emit; got {result} for {content!r}"
            assert data.subject_entity_id is not None, content

    async def test_trailing_comma_before_predicate_still_infers(self, monkeypatch):
        """``"TOKEN-XYZ, has release date 2027"`` — the comma between
        the subject and the predicate would defeat the regex's
        ``\\s*$`` anchor without an explicit punctuation strip.
        ``_infer_subject_token`` strips trailing ``.,;:!`` before the
        sentence-split."""
        self._patch_upsert(monkeypatch, uuid4())
        data = _input("TOKEN-XYZ, has release date 2027-05-01")
        result = await EmitMemoryTriple().execute(_ctx(data))
        assert result is None, f"Expected emit; got {result}"
        assert data.predicate == "release_date"
        assert data.subject_entity_id is not None

    async def test_explicit_entity_links_still_take_precedence(self, monkeypatch):
        """When caller supplies role=subject link AND content has an
        identifier token, the link wins — no upsert call."""
        fake = self._patch_upsert(monkeypatch, uuid4())
        link_id = uuid4()
        data = _input("TOKEN-Z has release date 2027", subject_id=link_id)
        await EmitMemoryTriple().execute(_ctx(data))
        assert data.subject_entity_id == link_id
        fake.assert_not_called()


@pytest.mark.unit
class TestPipelineComposition:
    """Guard: STM and extract-only pipelines must NOT include EmitMemoryTriple."""

    def test_fast_pipeline_includes_step(self):
        from core_api.pipeline.compositions.write import build_fast_write_pipeline

        names = [s.name for s in build_fast_write_pipeline()._steps]
        assert "emit_memory_triple" in names
        assert names.index("emit_memory_triple") < names.index("check_exact_duplicate")
        assert names.index("merge_enrichment_fields") < names.index(
            "emit_memory_triple"
        )

    def test_strong_pipeline_includes_step(self):
        from core_api.pipeline.compositions.write import build_strong_write_pipeline

        names = [s.name for s in build_strong_write_pipeline()._steps]
        assert "emit_memory_triple" in names
        assert names.index("emit_memory_triple") < names.index("check_exact_duplicate")

    def test_persist_pipeline_includes_step(self):
        from core_api.pipeline.compositions.write import build_persist_pipeline

        names = [s.name for s in build_persist_pipeline()._steps]
        assert "emit_memory_triple" in names

    def test_stm_pipeline_excludes_step(self):
        from core_api.pipeline.compositions.write import build_stm_write_pipeline

        names = [s.name for s in build_stm_write_pipeline()._steps]
        assert "emit_memory_triple" not in names

    def test_enrichment_pipeline_excludes_step(self):
        # The enrichment-only path (extract-only mode) doesn't persist, so
        # there's no value in emitting triples there.
        from core_api.pipeline.compositions.write import build_enrichment_pipeline

        names = [s.name for s in build_enrichment_pipeline()._steps]
        assert "emit_memory_triple" not in names


@pytest.mark.unit
class TestAllowlistParity:
    """Every predicate the step can emit must be in SINGLE_VALUE_PREDICATES.

    This is the contract that makes the RDF contradiction detector
    (contradiction_detector.py) actually find the emitted rows.
    """

    def test_phrase_table_predicates_are_subset_of_allowlist(self):
        from core_api.pipeline.steps.write.emit_memory_triple import (
            _PHRASE_TO_PREDICATE,
        )

        emitted = {predicate for _pat, predicate in _PHRASE_TO_PREDICATE}
        missing = emitted - SINGLE_VALUE_PREDICATES
        assert not missing, (
            f"Predicates in EmitMemoryTriple not present in SINGLE_VALUE_PREDICATES: {missing}"
        )


@pytest.mark.unit
class TestTenantConfigFlag:
    """Default-true contract for the new flag."""

    def test_default_is_true(self):
        from core_api.services.organization_settings import ResolvedConfig

        cfg = ResolvedConfig(org_settings={})
        assert cfg.triple_emission_enabled is True

    def test_explicit_false_disables(self):
        from core_api.services.organization_settings import ResolvedConfig

        cfg = ResolvedConfig(org_settings={"write": {"triple_emission_enabled": False}})
        assert cfg.triple_emission_enabled is False

    def test_explicit_true_enables(self):
        from core_api.services.organization_settings import ResolvedConfig

        cfg = ResolvedConfig(org_settings={"write": {"triple_emission_enabled": True}})
        assert cfg.triple_emission_enabled is True
