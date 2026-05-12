"""Tests for the Block-based chunker introduced in PR #7 / A4.

Covers:
- ``parse_markdown`` walks markdown-it AST and emits typed Blocks
- ``parse_plaintext`` splits on blank lines
- ``detect_format`` heuristic
- ``chunk_blocks`` greedy packer + heading boundaries + breadcrumb
- ``chunk_blocks`` handles oversized lists/tables/code blocks atomically
- ``chunk_blocks`` sentence-splits oversized paragraphs
- ``doc_token_count`` rough total
"""

from __future__ import annotations

import pytest

from core_api.services.ingest_chunking import (
    SECTION_HARD_TOKENS,
    Block,
    chunk_blocks,
    detect_format,
    doc_token_count,
    parse,
    parse_markdown,
    parse_plaintext,
)

# ---------------------------------------------------------------------------
# detect_format
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "# A heading\n\nBody text.",
        "Some intro.\n\n## Subheading\n\nMore text.",
        "Inline ```code``` doesn't count, but:\n\n```python\nx = 1\n```",
    ],
)
def test_detect_format_markdown(text):
    assert detect_format(text) == "markdown"


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "Just a paragraph of plain text.",
        "Two paragraphs.\n\nNo headings, no fences.",
        # ``#`` without a following space is not a markdown heading
        "#notaheading and some other text follows here.",
    ],
)
def test_detect_format_plaintext(text):
    assert detect_format(text) == "plaintext"


# ---------------------------------------------------------------------------
# parse_plaintext
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_plaintext_splits_on_blank_lines():
    text = "Paragraph one.\n\nParagraph two.\n\n\nParagraph three."
    blocks = parse_plaintext(text)
    assert len(blocks) == 3
    assert all(b.type == "paragraph" for b in blocks)
    assert [b.text for b in blocks] == [
        "Paragraph one.",
        "Paragraph two.",
        "Paragraph three.",
    ]


@pytest.mark.unit
def test_parse_plaintext_ignores_empty_input():
    assert parse_plaintext("") == []
    assert parse_plaintext("\n\n\n   \n") == []


# ---------------------------------------------------------------------------
# parse_markdown
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_markdown_basic_heading_and_paragraph():
    text = "# Title\n\nFirst paragraph.\n\n## Subsection\n\nSecond paragraph."
    blocks = parse_markdown(text)
    types = [b.type for b in blocks]
    assert types == ["heading", "paragraph", "heading", "paragraph"]
    assert blocks[0].depth == 1
    assert blocks[0].text == "Title"
    assert blocks[2].depth == 2
    assert blocks[2].text == "Subsection"


@pytest.mark.unit
def test_parse_markdown_bullet_list():
    text = "## Items\n\n- alpha\n- beta\n- gamma"
    blocks = parse_markdown(text)
    list_blocks = [b for b in blocks if b.type == "list"]
    assert len(list_blocks) == 1
    body = list_blocks[0].text
    assert "alpha" in body and "beta" in body and "gamma" in body


@pytest.mark.unit
def test_parse_markdown_fenced_code_preserved():
    text = "Intro.\n\n```python\ndef f(): return 1\n```\n\nOutro."
    blocks = parse_markdown(text)
    code = [b for b in blocks if b.type == "code"]
    assert len(code) == 1
    assert "def f(): return 1" in code[0].text


@pytest.mark.unit
def test_parse_markdown_table():
    text = "| col A | col B |\n|-------|-------|\n| row 1 | val 1 |\n| row 2 | val 2 |\n"
    blocks = parse_markdown(text)
    tables = [b for b in blocks if b.type == "table"]
    assert len(tables) == 1
    assert "row 1" in tables[0].text
    assert "val 2" in tables[0].text


# ---------------------------------------------------------------------------
# chunk_blocks: heading-bounded packing + breadcrumb
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_chunk_blocks_packs_small_blocks_into_one_section():
    """Three small paragraphs all under the soft cap → one Section."""
    blocks = [
        Block(type="paragraph", text=f"This is paragraph number {i} with sufficient text.") for i in range(3)
    ]
    sections = chunk_blocks(blocks)
    assert len(sections) == 1
    assert "paragraph number 0" in sections[0].text
    assert "paragraph number 2" in sections[0].text


