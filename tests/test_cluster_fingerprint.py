"""Cluster fingerprint tests (Skill Factory SF-103).

Pinning the six stability properties P1–P6 from the module
docstring as enforced invariants. The empirical ≥99% stability
target lives in SF-105 (eval harness); this file pins the
*formula's* deterministic guarantees so a future refactor of the
canonicalization rules can't silently break them.
"""

from __future__ import annotations

import pytest

from core_api.services.forge.fingerprint import (
    ENTITY_TOP_K,
    FINGERPRINT_FORMULA_VERSION,
    GOAL_PHRASE_TOP_K,
    STEP_WORDS_CAP,
    ClusterFingerprintInputs,
    canonical_domain,
    canonical_entities,
    canonical_goal_phrase,
    canonical_step_skeleton,
    compute_fingerprint,
    render_canonical_form,
)


# Convenience factory.
def _inputs(**overrides) -> ClusterFingerprintInputs:
    base = {
        "goal_phrase": "Deploy script hangs on step 7 in eu-west",
        "domain": "devops",
        "entity_ids": ["e1", "e2", "e3"],
        "step_skeleton": ["run preflight check", "execute deploy script", "rollback on failure"],
        "entity_centralities": None,
    }
    base.update(overrides)
    return ClusterFingerprintInputs(**base)


# ── Format + version contract ─────────────────────────────────────


@pytest.mark.unit
class TestFingerprintFormat:
    def test_version_prefix(self):
        fp = compute_fingerprint(_inputs()).fp
        assert fp.startswith(f"fp:{FINGERPRINT_FORMULA_VERSION}:")

    def test_hash_is_64_hex_chars(self):
        fp = compute_fingerprint(_inputs()).fp
        h = fp.split(":")[-1]
        assert len(h) == 64
        # All hex (lowercase).
        assert all(c in "0123456789abcdef" for c in h)

    def test_canonical_form_is_diffable(self):
        # Render exposes the canonical form so operators can diff
        # two clusters' canonical_forms to explain a hash difference.
        cf = compute_fingerprint(_inputs()).canonical_form
        assert cf.startswith("goal=")
        assert "\ndomain=" in cf
        assert "\nentities=" in cf
        assert "\nsteps=" in cf

    def test_top_k_constants_documented(self):
        # If someone bumps these without changing FINGERPRINT_FORMULA_VERSION
        # they'd silently re-fingerprint every existing cluster. Pin
        # them here so a refactor can't slip through unnoticed.
        assert ENTITY_TOP_K == 5
        assert GOAL_PHRASE_TOP_K == 6
        assert STEP_WORDS_CAP == 4


# ── P1: Determinism ───────────────────────────────────────────────


@pytest.mark.unit
class TestDeterminism:
    def test_same_inputs_same_fingerprint(self):
        fp1 = compute_fingerprint(_inputs()).fp
        fp2 = compute_fingerprint(_inputs()).fp
        assert fp1 == fp2

    def test_completely_empty_inputs_have_stable_fingerprint(self):
        empty = ClusterFingerprintInputs(
            goal_phrase="", domain="", entity_ids=[], step_skeleton=[]
        )
        fp1 = compute_fingerprint(empty).fp
        fp2 = compute_fingerprint(empty).fp
        assert fp1 == fp2
        # And it's distinguishable from a non-empty cluster.
        assert fp1 != compute_fingerprint(_inputs()).fp

    def test_different_clusters_different_fingerprints(self):
        # Sanity: sha256 actually distinguishes inputs.
        fp_a = compute_fingerprint(_inputs(domain="devops")).fp
        fp_b = compute_fingerprint(_inputs(domain="security")).fp
        assert fp_a != fp_b


# ── P2: Permutation invariance ────────────────────────────────────


