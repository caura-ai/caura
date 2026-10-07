"""The docs say what the code does (the 2026-10-01 audit's docs batch).

Each test reads one claim a doc makes and checks it against the code or the
config it describes, so the two cannot drift apart unnoticed again. Test names
carry the audit finding they close.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from common.enrichment.constants import (
    CLASSIFIER_DEPRECATED_MEMORY_TYPES,
    MEMORY_TYPES,
    SERVER_RESERVED_MEMORY_TYPES,
)
from common.llm._credentials import _TENANT_KEY_ATTR
from core_api.heartbeat.payload import normalise_version
from core_api.schemas import SearchResponse
from core_api.services.organization_settings import DEFAULT_SETTINGS

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
GUIDE = REPO / "static/docs/integration-guide.md"
MIGRATIONS = REPO / "core-storage-api/src/core_storage_api/database/migrations"
KEYSTONE_SET = "caura_keystones_set op=set"
SKILLS = [
    path
    for path in sorted(REPO.glob("static/skills/*/SKILL.md"))
    if KEYSTONE_SET in path.read_text()
]


def _read(path: str) -> str:
    return (REPO / path).read_text()


def _section(text: str, heading: str) -> str:
    """From ``heading`` to the next heading of the same or a higher level.

    A line in a fenced code block is not a heading: ``# 1. Clone`` is a comment.
    """
    lines = text[text.index(heading) :].splitlines()
    level = len(heading.split(" ", 1)[0])
    fenced = False
    for number, line in enumerate(lines[1:], 1):
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and re.match(rf"#{{1,{level}}} ", line):
            return "\n".join(lines[:number])
    return "\n".join(lines)


def _line(text: str, needle: str) -> str:
    return next(line for line in text.splitlines() if needle in line)


def test_the_docs_and_skills_are_found() -> None:
    assert GUIDE.exists()
    assert SKILLS


def test_l81_env_example_names_real_flags_and_the_1024_dim_migration() -> None:
    env = _read(".env.example")
    assert "--no-pull" not in env
    revision = re.search(r"1024-dim \(alembic (\d+)\)", env)
    assert revision
    names = [path.name for path in MIGRATIONS.glob(f"versions/{revision[1]}_*.py")]
    assert len(names) == 1
    assert "1024" in names[0]


def test_l82_every_tenant_llm_provider_is_documented() -> None:
    env = _read(".env.example")
    matrix = _read("docs/self-hosting.md")
    assert [p for p in _TENANT_KEY_ATTR if f"{p.upper()}_API_KEY=" not in env] == []
    assert [p for p in _TENANT_KEY_ATTR if p not in matrix] == []


def test_l83_db_pool_knobs_say_where_compose_takes_them() -> None:
    services = yaml.safe_load(_read("docker-compose.yml"))["services"]
    storage = services["core-storage-api"]
    passed = "env_file" in storage or "DB_POOL_SIZE" in str(storage["environment"])
    env = _read(".env.example")
    block = env[: env.index("# DB_POOL_SIZE=")].rsplit("\n\n", 1)[-1]
    assert passed or "docker-compose.yml" in block


def test_l87_manual_install_expects_redis_as_its_env_configures_it() -> None:
    section = _section(_read("AGENT-INSTALL.md"), "## Option B")
    heredoc = section.split("cat > .env << 'EOF'", 1)[1].split("\nEOF", 1)[0]
    expected = re.search(r"# Expected: (\{.*\})", section)
    assert expected
    redis = json.loads(expected[1])["redis"]
    assert redis == ("connected" if "REDIS_URL" in heredoc else "not configured")


def test_l88_plugin_agent_id_goes_where_the_plugin_reads_it() -> None:
    section = _section(_read("AGENT-INSTALL.md"), "## Connect via OpenClaw Plugin")
    script = _read("core-api/src/core_api/routes/plugin.py")
    marker = 'cat > "$PLUGIN_DIR/.env" << ENV_EOF'
    env_file = script.split(marker, 1)[1].split("ENV_EOF", 1)[0]
    if "CAURA_AGENT_ID" in env_file:
        return
    before_install = section.split("install-plugin", 1)[0]
    assert not re.search(r"^CAURA_AGENT_ID=", before_install, flags=re.MULTILINE)
    assert "CAURA_AGENT_ID=" in section


def test_l89_agent_install_gives_caura_list_the_tool_trust_ladder() -> None:
    tool = _read("core-api/src/core_api/tools/caura_list.py")
    assert "OWN fleet at trust ≥ 1" in tool
    row = _line(_read("AGENT-INSTALL.md"), "| `caura_list` |")
    assert "own fleet" in row
    assert "`scope=fleet`/`all` trust ≥ 2" not in row


def test_l91_readme_counts_the_types_the_classifier_assigns() -> None:
    never_assigned = SERVER_RESERVED_MEMORY_TYPES | CLASSIFIER_DEPRECATED_MEMORY_TYPES
    stated = re.search(r"one of (\d+) memory types", _read("README.md"))
    assert stated
    assert int(stated[1]) == len(set(MEMORY_TYPES) - never_assigned)


def test_l92_security_policy_covers_each_component_major() -> None:
    manifest = json.loads(_read(".release-please-manifest.json"))
    majors = {version.split(".")[0] for version in manifest.values()}
    table = _section(_read("SECURITY.md"), "## Supported Versions")
    named = set(re.findall(r"(\d+)\.x", table))
    assert not named or named == majors


def test_l100_heartbeat_version_fits_the_schema() -> None:
    schema = json.loads(_read("docs/telemetry-schema-v1.json"))
    cap = schema["properties"]["version"]["maxLength"]
    assert normalise_version("v3.17.0") == "3.17.0"
    assert len(normalise_version("1." + "9" * cap)) <= cap


def test_l100_telemetry_doc_names_the_env_value_it_sends() -> None:
    assert "CAURA_VERSION" in _section(_read("docs/telemetry.md"), "### Never sent")


def test_l151_docs_name_the_served_openapi_path() -> None:
    app = _read("core-api/src/core_api/app.py")
    served = re.search(r'openapi_url="([^"]+)"', app)
    assert served
    docs = [REPO / "README.md", *sorted((REPO / "docs").glob("*.md"))]
    pattern = re.compile(r"`([^`\s]*openapi\.json)`")
    named = {path for doc in docs for path in pattern.findall(doc.read_text())}
    assert named
    assert [path for path in named if not path.endswith(served[1])] == []


def test_l153_local_embedder_check_reads_a_search_response_field() -> None:
    text = _read("docs/local-embedder.md")
    field = re.search(r"non-empty `(\w+)`\s+array in the search response", text)
    assert field
    assert field[1] in SearchResponse.model_fields


def test_l154_skill_delivery_has_one_query_row_and_it_matches_the_handler() -> None:
    lines = _read("docs/mcp-skill-delivery.md").splitlines()
    rows = [line for line in lines if line.startswith("| `query` |")]
    assert len(rows) == 1
    assert "INVALID_ARGUMENTS" in rows[0]


def test_l155_forge_cron_doc_reads_only_what_the_audit_row_stores() -> None:
    audit = _read("core-api/src/core_api/services/lifecycle_audit.py")
    assert "stats.candidates_produced" in audit
    text = _read("docs/operator-forge-cron.md")
    assert set(re.findall(r"stats\.(\w+)", text)) <= {"candidates_produced"}
    assert "stats={" not in text


def test_l156_plugin_upgrade_table_matches_the_manifest_deploy() -> None:
    text = _read("docs/plugin-upgrade.md")
    cells = [line.split("|") for line in text.splitlines() if line.startswith("| `")]
    auto = {row[1].strip(): row[2].strip() for row in cells if len(row) > 3}
    assert "merge" not in auto["`.env` (`CAURA_API_KEY` etc.)"].lower()
    assert auto["`node_modules/`"] != "Replaced"
    assert "merges any new keys" not in text


def test_l157_agent_key_provisioning_is_marked_managed_only() -> None:
    from core_api.app import app

    assert [r for r in app.routes if "agent-keys" in getattr(r, "path", "")] == []
    docs = (
        "README.md",
        "docs/public-api-stability.md",
        "docs/integration-without-plugin.md",
    )
    lines = [
        line
        for doc in docs
        for line in _read(doc).splitlines()
        if "agent-keys/provision" in line
    ]
    assert lines
    unmarked = [
        line for line in lines if "caura.ai" not in line and "managed" not in line
    ]
    assert unmarked == []


def test_l158_air_gapped_hosts_are_told_how_to_bring_images_in() -> None:
    text = _read("docs/self-hosting.md")
    section = _section(text, "### Offline and air-gapped operation")
    assert "docker save" in section
    assert "docker load" in section


def test_l167_cursor_is_told_the_transport_mcp_serves() -> None:
    assert "streamable_http_app(" in _read("core-api/src/core_api/mcp_server.py")
    row = _line(GUIDE.read_text(), "| Cursor |")
    assert "sse" not in row


def test_l168_guide_says_auto_chunking_is_off_by_default() -> None:
    assert not DEFAULT_SETTINGS["chunking"]["auto_chunk_enabled"]
    section = _section(GUIDE.read_text(), "### Auto-chunking")
    assert "off by default" in section
    assert "Disable per tenant" not in section


def test_l169_guide_scopes_exact_dedup_as_the_write_path_does() -> None:
    step = "core-api/src/core_api/pipeline/steps/write/check_exact_duplicate.py"
    assert "agent_id=data.agent_id" in _read(step)
    section = _section(GUIDE.read_text(), "### Deduplication")
    assert "tenant + fleet + agent" in section


@pytest.mark.parametrize("skill", SKILLS, ids=lambda path: path.parent.name)
def test_l170_keystone_examples_pass_the_required_doc_id(skill: Path) -> None:
    example = rf"{KEYSTONE_SET}(?:[^\n]*\\\n)*[^\n]*"
    examples = re.findall(example, skill.read_text())
    assert examples
    assert [example for example in examples if "doc_id=" not in example] == []


def test_l230_readme_states_the_digest_cadence_and_its_default() -> None:
    bullet = _line(_read("README.md"), "**Agent activity digests**")
    assert "agent_digest.cadence" in bullet
    assert DEFAULT_SETTINGS["agent_digest"]["cadence"] in bullet
