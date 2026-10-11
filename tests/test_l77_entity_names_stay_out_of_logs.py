"""Entity names stay out of INFO-and-above logs (L-77).

An entity's canonical name is tenant content: people, customers, projects. Log
sinks have no tenant access control and outlive a tenant purge, so the lines
that fire on routine entity races and matches name the entity by id and type
instead. DEBUG may still carry a name, as query text does.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
_MODULES = (
    "core-storage-api/src/core_storage_api/services/postgres_service.py",
    "core-api/src/core_api/services/entity_service.py",
    "core-api/src/core_api/services/entity_extraction_worker.py",
    "core-api/src/core_api/pipeline/steps/entity_linking/backfill_entity_embeddings.py",
)
_LOUD = frozenset({"info", "warning", "error", "exception", "critical"})
# How these modules hold an entity's name: the field itself, and the local the
# extraction worker resolves it into.
_NAME_HOLDERS = frozenset({"name", "existing_name"})


def _carries_a_name(arg: ast.expr) -> bool:
    return "canonical_name" in ast.unparse(arg) or (
        isinstance(arg, ast.Name) and arg.id in _NAME_HOLDERS
    )


def _loud_lines_with_names(path: Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text())):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _LOUD
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "logger"
        ):
            continue
        args = [*node.args[1:], *(kw.value for kw in node.keywords)]
        if any(_carries_a_name(arg) for arg in args):
            found.append(f"{path.name}:{node.lineno}")
    return found


def test_no_info_or_louder_line_logs_an_entity_name() -> None:
    offenders = [
        line for module in _MODULES for line in _loud_lines_with_names(_ROOT / module)
    ]
    assert not offenders, (
        f"these log lines copy an entity's name into the log sink: {offenders}. "
        "Log its id, type and tenant instead."
    )


def test_the_scan_sees_a_named_line(tmp_path: Path) -> None:
    """Guards the scan: it must flag the shape it exists to catch."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "logger.info('race on %s', item['canonical_name'])\n"
        "logger.warning('match for %s', name)\n"
        "logger.debug('fine at %s', name)\n"
    )
    assert _loud_lines_with_names(probe) == ["probe.py:1", "probe.py:2"]