@pytest.mark.unit
class TestPermutationInvariance:
    def test_entity_id_permutation_no_effect(self):
        a = _inputs(entity_ids=["e1", "e2", "e3"])
        b = _inputs(entity_ids=["e3", "e1", "e2"])
        assert compute_fingerprint(a).fp == compute_fingerprint(b).fp

    def test_goal_phrase_word_permutation_no_effect(self):
        # The goal phrase is a bag-of-words distillation (sorted + deduped).
        a = _inputs(goal_phrase="deploy hangs eu-west")
        b = _inputs(goal_phrase="eu-west hangs deploy")
        assert compute_fingerprint(a).fp == compute_fingerprint(b).fp

    def test_goal_phrase_duplicate_words_no_effect(self):
        a = _inputs(goal_phrase="deploy deploy deploy hangs")
        b = _inputs(goal_phrase="deploy hangs")
        assert compute_fingerprint(a).fp == compute_fingerprint(b).fp

    def test_domain_case_no_effect(self):
        a = _inputs(domain="DevOps")
        b = _inputs(domain="devops")
        c = _inputs(domain="  DEVOPS  ")
        assert compute_fingerprint(a).fp == compute_fingerprint(b).fp == compute_fingerprint(c).fp


# ── P3: Step ordering significance + within-step normalization ────


@pytest.mark.unit
class TestStepOrdering:
    def test_step_order_significant(self):
        a = _inputs(step_skeleton=["A", "B", "C"])
        b = _inputs(step_skeleton=["C", "B", "A"])
        assert compute_fingerprint(a).fp != compute_fingerprint(b).fp

    def test_step_word_order_preserved_within_step(self):
        # "deploy script" and "script deploy" are different procedures
        # (verb-then-object semantics matter).
        a = _inputs(step_skeleton=["deploy script", "verify health"])
        b = _inputs(step_skeleton=["script deploy", "health verify"])
        assert compute_fingerprint(a).fp != compute_fingerprint(b).fp

    def test_step_punctuation_stripped(self):
        a = _inputs(step_skeleton=["deploy, script!", "verify health"])
        b = _inputs(step_skeleton=["deploy script", "verify health"])
        assert compute_fingerprint(a).fp == compute_fingerprint(b).fp

    def test_step_cap_at_4_words(self):
        # Words past the cap don't influence the fingerprint.
        a = _inputs(step_skeleton=["one two three four FIVE SIX"])
        b = _inputs(step_skeleton=["one two three four"])
        assert compute_fingerprint(a).fp == compute_fingerprint(b).fp


# ── P4: Top-K stability (cluster growth) ──────────────────────────


@pytest.mark.unit
class TestTopKEntityStability:
    def test_adding_low_centrality_entity_does_not_shift(self):
        base_ents = ["e1", "e2", "e3", "e4", "e5"]
        centralities = {e: 1.0 for e in base_ents}
        # Sixth entity, very low centrality — won't crack top 5.
        plus_one_ents = base_ents + ["e6"]
        plus_one_centralities = {**centralities, "e6": 0.001}

        a = _inputs(entity_ids=base_ents, entity_centralities=centralities)
        b = _inputs(entity_ids=plus_one_ents, entity_centralities=plus_one_centralities)
        assert compute_fingerprint(a).fp == compute_fingerprint(b).fp

    def test_high_centrality_newcomer_does_shift(self):
        # If a new entity DOES break into top 5 by centrality, the
        # fingerprint legitimately changes — the cluster's identity
        # really did move.
        base_ents = ["e1", "e2", "e3", "e4", "e5"]
        centralities = {e: 1.0 for e in base_ents}
        plus_one_ents = base_ents + ["e6"]
        plus_one_centralities = {**{e: 0.1 for e in base_ents}, "e6": 10.0}

        a = _inputs(entity_ids=base_ents, entity_centralities=centralities)
        b = _inputs(entity_ids=plus_one_ents, entity_centralities=plus_one_centralities)
        assert compute_fingerprint(a).fp != compute_fingerprint(b).fp

    def test_centrality_path_dedupes_inputs(self):
        """Duplicates in entity_ids must not survive into the top-K
        selection on the centrality path. Without this, duplicate
        inputs burn top-K slots and silently shift the canonical
        form. The no-centrality path already dedupes via
        ``sorted(set(...))`` — this asserts the centrality path
        matches that contract."""
        ents = ["e1", "e1", "e2", "e2", "e3", "e4", "e5", "e6"]
        cent = {e: 1.0 for e in set(ents)}
        # With dedup: top-5 by id ASC is e1..e5. Without dedup, the
        # ranked list would be ["e1","e1","e2","e2","e3"] — the
        # duplicates would crowd out e4 and e5.
        out = canonical_entities(ents, centralities=cent)
        assert out == ["e1", "e2", "e3", "e4", "e5"]

    def test_centrality_tie_broken_deterministically(self):
        # When two entities have identical centrality, we tie-break by
        # id ASC — so the canonical form is deterministic regardless
        # of input order.
        ents_v1 = ["e1", "e2", "e3", "e4", "e5", "e6", "e7"]
        ents_v2 = ["e7", "e2", "e6", "e1", "e5", "e3", "e4"]
        cent = {e: 1.0 for e in ents_v1}

        a = _inputs(entity_ids=ents_v1, entity_centralities=cent)
        b = _inputs(entity_ids=ents_v2, entity_centralities=cent)
        assert compute_fingerprint(a).fp == compute_fingerprint(b).fp

    def test_no_centrality_falls_back_to_id_sort(self):
        a = _inputs(entity_ids=["z", "a", "m"], entity_centralities=None)
        b = _inputs(entity_ids=["a", "m", "z"], entity_centralities=None)
        assert compute_fingerprint(a).fp == compute_fingerprint(b).fp


