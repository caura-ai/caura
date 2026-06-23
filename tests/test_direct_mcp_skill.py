"""Invariants for the direct-MCP SKILL.md adapter.

The adapter at ``static/skills/memclaw/SKILL.md`` is served by
``/api/v1/skill/memclaw`` and installed into ``~/.claude/skills/memclaw/``
(Claude Code) or ``~/.agents/skills/memclaw/`` (Codex) by the
``/api/v1/install-skill`` bash installer.

It is *intentionally* maintained independently of the OpenClaw plugin's
SKILL.md. These tests pin the invariants specific to the direct-MCP
distribution: runtime-neutral frontmatter, no OpenClaw config gate, no
references to the plugin-runtime-generated TOOLS.md, the canonical section
structure, and the content value-adds this file carries.

Structure note: the adapter defers per-tool *signatures* to the live MCP tool
schemas (always in context when the tools are) and instead documents the
behaviors a parameter list can't express — so these tests assert tool *names*
and the "Behaviors the schema won't tell you" section, not signature cards.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ADAPTER_PATH = REPO_ROOT / "static" / "skills" / "memclaw" / "SKILL.md"

# Every tool the canonical adapter documents (direct-MCP exposes all 12,
# including keystones_set — which the OpenClaw plugin variant withholds).
ALL_TOOLS = (
    "memclaw_recall",
    "memclaw_write",
    "memclaw_manage",
    "memclaw_list",
    "memclaw_doc",
    "memclaw_entity_get",
    "memclaw_tune",
    "memclaw_insights",
    "memclaw_evolve",
    "memclaw_stats",
    "memclaw_keystones",
    "memclaw_keystones_set",
)


def _read_adapter() -> str:
    return ADAPTER_PATH.read_text(encoding="utf-8")


def test_file_exists_at_expected_path() -> None:
    assert ADAPTER_PATH.is_file(), f"expected direct-MCP adapter at {ADAPTER_PATH}"


def test_has_minimal_frontmatter() -> None:
    skill = _read_adapter()
    assert skill.startswith("---\n"), "missing YAML frontmatter delimiter"
    assert "\nname: memclaw\n" in skill, "missing/wrong 'name: memclaw'"
    assert "\ndescription:" in skill, "missing description field"
    assert "\nuser-invocable: false\n" in skill, (
        "adapter should set user-invocable: false to suppress /memclaw slash command"
    )


def test_has_no_openclaw_config_gate() -> None:
    """The plugin-enabled config gate is OpenClaw-specific; direct-MCP users
    don't have OpenClaw so the gate is meaningless and confusing."""
    skill = _read_adapter()
    assert "plugins.entries.memclaw.enabled" not in skill, (
        "adapter must not carry the OpenClaw plugin-enabled config gate"
    )
    # Frontmatter must not declare an openclaw metadata block. We check the
    # frontmatter specifically (not the whole file) because the footer
    # intentionally mentions OpenClaw when pointing users to the plugin copy.
    frontmatter = skill.split("---\n", 2)[1] if skill.count("---\n") >= 2 else ""
    assert "openclaw" not in frontmatter.lower(), (
        "frontmatter must not declare an openclaw metadata block"
    )


def test_has_no_tools_md_references() -> None:
    """TOOLS.md is generated at runtime by plugin/src/educate.ts for
    OpenClaw agent workspaces. Direct-MCP users don't have it, so any
    reference in the adapter is a dangling pointer."""
    skill = _read_adapter()
    assert "TOOLS.md" not in skill, (
        "TOOLS.md is an OpenClaw runtime artifact; references must be inlined "
        "in the direct-MCP adapter (e.g. status vocabulary listed directly)"
    )


def test_contains_required_body_sections() -> None:
    skill = _read_adapter()
    required = [
        "## 0 · Identity",
        "## 1 · Session start",
        "## 2 · The loop",
        "## 3 · How and when to write a memory",
        "## 5 · Two stores, one rule",
        "## 6 · Trust and sharing",
        "## Tool reference",
        "### Which tool, when",
        "### Behaviors the schema won't tell you",
        "### Anti-patterns",
        "### Constraints & errors",
    ]
    for heading in required:
        assert heading in skill, f"adapter missing section {heading!r}"


def test_documents_every_tool_by_name() -> None:
    """The adapter defers parameter signatures to the MCP schemas, but every
    tool must still be named (in 'Which tool, when' / 'Behaviors') so the
    model knows the surface exists. Presence by name, not by signature card."""
    skill = _read_adapter()
    for tool in ALL_TOOLS:
        assert tool in skill, f"adapter does not mention {tool}"


def test_does_not_reintroduce_signature_cards() -> None:
    """Regression guard for the schema-deferral decision: the adapter should
    NOT carry per-tool signature cards (``memclaw_x(arg, ...)``) that merely
    duplicate the always-in-context MCP tool schemas."""
    skill = _read_adapter()
    assert "`memclaw_recall(" not in skill and "`memclaw_write(" not in skill, (
        "adapter reintroduced signature cards; signatures live in the MCP "
        "tool schemas — keep the lean 'Behaviors the schema won't tell you' form"
    )


def test_contains_error_codes_verbatim() -> None:
    skill = _read_adapter()
    for code in ("INVALID_ARGUMENTS", "BATCH_TOO_LARGE", "INVALID_BATCH_ITEM"):
        assert code in skill, f"adapter missing error code {code}"


def test_footer_references_direct_mcp_install_targets() -> None:
    """The footer must tell users where the file lives on their machine
    after install. Without this, a user who finds the file on disk has no
    context for what it is or how to replace it."""
    skill = _read_adapter()
    assert "~/.claude/skills/memclaw" in skill, (
        "footer should mention the Claude Code install path"
    )
    assert "~/.agents/skills/memclaw" in skill, (
        "footer should mention the Codex install path"
    )


def test_contains_canonical_content_invariants() -> None:
    """Pins the load-bearing facts and value-adds of the canonical adapter.
    If you remove one, justify it; if you add a new must-keep fact, add it
    here so it can't silently fall back out.
    """
    skill = _read_adapter()
    must_contain = [
        # The loop + write discipline
        "most-binding-first",          # orient ordering
        "## 3 · How and when to write a memory",
        # Grounded accuracy facts (must not regress)
        "home fleet",                  # fleet_id resolves home fleet on omit (post-#465)
        "private by default",          # evolve failure -> private rule by default
        # Two-stores hybrid pattern
        "Cross-store discovery",       # pointer-memory workaround
        "op=list_collections",         # enumerate collections
        "op=search",                   # semantic search over docs
        'data["summary"]',             # opt-in semantic indexing on write
        "scalar exact-match only",     # memclaw_doc where-filter gotcha
        # Trust self-awareness
        "trust 1",                     # auto-register tier
    ]
    for phrase in must_contain:
        assert phrase in skill, f"adapter missing {phrase!r}"
