"""A69 — a degraded extraction was silent, and one absent field caused it.

Two independent gaps, both on the path where extraction stops using the model
and starts using a regex:

1. ``ExtractedEntity.role`` was a required ``str``. Nothing enforces the schema
   ``_do_extract`` sends, so an entity whose payload omitted ``role`` was
   dropped by ``_parse_graph_lenient``; when the model omitted it on EVERY
   entity the loss was total, ``_do_extract`` raised, and the chain fell
   through to ``_fake_extract``. That discards the model's canonical names and
   types and substitutes bare capitalised bigrams typed ``unknown``.

   Observed twice while measuring A68: a draft prompt whose examples named a
   single field taught the model to stop emitting ``role`` (3/3 entities
   dropped), and main's own prompt did it on "The billing service owner is
   Dana." (``dropped={'entities': 2}``, no subject). Same class as the
   ``Mention.cluster_id`` prod incident (#788).

2. Nothing said it had happened. The chain logs "All LLM providers failed for
   %s" with the service interpolated into the message, so it cannot be filtered
   or counted per service; downstream, NOTHING distinguishes a heuristic graph
   from a real one. Worse, ``subject_writeback`` logged the "set" and the
   "ambiguous, refused" cases and logged NOTHING when no entity claimed a
   subject — which is precisely the state a heuristic graph always leaves the
   row in, because ``_fake_extract`` stamps ``role="mentioned"`` on everything.
   A memory that can never take part in subject-scoped contradiction detection
   was indistinguishable from one that was never processed: the same "ran and
   found nothing" vs "never ran" ambiguity D3 closed for the detector.

Scope note, recorded because it will look overstated otherwise: after A68
merged, gap (1) no longer reproduces against the live model — 0 role omissions
across 18 sentences, including deliberately messy multi-entity, code-like and
non-English content. The default is hardening against a latent trigger, not a
fix for an active one. Gap (2) is live on main today and is what makes gap (1)
expensive: while a degrade is invisible, every extraction measurement is
suspect, which is what cost a full measurement round on A68.
"""

import logging

import pytest

from core_api.services.entity_extraction import (
    ExtractedEntity,
    _fake_extract,
    _parse_graph_lenient,
)

pytestmark = pytest.mark.unit


# ── (1) an absent role no longer discards the entity ──────────────────────


def test_role_defaults_instead_of_being_required():
    e = ExtractedEntity(canonical_name="acme", entity_type="organization")
    assert e.role == "mentioned"


def test_payload_without_role_is_kept_not_dropped():
    """The regression in one assertion: this payload used to yield zero
    entities and a total-loss raise."""
    raw = {
        "entities": [
            {"canonical_name": "acme (delaware)", "entity_type": "organization"},
            {"canonical_name": "annual report", "entity_type": "artifact"},
        ]
    }
    graph, dropped = _parse_graph_lenient(raw)
    assert dropped == {}
    assert [e.canonical_name for e in graph.entities] == [
        "acme (delaware)",
        "annual report",
    ]


def test_defaulting_can_never_invent_a_subject():
    """The safety property that makes the default safe rather than convenient.

    Subject write-back fires on exactly one ``role="subject"`` entity. If the
    default were "subject", a role-less payload would silently nominate one —
    and a WRONG subject is worse than none, because A1 #17 treats the column as
    authoritative. "mentioned" can only decline to name a subject, which is
    what an absent field actually means."""
    assert ExtractedEntity.model_fields["role"].default == "mentioned"


def test_a_genuinely_malformed_entity_is_still_dropped():
    """The default must not turn the lenient parser into a permissive one:
    ``canonical_name`` carries no default and its absence is still fatal to the
    item."""
    graph, dropped = _parse_graph_lenient(
        {"entities": [{"entity_type": "organization"}]}
    )
    assert dropped == {"entities": 1}
    assert graph.entities == []


# ── (2) the degrade is visible ────────────────────────────────────────────


