"""Invariants for the direct-MCP SKILL.md adapter.

The adapter at ``static/skills/memclaw/SKILL.md`` is served by
``/api/v1/skill/memclaw`` and installed into ``~/.claude/skills/memclaw/``
(Claude Code) or ``~/.agents/skills/memclaw/`` (Codex) by the
``/api/v1/install-skill`` bash installer.

It is *intentionally* maintained independently of the OpenClaw plugin's
SKILL.md. These tests pin the invariants specific to the direct-MCP
distribution: runtime-neutral frontmatter, no OpenClaw config gate, no
references to the plugin-runtime-generated TOOLS.md, and the four
content additions this file carries.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ADAPTER_PATH = REPO_ROOT / "static" / "skills" / "memclaw" / "SKILL.md"


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
        "## Your identity",
        "## The three rules",
        "## Trust levels",
        "## Sharing",
        "## Containers",
        "## Session loop",
        "## Tool reference",
        "### Tool cards",
        "### Which tool, when",
        "### Constraints that matter",
        "### Error codes",
    ]
    for heading in required:
        assert heading in skill, f"adapter missing section {heading!r}"


def test_contains_all_nine_tool_cards() -> None:
    skill = _read_adapter()
    for tool in (
        "memclaw_recall",
        "memclaw_write",
        "memclaw_manage",
        "memclaw_list",
        "memclaw_doc",
        "memclaw_entity_get",
        "memclaw_tune",
        "memclaw_insights",
        "memclaw_evolve",
    ):
        # Accept both **`tool(` (bolded) and `tool(` (plain) to tolerate
        # formatting drift; the test's purpose is presence, not style.
        assert f"`{tool}(" in skill, f"adapter missing tool card for {tool}"


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


def test_contains_direct_mcp_specific_content_additions() -> None:
    """Pins the value-adds this adapter carries beyond the canonical plugin
    SKILL.md. These landed from lived usage — hybrid-pattern,
    collection-listing convention, where-filter scalar gotcha, anti-patterns
    subsection. If you remove one, justify it; if you add a new one, add it
    here so it can't silently fall back out.
    """
    skill = _read_adapter()
    must_contain = [
        # Foundation (present in canonical too; guards against regression)
        "Rule 1 — Recall before you start",
        "Rule 2 — Write when something matters",
        "Rule 3 — Supersede, don't delete",
        "Auto-registered at trust 1 on your first write",
        # Direct-MCP-specific additions
        "Cross-store discovery",  # hybrid-pattern for doc discoverability
        'op="list_collections"',  # real op for enumerating collections
        'op="search"',  # semantic search over docs
        'data["summary"]',  # opt-in semantic indexing on write
        "scalar exact-match only",  # memclaw_doc where-filter gotcha
        "### Anti-patterns",  # explicit don't-do-this subsection
    ]
    for phrase in must_contain:
        assert phrase in skill, f"adapter missing {phrase!r}"