# ── P5: Token normalization (small surface perturbations) ─────────


@pytest.mark.unit
class TestTokenNormalization:
    def test_simple_plural_stripped(self):
        a = _inputs(goal_phrase="deploys hang frequent")
        b = _inputs(goal_phrase="deploy hang frequent")
        # "deploys" → "deploy", "frequent" stays
        assert compute_fingerprint(a).fp == compute_fingerprint(b).fp

    def test_double_s_protected(self):
        # "press" must NOT plural-strip to "pres" — would mangle real words.
        a = _inputs(goal_phrase="press button release")
        b = _inputs(goal_phrase="pres button release")
        # Different (pres is a different stem after the protection).
        assert compute_fingerprint(a).fp != compute_fingerprint(b).fp

    def test_short_word_protected(self):
        # 4-char and shorter words don't get plural-stripped — protects
        # words like "bus", "gas", "ass" etc.
        a = _inputs(goal_phrase="bus stop")
        # If we'd plural-stripped, "bus" → "bu" → fingerprint shift.
        assert "bus" in canonical_goal_phrase("bus stop")

    def test_stopwords_dropped(self):
        a = _inputs(goal_phrase="The deploy is on step 7")
        b = _inputs(goal_phrase="deploy step 7")
        assert compute_fingerprint(a).fp == compute_fingerprint(b).fp

    def test_punctuation_stripped(self):
        a = _inputs(goal_phrase="deploy: step 7, eu-west!")
        b = _inputs(goal_phrase="deploy step 7 eu west")
        assert compute_fingerprint(a).fp == compute_fingerprint(b).fp


# ── P6: Domain canonicalization ───────────────────────────────────


@pytest.mark.unit
class TestDomainCanonicalization:
    def test_lowercase(self):
        assert canonical_domain("DevOps") == "devops"

    def test_strip_outer_whitespace(self):
        assert canonical_domain("  security  ") == "security"

    def test_empty_or_none(self):
        assert canonical_domain("") == ""

    def test_internal_whitespace_preserved(self):
        # Multi-word domains aren't expected, but if one slips
        # through, internal spaces are preserved (no over-normalisation).
        assert canonical_domain("Multi Domain") == "multi domain"


# ── Goal-phrase top-K cap ─────────────────────────────────────────


@pytest.mark.unit
class TestGoalPhraseTopK:
    def test_top_k_caps_at_constant(self):
        # 10 distinct tokens; only the first GOAL_PHRASE_TOP_K (sorted)
        # influence the fingerprint.
        a = _inputs(goal_phrase="apple banana cherry date elderberry "
                                "fig grape honeydew imbe jackfruit")
        # First 6 sorted: apple, banana, cherry, date, elderberry, fig
        # Adding "kiwi" (which sorts after "jackfruit") wouldn't crack the top 6.
        b = _inputs(goal_phrase="apple banana cherry date elderberry "
                                "fig grape honeydew imbe jackfruit kiwi")
        assert compute_fingerprint(a).fp == compute_fingerprint(b).fp


# ── Canonical-form integration (the formula's anatomy) ────────────