@pytest.mark.unit
def test_chunk_blocks_h2_creates_new_section():
    """An H2 boundary closes the prior section and starts a new one."""
    blocks = [
        Block(type="heading", depth=1, text="Doc Title"),
        Block(type="paragraph", text="Intro text under the doc title."),
        Block(type="heading", depth=2, text="Section A"),
        Block(type="paragraph", text="Body of section A."),
        Block(type="heading", depth=2, text="Section B"),
        Block(type="paragraph", text="Body of section B."),
    ]
    sections = chunk_blocks(blocks)
    # 3 sections: intro under Doc Title, then Section A, then Section B.
    # (H1 + first content packs as one; H2 closes and opens a new section.)
    assert len(sections) == 3
    crumbs = [s.breadcrumb for s in sections]
    assert crumbs[0] == "Doc Title"
    assert crumbs[1] == "Doc Title > Section A"
    assert crumbs[2] == "Doc Title > Section B"


@pytest.mark.unit
def test_chunk_blocks_h3_does_NOT_create_new_section():
    """H3 stays within the parent H2 section — only H1/H2 are section breaks.
    The breadcrumb still includes the H3 though."""
    blocks = [
        Block(type="heading", depth=2, text="Section A"),
        Block(type="paragraph", text="Body of section A."),
        Block(type="heading", depth=3, text="Subsection a1"),
        Block(type="paragraph", text="Body of subsection a1."),
    ]
    sections = chunk_blocks(blocks)
    assert len(sections) == 1
    assert sections[0].breadcrumb == "Section A > Subsection a1"


@pytest.mark.unit
def test_chunk_blocks_breadcrumb_pops_correctly_when_depth_decreases():
    """H2 should pop a previous H3 from the breadcrumb stack."""
    blocks = [
        Block(type="heading", depth=2, text="A"),
        Block(type="heading", depth=3, text="A1"),
        Block(type="paragraph", text="under A1"),
        Block(type="heading", depth=2, text="B"),
        Block(type="paragraph", text="under B"),
    ]
    sections = chunk_blocks(blocks)
    assert sections[-1].breadcrumb == "B", "A1 should NOT carry forward to section under B"


@pytest.mark.unit
def test_chunk_blocks_oversized_paragraph_split_by_sentences():
    """A single paragraph above the hard cap gets sentence-split via pysbd."""
    # Build a paragraph with many sentences such that the total exceeds hard_tokens.
    # Use a smaller hard cap to force the split path with reasonable test input.
    sentences = " ".join([f"Sentence number {i} contains some meaningful content here." for i in range(200)])
    blocks = [Block(type="paragraph", text=sentences)]
    sections = chunk_blocks(blocks, soft_tokens=200, hard_tokens=300)
    # Multiple sections — proves it sub-split
    assert len(sections) >= 2
    for s in sections:
        assert s.token_count <= SECTION_HARD_TOKENS  # very loose upper bound


@pytest.mark.unit
def test_chunk_blocks_oversized_code_block_emitted_atomic():
    """An oversized fenced code block stands alone — no sub-splitting."""
    big_code = "\n".join(f"line {i} = some code content here {i}" for i in range(500))
    blocks = [
        Block(type="paragraph", text="Intro paragraph before the code."),
        Block(type="code", text=big_code),
    ]
    sections = chunk_blocks(blocks, soft_tokens=200, hard_tokens=300)
    # The code stands as its own section, intact
    code_section = next((s for s in sections if "line 0" in s.text and "line 499" in s.text), None)
    assert code_section is not None
    assert code_section.block_types == ["code"]


@pytest.mark.unit
def test_chunk_blocks_oversized_table_emitted_atomic():
    """An oversized table stays whole, even if it bursts the cap."""
    big_table = "\n".join(f"row {i} | col_b {i}" for i in range(500))
    blocks = [Block(type="table", text=big_table)]
    sections = chunk_blocks(blocks, soft_tokens=200, hard_tokens=300)
    assert len(sections) == 1
    assert sections[0].block_types == ["table"]
    assert "row 0" in sections[0].text
    assert "row 499" in sections[0].text


@pytest.mark.unit
def test_chunk_blocks_empty_input_returns_empty():
    assert chunk_blocks([]) == []


# ---------------------------------------------------------------------------
# parse() dispatch
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_dispatches_to_markdown_when_headings_present():
    text = "# Title\n\nBody."
    blocks = parse(text)
    types = [b.type for b in blocks]
    assert "heading" in types


@pytest.mark.unit
def test_parse_dispatches_to_plaintext_when_no_markdown_signal():
    text = "Just a paragraph.\n\nAnd another one."
    blocks = parse(text)
    assert all(b.type == "paragraph" for b in blocks)
    assert len(blocks) == 2


# ---------------------------------------------------------------------------
# doc_token_count
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_doc_token_count_grows_with_input():
    short = doc_token_count("Hello world.")
    long = doc_token_count("Hello world. " * 100)
    assert short > 0
    assert long > short
