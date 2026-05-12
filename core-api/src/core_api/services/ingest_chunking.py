"""Block-based structure-aware chunker for ingest (Tier-1 PR #7 / A4).

The ingest path used to send the first 50k chars of any document as one
big blob to the LLM. This module replaces that with a structure-aware
pipeline:

  raw text  →  parse to typed ``Block`` list  →  greedy-pack into
  ``Section`` list (heading-bounded, token-capped)

Sections then go through the per-section LLM extraction one at a time
(or in parallel) with their breadcrumb context appended to the prompt.

Three input formats today (more via PR #8 Kreuzberg):
  - markdown  → ``parse_markdown`` via markdown-it-py AST walk
  - plain text → ``parse_plaintext`` via ``\\n\\n+`` paragraph split
  - HTML       → out of scope here; tag-stripped upstream in ``_fetch_url_text``
                 and treated as plain text by this module
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Literal

import tiktoken
from markdown_it import MarkdownIt

logger = logging.getLogger(__name__)

# Token budget per Section. Soft = the target the greedy packer aims for
# when joining adjacent blocks. Hard = the maximum a single Section is
# allowed to grow to; oversized blocks get sub-split rather than emitted
# as a giant section. Picks match the spec in the Tier-1 plan; tuned for
# gpt-5-class models with ~8k context comfort.
SECTION_SOFT_TOKENS = 2_000
SECTION_HARD_TOKENS = 3_000

# Doc-level refuse threshold. Inputs above this get a clean 413 from
# the caller (``ingest_preview``). Picks 100k tokens — roughly a small
# book — to avoid pathological ingests. Bump when real users want more.
DOC_HARD_TOKEN_LIMIT = 100_000

# Min word count for a paragraph block before we'll let it stand alone
# (otherwise we may merge it with neighbors). Two-word lines like "Yes"
# or "Note:" are noise.
_MIN_PARAGRAPH_WORDS = 3

BlockType = Literal["heading", "paragraph", "list", "code", "table", "blockquote"]


@dataclass
class Block:
    """A single typed unit from the parsed input.

    ``depth`` is meaningful only for ``heading`` blocks (1 = H1, 2 = H2, ...).
    For everything else it's 0.

    ``text`` is the rendered content of the block as plain text. For
    markdown headings we strip the leading ``#``s; for lists we render
    each item on its own line; for code blocks we keep the body verbatim.
    """

    type: BlockType
    text: str
    depth: int = 0


@dataclass
class Section:
    """A chunked unit produced by ``chunk_blocks`` — one LLM call per Section.

    ``breadcrumb`` is the heading trail at this Section's position,
    e.g. ``"Release Notes > v2.3 > Performance"``. Empty when the input
    had no headings (plain-text path).

    ``text`` is the joined plain-text content the LLM will see, NOT
    including the breadcrumb (caller stitches the breadcrumb in via the
    prompt as separate context).

    ``token_count`` is the tiktoken-counted size of ``text``. Useful for
    diagnostics and for the caller to refuse pathological docs early.
    """

    text: str
    breadcrumb: str = ""
    token_count: int = 0
    block_types: list[BlockType] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Token counting — shared singleton encoder (creating the encoder is ~50ms)
# ---------------------------------------------------------------------------


_ENCODER: tiktoken.Encoding | None = None


def _encoder() -> tiktoken.Encoding:
    global _ENCODER
    if _ENCODER is None:
        # cl100k_base matches GPT-4 family. Close enough for size budgeting
        # against newer models that use o200k_base — we're not optimizing
        # for byte-precision, just keeping sections under ~2-3k tokens.
        _ENCODER = tiktoken.get_encoding("cl100k_base")
    return _ENCODER


def _count_tokens(text: str) -> int:
    """tiktoken cl100k_base count. Used by chunk_blocks for size caps."""
    return len(_encoder().encode(text))


# ---------------------------------------------------------------------------
# Parsers — format → list[Block]
# ---------------------------------------------------------------------------


# ``gfm-like`` enables tables (the only GFM addition we care about) plus
# strikethrough and linkify. We disable linkify because it requires the
# separate ``linkify-it-py`` dependency and we don't need URL auto-
# detection for fact extraction.
_md = MarkdownIt("gfm-like", {"breaks": False, "html": False, "linkify": False})


def parse_markdown(text: str) -> list[Block]:
    """Walk a markdown-it AST and emit a typed Block list.

    Block types emitted:
      - ``heading``: depth = 1..6, text = the heading body
      - ``paragraph``: text = inline content
      - ``list``: bullet/ordered items joined as ``"- item\\n- item"``
      - ``code``: fenced or indented code blocks, body verbatim
      - ``table``: rendered as plain-text rows
      - ``blockquote``: text = inner content

    Heading depth is preserved so the chunker can use H1/H2 as section
    boundaries. Anything we don't recognize falls back to ``paragraph``.
    """
    tokens = _md.parse(text)
    blocks: list[Block] = []

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        ttype = tok.type

        if ttype == "heading_open":
            depth = int(tok.tag[1])  # tag is "h1", "h2", ...
            # heading_open → inline → heading_close
            inline = tokens[i + 1]
            body = (inline.content or "").strip()
            if body:
                blocks.append(Block(type="heading", text=body, depth=depth))
            i += 3
            continue

        if ttype == "paragraph_open":
            inline = tokens[i + 1]
            body = (inline.content or "").strip()
            if body:
                blocks.append(Block(type="paragraph", text=body))
            i += 3
            continue

        if ttype in ("bullet_list_open", "ordered_list_open"):
            # Walk until the matching list_close, collecting the inline
            # content of each list_item.
            close = ttype.replace("_open", "_close")
            depth_stack = 1
            items: list[str] = []
            j = i + 1
            while j < len(tokens) and depth_stack > 0:
                inner = tokens[j]
                if inner.type == ttype:
                    depth_stack += 1
                elif inner.type == close:
                    depth_stack -= 1
                    if depth_stack == 0:
                        break
                elif inner.type == "inline":
                    body = (inner.content or "").strip()
                    if body:
                        items.append(f"- {body}")
                j += 1
            if items:
                blocks.append(Block(type="list", text="\n".join(items)))
            i = j + 1
            continue

        if ttype == "fence" or ttype == "code_block":
            body = (tok.content or "").rstrip()
            if body:
                blocks.append(Block(type="code", text=body))
            i += 1
            continue

        if ttype == "table_open":
            # Walk to table_close, render each row as tab-separated text.
            rows: list[str] = []
            current_row: list[str] = []
            j = i + 1
            while j < len(tokens):
                inner = tokens[j]
                if inner.type == "table_close":
                    break
                if inner.type == "tr_open":
                    current_row = []
                elif inner.type == "tr_close":
                    if current_row:
                        rows.append(" | ".join(current_row))
                elif inner.type == "inline":
                    current_row.append((inner.content or "").strip())
                j += 1
            if rows:
                blocks.append(Block(type="table", text="\n".join(rows)))
            i = j + 1
            continue

        if ttype == "blockquote_open":
            close = "blockquote_close"
            depth_stack = 1
            parts: list[str] = []
            j = i + 1
            while j < len(tokens) and depth_stack > 0:
                inner = tokens[j]
                if inner.type == "blockquote_open":
                    depth_stack += 1
                elif inner.type == close:
                    depth_stack -= 1
                    if depth_stack == 0:
                        break
                elif inner.type == "inline":
                    parts.append((inner.content or "").strip())
                j += 1
            if parts:
                blocks.append(Block(type="blockquote", text="\n".join(parts)))
            i = j + 1
            continue

        # Skip thematic breaks / opens we already handled / anything else.
        i += 1

    return blocks


_PARAGRAPH_SPLIT_RE = re.compile(r"\n{2,}")


def parse_plaintext(text: str) -> list[Block]:
    """Treat the input as paragraphs separated by blank lines.

    No headings detected; the chunker will pack purely by token budget.
    Very short paragraphs (< _MIN_PARAGRAPH_WORDS) are kept (they may be
    significant signal — section titles, dates) but the chunker is free
    to merge them with neighbors.
    """
    blocks: list[Block] = []
    for chunk in _PARAGRAPH_SPLIT_RE.split(text):
        body = chunk.strip()
        if body:
            blocks.append(Block(type="paragraph", text=body))
    return blocks


def detect_format(text: str) -> Literal["markdown", "plaintext"]:
    """Heuristic: presence of markdown headings or fenced code blocks?

    Used by ``ingest_preview`` when the caller didn't specify a format.
    The current ingest endpoint accepts text either via pasted ``content``
    or via URL fetch — neither carries explicit format info. The heuristic
    is intentionally conservative: only call it markdown if we see
    at-least-one ATX heading (``# ``, ``## ``, ...) or a fenced code
    block (``` ``` ```). Everything else → plaintext.
    """
    # ATX headings: line starts with 1-6 ``#`` followed by space + text
    if re.search(r"(?m)^#{1,6}\s+\S", text):
        return "markdown"
    # Fenced code blocks
    if re.search(r"(?m)^```[a-zA-Z0-9_-]*\s*$", text):
        return "markdown"
    return "plaintext"


def parse(text: str) -> list[Block]:
    """Dispatch helper — format detection + parse in one call."""
    fmt = detect_format(text)
    if fmt == "markdown":
        return parse_markdown(text)
    return parse_plaintext(text)


# ---------------------------------------------------------------------------
# Chunker — list[Block] → list[Section]
# ---------------------------------------------------------------------------


def _split_oversized_paragraph(text: str, hard_tokens: int) -> list[str]:
    """Last-resort sub-split for a single paragraph that exceeds the hard cap.

    Uses pysbd for sentence boundaries (handles abbreviations correctly).
    Falls back to a naive `. ` split if pysbd raises.

    Returns one or more substrings, each under ``hard_tokens`` tokens.
    """
    try:
        # Import inside the function — pysbd warmup is non-trivial and
        # most paragraphs DON'T need this path.
        import pysbd  # type: ignore[import-untyped]

        seg = pysbd.Segmenter(language="en", clean=False)
        sents = [s.strip() for s in seg.segment(text) if s.strip()]
    except Exception:
        logger.warning("pysbd failed; falling back to naive sentence split", exc_info=True)
        sents = [s.strip() + "." for s in text.split(". ") if s.strip()]

    out: list[str] = []
    current = ""
    current_tokens = 0
    for sent in sents:
        sent_tokens = _count_tokens(sent)
        if current_tokens + sent_tokens > hard_tokens and current:
            out.append(current.strip())
            current = sent
            current_tokens = sent_tokens
        else:
            current = f"{current} {sent}".strip() if current else sent
            current_tokens += sent_tokens
    if current:
        out.append(current.strip())
    return out


def chunk_blocks(
    blocks: list[Block],
    *,
    soft_tokens: int = SECTION_SOFT_TOKENS,
    hard_tokens: int = SECTION_HARD_TOKENS,
) -> list[Section]:
    """Greedy-pack typed blocks into Sections under token caps.

    Section boundary triggers (in priority order):
      1. A heading at depth ≤ 2 (H1/H2) AND the current accumulator is non-empty.
         → close current section, start a new one with this heading.
      2. Adding the next block would exceed ``soft_tokens``.
         → close current section, the next block starts a new one.

    Lists, tables, and code blocks are never split mid-element. If a
    single such block exceeds ``hard_tokens`` on its own, it stands as
    its own oversized section (the LLM handles oversized inputs better
    than malformed half-tables).

    Paragraphs that exceed ``hard_tokens`` alone get sentence-split via
    pysbd into multiple sub-paragraphs, each its own paragraph block in
    a new section.

    The breadcrumb on each emitted Section reflects the most recent H1
    and H2 (and H3 if shallow enough) encountered before that section.
    Without headings (plain-text input), breadcrumb is "".
    """
    sections: list[Section] = []
    current_text_parts: list[str] = []
    current_tokens = 0
    current_types: list[BlockType] = []
    # Breadcrumb tracking: stack of (depth, text) for the current heading trail
    breadcrumb_stack: list[tuple[int, str]] = []

    def _current_breadcrumb() -> str:
        return " > ".join(text for _, text in breadcrumb_stack)

    def _flush() -> None:
        nonlocal current_text_parts, current_tokens, current_types
        if not current_text_parts:
            return
        joined = "\n\n".join(current_text_parts).strip()
        if joined:
            sections.append(
                Section(
                    text=joined,
                    breadcrumb=_current_breadcrumb(),
                    token_count=current_tokens,
                    block_types=list(current_types),
                )
            )
        current_text_parts = []
        current_tokens = 0
        current_types = []

    for block in blocks:
        block_tokens = _count_tokens(block.text)

        if block.type == "heading":
            # An H1/H2 closes the current section if non-empty. Flush
            # BEFORE updating the heading stack so the prior section's
            # breadcrumb reflects the OLD heading trail (the new heading
            # belongs to the next section, not the one we're closing).
            if block.depth <= 2 and current_text_parts:
                _flush()
            # Update breadcrumb stack: pop deeper-or-equal entries, push this one.
            while breadcrumb_stack and breadcrumb_stack[-1][0] >= block.depth:
                breadcrumb_stack.pop()
            breadcrumb_stack.append((block.depth, block.text))
            # The heading text itself goes into the next section as a
            # rendered line (LLM benefits from seeing the heading).
            current_text_parts.append(("#" * block.depth) + " " + block.text)
            current_tokens += block_tokens
            current_types.append(block.type)
            continue

        # Oversized atomic blocks (list/table/code) — emit as own section
        # without splitting.
        if block.type in ("list", "table", "code") and block_tokens > hard_tokens:
            _flush()
            sections.append(
                Section(
                    text=block.text,
                    breadcrumb=_current_breadcrumb(),
                    token_count=block_tokens,
                    block_types=[block.type],
                )
            )
            continue

        # Oversized paragraph — sentence-split into multiple sub-paragraphs.
        if block.type == "paragraph" and block_tokens > hard_tokens:
            _flush()
            for sub in _split_oversized_paragraph(block.text, hard_tokens):
                sub_tokens = _count_tokens(sub)
                sections.append(
                    Section(
                        text=sub,
                        breadcrumb=_current_breadcrumb(),
                        token_count=sub_tokens,
                        block_types=["paragraph"],
                    )
                )
            continue

        # Would adding this block exceed the soft cap? Close current first.
        if current_tokens + block_tokens > soft_tokens and current_text_parts:
            _flush()

        current_text_parts.append(block.text)
        current_tokens += block_tokens
        current_types.append(block.type)

    _flush()
    return sections


def doc_token_count(text: str) -> int:
    """Lightweight total-token estimate for the doc-refuse gate.

    Used by ``ingest_preview`` to refuse pathological inputs with 413
    before any parsing work.
    """
    return _count_tokens(text)
