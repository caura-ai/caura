"""09/02 M-41 — the oversized-paragraph splitter could return oversized pieces.

`_split_oversized_paragraph` documents "one or more substrings, each under
`hard_tokens` tokens". It packs SENTENCES, which can only split BETWEEN them,
so it had nothing to offer for:

* a single sentence already over the cap;
* text pysbd finds no boundaries in — a minified blob, a CSV line, a wall of
  prose with no terminators.

Those came back as one piece of whatever size they started at. Measured before
the fix: a 12,001-token paragraph returned unchanged against a 3,000 cap, and a
20,001-token one likewise. The contract was not merely approximate, it was
absent.

The enforcement path splits on the tiktoken encoding rather than on words,
because the cap is counted in tokens and a word split still overshoots on text
that tokenises densely — CJK, base64, long identifiers — which is exactly the
shape that reaches this path in the first place.
"""

import re

import pytest

from core_api.services.ingest_chunking import (
    SECTION_HARD_TOKENS,
    _count_tokens,
    _split_oversized_paragraph,
)

pytestmark = pytest.mark.unit

CAP = SECTION_HARD_TOKENS


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


# Shapes chosen for how they TOKENISE, not how they read: each defeats a
# different assumption the old implementation made.
_HARD_INPUTS = {
    "one enormous sentence, no terminator": "alpha beta gamma delta " * 3000,
    "one enormous sentence with a period": ("word " * 12000).strip() + ".",
    "no sentence boundaries at all": "x" + (" y" * 20000),
    "CJK — several tokens per character": "这是一个测试句子" * 4000,
    "emoji — surrogate-pair heavy": "🙂🎉" * 6000,
    "arabic — combining marks": "مرحبا بالعالم " * 5000,
}


@pytest.mark.parametrize("text", _HARD_INPUTS.values(), ids=list(_HARD_INPUTS))
def test_no_piece_exceeds_the_cap(text):
    """The contract the docstring always claimed."""
    parts = _split_oversized_paragraph(text, CAP)
    oversized = [_count_tokens(p) for p in parts if _count_tokens(p) > CAP]
    assert not oversized, f"{len(oversized)} piece(s) over {CAP}: {oversized}"


@pytest.mark.parametrize("text", _HARD_INPUTS.values(), ids=list(_HARD_INPUTS))
def test_splitting_loses_no_content(text):
    """A size fix that drops text would be worse than the bug. Compared with
    whitespace normalised, since packing re-joins on single spaces."""
    parts = _split_oversized_paragraph(text, CAP)
    assert _norm("".join(parts)) == _norm(text)


@pytest.mark.parametrize("text", _HARD_INPUTS.values(), ids=list(_HARD_INPUTS))
def test_no_replacement_characters_are_introduced(text):
    """One CJK character spans several cl100k_base tokens, so cutting between
    tokens can land mid-character and `decode` yields U+FFFD — silently
    corrupting the text this function exists to preserve.

    This test caught exactly that in my first attempt: the docstring described
    a round-trip guard that the code did not implement.
    """
    parts = _split_oversized_paragraph(text, CAP)
    assert "�" not in "".join(parts)


# ── behaviour that must not change ───────────────────────────────────────


def test_normal_prose_still_splits_on_sentences():
    """Enforcement is a LAST resort. Ordinary text must still be cut at
    sentence boundaries, not mid-sentence by the token splitter."""
    text = "This is a sentence. " * 800
    parts = _split_oversized_paragraph(text, CAP)
    assert len(parts) >= 2
    # Sentence packing keeps terminators at the ends of pieces.
    assert sum(p.rstrip().endswith(".") for p in parts) >= len(parts) - 1


def test_text_under_the_cap_is_returned_untouched():
    text = "Just one short sentence."
    assert _split_oversized_paragraph(text, CAP) == [text]


def test_the_enforcement_runs_after_sentence_packing():
    """Ordering is the whole design: packing first preserves meaning, the token
    split only rescues what packing could not.

    Reads the function with docstrings stripped. The docstring NAMES
    ``_hard_split_on_tokens`` before the loop that must precede it, so a plain
    substring search finds the prose and asserts the wrong thing — which is
    exactly what it did when I first wrote this.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(_split_oversized_paragraph).lstrip())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            node.value.value = ""
    code = ast.unparse(tree)
    assert code.index("for sent in sents") < code.index("_hard_split_on_tokens")


def test_a_single_token_cap_still_terminates():
    """Degenerate but reachable via config; must not loop or return nothing."""
    parts = _split_oversized_paragraph("alpha beta gamma delta", 1)
    assert parts
    assert all(_count_tokens(p) <= 1 for p in parts)
