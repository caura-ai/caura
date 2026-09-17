"""A66 — updates to person-valued facts could never supersede anything.

Root cause was one undefined word. ``EXTRACTION_PROMPT`` listed
``role: one of subject, object, mentioned`` and never said what "subject" meant,
so for "The X primary oncall is Marta" the model could defensibly mark either
``marta`` or ``x primary oncall`` as the subject — and it picked differently on
different writes.

Everything downstream then behaved correctly on unstable input:
  * the worker only sets ``subject_entity_id`` when exactly ONE entity claims
    role=subject;
  * ``_subjects_differ_with_certainty`` drops candidates whose subjects differ.

So each new value became a new subject, no candidate survived to the judge, and
the old value was never retired.

Measured on a live stack, same three-write chain, before and after:

    before   run 1 resolved | run 2 fired NOTHING (3 subjects) | run 3 partial
             -> 2/3 runs ended with contradictory claims co-active
    after    3/3 resolved: A conflicted, B conflicted(sup=A), C active(sup=B)
             -> 0/3, and all three rows now share ONE subject entity

These tests pin the prompt contract. They cannot prove model behaviour — only
the live chain repro does that (benchmark/a66_subject_stability.py) — but they
fail loudly if the definition is dropped again, which is what made this
regression invisible for so long.
"""

import pytest

from core_api.services.entity_extraction import EXTRACTION_PROMPT

pytestmark = pytest.mark.unit


def test_prompt_defines_what_subject_means():
    """The bug was the ABSENCE of this definition, so its presence is the fix."""
    assert "the entity the statement is ABOUT" in EXTRACTION_PROMPT
    assert "grammatical subject" in EXTRACTION_PROMPT


def test_prompt_says_a_named_value_is_still_the_object():
    """The specific confusion: a person-valued predicate. "Dana" is the value,
    not the thing the sentence is about."""
    assert "even when" in EXTRACTION_PROMPT
    assert "named person" in EXTRACTION_PROMPT


def test_prompt_states_subject_stability_across_updates():
    """Stability across writes is the property contradiction detection depends
    on — a subject that changes when the value changes breaks supersession."""
    assert "it does not change because the value changed" in EXTRACTION_PROMPT


def test_prompt_carries_worked_examples():
    """A rule without an example was what the model was already working from."""
    assert "billing service owner" in EXTRACTION_PROMPT
    assert "deploy window" in EXTRACTION_PROMPT


def test_prompt_explains_the_consequence():
    """Keeps the next editor from trimming the rule as verbose: the paragraph
    exists because removing it silently disables updates."""
    assert "would never supersede" in EXTRACTION_PROMPT