@pytest.mark.unit
class TestCanonicalFormAnatomy:
    def test_form_has_all_four_sections(self):
        cf = render_canonical_form(_inputs())
        assert "goal=" in cf
        assert "domain=" in cf
        assert "entities=" in cf
        assert "steps=" in cf

    def test_entities_separator_is_comma(self):
        cf = render_canonical_form(_inputs(entity_ids=["e1", "e2"], entity_centralities=None))
        assert "entities=e1,e2" in cf

    def test_steps_separator_is_pipe(self):
        cf = render_canonical_form(_inputs(step_skeleton=["one", "two"]))
        assert "steps=one|two" in cf

    def test_section_separator_is_newline(self):
        cf = render_canonical_form(_inputs())
        assert "\ngoal=" not in cf  # first line starts with goal=
        # 3 newlines between 4 sections.
        assert cf.count("\n") == 3


# ── Adversarial: edge cases that could destabilize a real fleet ───


@pytest.mark.unit
class TestAdversarialPerturbations:
    """Cases that a session re-runner would actually encounter. If
    any of these fail, real-world ≥99% stability is at risk."""

    def test_extra_whitespace_in_goal_phrase(self):
        a = _inputs(goal_phrase="deploy   step    7")
        b = _inputs(goal_phrase="deploy step 7")
        assert compute_fingerprint(a).fp == compute_fingerprint(b).fp

    def test_unicode_in_goal_phrase_normalised_to_ascii(self):
        # Our regex strips non-[a-z0-9] so an em-dash or smart quote
        # disappears like punctuation. Stability preserved.
        a = _inputs(goal_phrase="deploy — step 7")
        b = _inputs(goal_phrase="deploy step 7")
        assert compute_fingerprint(a).fp == compute_fingerprint(b).fp

    def test_step_skeleton_growth_does_not_affect_existing_step_fps(self):
        # Adding a 4th step changes the cluster, but the FIRST THREE
        # steps' canonical rendering remains identical (no
        # cross-step interference).
        a_steps = ["preflight", "deploy", "verify"]
        b_steps = ["preflight", "deploy", "verify", "rollback"]
        # The fingerprints differ (P3 — step ordering significant),
        # but the canonical form for the first three steps is
        # identical inside both.
        cf_a = render_canonical_form(_inputs(step_skeleton=a_steps))
        cf_b = render_canonical_form(_inputs(step_skeleton=b_steps))
        # Both start with the same steps; b's only differs in trailing
        # |rollback.
        assert cf_a.split("steps=")[1] + "|rollback" == cf_b.split("steps=")[1]

    def test_inputs_dataclass_is_frozen(self):
        # Defensive: the inputs are passed around to logging /
        # comparison code. Frozen means downstream mutations can't
        # corrupt the canonical form.
        inp = _inputs()
        with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
            inp.domain = "tampered"  # type: ignore[misc]


# ── Canonicalization unit details (white-box) ─────────────────────


@pytest.mark.unit
class TestCanonicalGoalPhraseInternals:
    def test_lowercase_normalisation(self):
        assert canonical_goal_phrase("DEPLOY HANGS") == "deploy hang"

    def test_dedup(self):
        # "the deploy and the deploy" → ["deploy"] after stopword
        # removal + dedupe + plural stripping.
        assert canonical_goal_phrase("the deploy and the deploy") == "deploy"

    def test_sort_order_is_ascii(self):
        assert canonical_goal_phrase("zebra apple mango") == "apple mango zebra"


@pytest.mark.unit
class TestCanonicalEntitiesInternals:
    def test_dedupe_in_no_centrality_path(self):
        assert canonical_entities(["e1", "e1", "e2", "e2"]) == ["e1", "e2"]

    def test_top_k_clamp_in_no_centrality_path(self):
        ents = [f"e{i:02d}" for i in range(20)]
        out = canonical_entities(ents)
        assert len(out) == ENTITY_TOP_K
        assert out == sorted(ents)[:ENTITY_TOP_K]

    def test_empty_input(self):
        assert canonical_entities([]) == []


@pytest.mark.unit
class TestCanonicalStepSkeletonInternals:
    def test_empty_step_preserved(self):
        out = canonical_step_skeleton(["", "deploy script", ""])
        assert out == ["", "deploy script", ""]
        # The empty positions matter so step indices don't shift.

    def test_word_count_cap_per_step(self):
        out = canonical_step_skeleton(["one two three four five six seven"])
        # Cap at STEP_WORDS_CAP=4.
        assert out == ["one two three four"]
