"""Forge distill — prompt template + response parser (SF-104 part 1).

Pure functions only. No LLM client, no DB, no I/O. The Forge
service in :mod:`forge_service` injects an ``llm_fn`` callable that
the prompt feeds and the parser consumes; this module just
formats inputs → prompt string and parses the structured LLM
output → typed dict.

Why a separate module: prompt drift (the prompt itself, the
response schema, the parser robustness rules) is the dominant
source of Phase-1 quality bugs. Pulling the prompt out of the
orchestrator gives the prompt its own test surface, its own
golden-input regression suite, and a stable place to A/B
variants when SF-105's eval harness measures precision/recall.

Output schema (the dict the LLM is asked to produce — also the
shape :func:`parse_distill_response` returns):

    {
        # Fingerprint inputs (consumed by SF-103).
        "goal_phrase":     "<6-ish-word what-is-this-cluster-about>",
        "domain":          "<dev|security|marketing|...>",
        "step_skeleton":   ["<verb+object>", ...],   # ordered procedure
        # Skill content (consumed by the upsert into `skills`).
        "name":            "<Display Name>",
        "slug":            "<filesystem-safe-slug>",
        "description":     "<<=160-byte trigger sentence>",
        "summary":         "<2-3 sentence detailed summary>",
        "content":         "<full SKILL.md body, markdown>",
        "tags":            ["<tag>", ...],
        "evidence":        "<2-3 sentence human-readable rationale>",
        "goal":            "<single sentence: the task this skill addresses>",
    }
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# Schema version stamped into the prompt so a future schema-rev can
# be detected at parse time. Bump this when the response schema
# changes; the parser checks ``data.get("schema_version")`` and
# rejects unknown versions.
DISTILL_SCHEMA_VERSION: str = "v1"

# Max characters of trace content fed into the prompt. Per-trace,
# multi-trace clusters concatenate but cap so the prompt stays
# within model context windows. The eval harness (SF-105) tunes
# this knob if precision drops.
MAX_TRACE_CONTENT_CHARS: int = 1_200

# Required keys in the parsed response (we 422 on any missing).
# Mirrors the ``data.slug`` regex enforced by the SF-002 validator in
# ``skill_lifecycle.py``. Kept here for compile-time inspection by the
# distill response parser so a malformed LLM slug (uppercase letters,
# spaces, leading punctuation) gets a clear DistillParseError instead
# of bubbling through to the storage layer as a generic 422.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")


REQUIRED_OUTPUT_KEYS: frozenset[str] = frozenset(
    {
        "goal_phrase",
        "domain",
        "step_skeleton",
        "name",
        "slug",
        "description",
        "summary",
        "content",
        "tags",
        "evidence",
        "goal",
    }
)


# ── Inputs to the prompt ──────────────────────────────────────────


@dataclass(frozen=True)
class TraceSnapshot:
    """One trace's contribution to the cluster prompt.

    Carries only what the LLM needs — not the full memory blobs,
    not the embeddings. ``memory_excerpts`` is the ordered list of
    short content samples (head + tail of each member memory,
    truncated to a per-cluster char budget).
    """

    run_id: str
    agent_id: str
    outcome_label: str  # success | failure | unknown
    memory_excerpts: list[str]
    entity_ids: list[str]
    started_at_iso: str
    ended_at_iso: str


@dataclass(frozen=True)
class ClusterPromptInputs:
    """Aggregated cluster shape the prompt is built from."""

    tenant_id: str
    fleet_id: str | None
    traces: list[TraceSnapshot]
    # Most-common entity IDs (UUIDs) across the cluster — pre-computed
    # by the caller. Renamed from ``top_entity_labels`` since the
    # values are storage UUIDs, not human-readable labels; the prompt
    # surfaces them as identifiers (the LLM doesn't try to interpret
    # them as English nouns).
    top_entity_ids: list[str] = field(default_factory=list)
    # Caller may pre-bias the domain (e.g. "all traces in this
    # cluster came from agents tagged with domain X"). The LLM is
    # told to confirm or correct. Optional.
    hint_domain: str | None = None


# ── Prompt construction ───────────────────────────────────────────


_SYSTEM_PREAMBLE = """\
You are Forge, an autonomous resident inside MemClaw's "live memory".
Your job: turn a cluster of agent session-traces — all of which
followed approximately the same procedure to the same outcome —
into a single reusable SKILL candidate.

