"""caura-interviewer — the Claude Code disk-parser adapter (Interviewer Phase 2).

Reads Claude Code session transcripts (``~/.claude/projects/<slug>/<uuid>.jsonl``)
READ-ONLY, tracks per-file cursors via the server's forward-only watermark
documents, and submits event windows to ``POST /api/v1/interview/submit``.
No local state; no server changes; the trail belongs to Claude Code and is
never modified.
"""

from __future__ import annotations