def test_heuristic_output_never_claims_a_subject():
    """Load-bearing for the log line below: "zero subjects" is only a usable
    signature of a degrade because the heuristic cannot produce one."""
    graph = _fake_extract("Anna Bergstrom joined Acme Corp last Tuesday.")
    assert graph.entities, "the heuristic found nothing to assert about"
    assert {e.role for e in graph.entities} == {"mentioned"}


async def test_degrade_is_logged_at_error_with_a_greppable_slug(caplog, monkeypatch):
    """WARNING put this in the same bucket as ordinary retry noise, and the
    service name lived inside the message rather than a field. Both are why a
    silent regex degrade went unnoticed long enough to corrupt a measurement.

    Drives the REAL call site: the chain is stubbed to do what it does when
    every provider has failed — invoke the ``fake_fn`` it was handed — so this
    fails if that argument is ever wired back to a bare ``_fake_extract``.
    """
    from core_api.services import entity_extraction as ee

    async def _chain_gives_up(*_a, fake_fn, **_kw):
        return fake_fn()

    monkeypatch.setattr(ee, "call_with_fallback", _chain_gives_up)
    monkeypatch.setattr(ee.settings, "entity_extraction_provider", "openai")

    with caplog.at_level(logging.ERROR, logger=ee.__name__):
        graph = await ee.extract_entities_from_content(
            "Anna Bergstrom joined Acme Corp.", "episodic"
        )

    recs = [
        r
        for r in caplog.records
        if "entity_extraction_degraded_to_heuristic" in r.getMessage()
    ]
    assert recs, "a degrade to the regex heuristic produced no ERROR record"
    assert recs[0].levelno == logging.ERROR
    assert getattr(recs[0], "event", None) == "entity_extraction_degraded"
    assert graph.entities  # it still degrades gracefully, it just says so


async def test_an_intentional_fake_provider_is_not_reported_as_a_degrade(monkeypatch):
    """A tenant configured for ``fake`` is not failing, and must not raise an
    ERROR on every write. That path short-circuits before the chain, so this
    pins the distinction rather than assuming it."""
    from core_api.services import entity_extraction as ee

    monkeypatch.setattr(ee.settings, "entity_extraction_provider", "fake")
    logged = []
    monkeypatch.setattr(ee.logger, "error", lambda *a, **k: logged.append(a))

    graph = await ee.extract_entities_from_content(
        "Anna Bergstrom joined Acme Corp.", "episodic"
    )
    assert graph.entities
    assert logged == []


# ── (2b) the zero-subject branch is no longer silent ──────────────────────
#
# Driving the worker's write-back needs a live storage client, so these pin the
# branch structurally — the same approach ``test_a54_d3_rdf_visibility_and_logging``
# uses for the D3 completion log, and for the same reason: the defect is a
# MISSING log statement, which no amount of mocking makes observable if the
# branch that should emit it does not exist.


def test_zero_subject_writeback_is_logged():
    """ "Set it", "found two and refused" and "found none" must be three
    distinguishable outcomes. The third used to log nothing."""
    import inspect

    from core_api.services import entity_extraction_worker as w

    src = inspect.getsource(w)
    assert "skipped_no_subject" in src


def test_no_subject_branch_is_an_else_not_a_second_guard():
    """An ``elif len(subject_ids) == 0`` would be dead-ish and drift from the
    other two arms. Pin that the outcomes are exhaustive: every path through
    the write-back emits exactly one ``subject_writeback`` line."""
    import inspect

    from core_api.services import entity_extraction_worker as w

    src = inspect.getsource(w)
    block = src[src.index("subject_ids = {") :]
    block = block[: block.index("# Upsert relations")]
    assert "elif len(subject_ids) > 1:" in block
    assert "\n            else:\n" in block
    # one per outcome: set/kept_existing, failed, ambiguous, none
    assert block.count("subject_writeback") == 4