Constraints:
  * Output strict JSON only. No prose outside the JSON. No code fence.
  * The fingerprint fields (goal_phrase, domain, step_skeleton) drive
    cluster identity. Be precise; do not vary surface wording across
    re-runs of the same cluster.
  * The trigger fields (name, description, summary) drive when an
    agent later USES this skill. Write them as if a stranger has
    to decide whether to fire this skill from a one-line query.
  * The description MUST be <= 160 BYTES (UTF-8). Short, focused, "use
    when ..." style.
  * slug: lowercase-kebab-case only. Regex: [a-z0-9][a-z0-9._-]{{0,99}}.
    The slug becomes part of the doc_id (forge/<slug>) and a
    filesystem path on plugin nodes — uppercase letters, spaces, or
    leading punctuation will be rejected by the route validator.
  * Output `schema_version` exactly: "{schema_version}".
  * Output `kind`: "create".
  * step_skeleton: 3-7 items. Each item: 2-4 words. Verb-then-object.

If the cluster's procedure cannot be cleanly distilled (mixed
outcomes, divergent step orders, too few traces), still produce a
valid candidate but set goal_phrase="" and step_skeleton=[]. The
auto-gates downstream will reject those.
"""

# Filled at prompt-build time — saves a .format() call per
# invocation.
_SYSTEM_PREAMBLE_FILLED = _SYSTEM_PREAMBLE.format(schema_version=DISTILL_SCHEMA_VERSION)


def build_distill_prompt(inputs: ClusterPromptInputs) -> str:
    """Render the cluster into the LLM-facing prompt string.

    Deterministic given the same inputs. The system preamble is
    constant; the user section interpolates the cluster snapshot.
    """
    lines: list[str] = [_SYSTEM_PREAMBLE_FILLED, ""]

    if inputs.hint_domain:
        lines.append(f"Suggested domain (confirm or correct): {inputs.hint_domain}")
    if inputs.top_entity_ids:
        # Label the section explicitly as UUIDs so the LLM understands
        # they're opaque storage identifiers rather than topic labels.
        lines.append(
            "Top entity IDs (UUIDs) across the cluster: " + ", ".join(sorted(set(inputs.top_entity_ids)))
        )
    lines.append("")
    lines.append(
        f"Cluster: {len(inputs.traces)} trace(s) from tenant={inputs.tenant_id} "
        f"fleet={inputs.fleet_id or '<none>'}."
    )
    lines.append("")

    # Per-trace block.
    for i, t in enumerate(inputs.traces):
        lines.append(f"--- Trace {i + 1}/{len(inputs.traces)} ---")
        lines.append(f"agent: {t.agent_id}")
        lines.append(f"run_id: {t.run_id}")
        lines.append(f"window: {t.started_at_iso} → {t.ended_at_iso}")
        lines.append(f"outcome: {t.outcome_label}")
        if t.entity_ids:
            # Sorted for prompt-stability (same cluster → same prompt).
            lines.append("entities: " + ", ".join(sorted(t.entity_ids)))
        if t.memory_excerpts:
            lines.append("memories:")
            budget = MAX_TRACE_CONTENT_CHARS
            for excerpt in t.memory_excerpts:
                if budget <= 0:
                    break
                snippet = excerpt[:budget]
                budget -= len(snippet)
                lines.append(f"  - {snippet}")
        lines.append("")

    lines.append(
        f"Respond with one JSON object matching schema_version='{DISTILL_SCHEMA_VERSION}'. "
        f"Required keys: {sorted(REQUIRED_OUTPUT_KEYS)} + 'schema_version' + 'kind'."
    )
    return "\n".join(lines)


# ── Response parsing ──────────────────────────────────────────────


class DistillParseError(ValueError):
    """Raised when the LLM response doesn't match the expected
    schema. The Forge service catches this and skips the cluster
    (log + audit) rather than aborting the whole run."""


# Single pattern LLMs leak despite "no code fence" instructions.
# Leading + trailing prose is handled via JSONDecoder.raw_decode in
# parse_distill_response itself (more robust than a regex —
# regex-based JSON extraction trips on nested braces inside strings).
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_distill_response(raw: str) -> dict[str, Any]:
    """Strict-ish parser. Tries the raw input as JSON; on failure
    strips code-fence wrappers and retries; on second failure scans
    for the first ``{`` and lets :class:`json.JSONDecoder` consume
    one object, ignoring any trailing prose.

    Returns the parsed dict; the caller (Forge service) is
    responsible for further validation against the skills schema.
    """
    if not raw or not raw.strip():
        raise DistillParseError("LLM response was empty.")

    candidate = raw.strip()

    # First attempt: as-is.
    parsed: dict[str, Any] | None = None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # Second attempt: strip code fence.
    if parsed is None:
        stripped = _CODE_FENCE_RE.sub("", candidate).strip()
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            pass

    # Third attempt: extract a single JSON object from the response
    # starting at the first ``{``. Uses ``JSONDecoder.raw_decode``
    # which consumes one well-formed object and reports where it
    # stopped — handles BOTH leading prose ("Here's the JSON: {...}")
    # AND trailing prose ("{...}\n\nLet me know if you want changes")
    # in a single pass. Beats the prior regex (``^[^{]*({.*})\s*$``)
    # because regex JSON-extraction misbehaves when the LLM's
    # output contains string-literal braces or whitespace tails the
    # `$` anchor doesn't match.
    if parsed is None:
        start = candidate.find("{")
        if start != -1:
            try:
                parsed, _ = json.JSONDecoder().raw_decode(candidate[start:])
            except json.JSONDecodeError:
                pass

    if parsed is None:
        raise DistillParseError(
            f"LLM response was not parseable as JSON after fence/prose stripping. Head: {raw[:200]!r}"
        )

    if not isinstance(parsed, dict):
        raise DistillParseError(f"LLM response parsed to {type(parsed).__name__}, expected JSON object.")

    # Schema version gate.
    if parsed.get("schema_version") != DISTILL_SCHEMA_VERSION:
        raise DistillParseError(
            f"LLM response schema_version was {parsed.get('schema_version')!r}; "
            f"expected {DISTILL_SCHEMA_VERSION!r}. This usually means the prompt "
            "was changed but the parser wasn't — bump DISTILL_SCHEMA_VERSION."
        )

    # Required keys.
    missing = REQUIRED_OUTPUT_KEYS - set(parsed)
    if missing:
        raise DistillParseError(
            f"LLM response missing required keys: {sorted(missing)}. Got: {sorted(parsed)}."
        )

    # Light typing checks. Catches the most common LLM mistakes
    # (string-for-list, list-for-string) without re-implementing
    # the SF-002 schema validator.
    for k in (
        "goal_phrase",
        "domain",
        "name",
        "slug",
        "description",
        "summary",
        "content",
        "evidence",
        "goal",
    ):
        if not isinstance(parsed[k], str):
            raise DistillParseError(
                f"LLM response key {k!r} must be a string, got {type(parsed[k]).__name__}."
            )
    for k in ("step_skeleton", "tags"):
        v = parsed[k]
        if not isinstance(v, list) or not all(isinstance(item, str) for item in v):
            raise DistillParseError(f"LLM response key {k!r} must be a list of strings.")

    # Slug format check — the slug becomes part of the doc_id
    # (``forge/<slug>``) and a filesystem path on plugin nodes, so it
    # must match the same regex the route validator enforces on
    # ``data.slug``. Catching this here gives a clear error pointing
    # at the model's output instead of a generic 422 from the storage
    # layer when the candidate_writer tries to upsert.
    if not _SLUG_RE.fullmatch(parsed["slug"]):
        raise DistillParseError(
            f"LLM response slug {parsed['slug']!r} does not match required "
            f"format {_SLUG_RE.pattern!r}. Instruct the model to use "
            "lowercase-kebab-case."
        )

    # `kind` may be present even if not in REQUIRED_OUTPUT_KEYS —
    # we accept both presence and explicit "create" / "update".
    kind = parsed.get("kind", "create")
    if kind not in ("create", "update"):
        raise DistillParseError(f"LLM response kind must be 'create' or 'update', got {kind!r}.")
    parsed["kind"] = kind

    return parsed
