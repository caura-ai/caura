"""Guards for code and config that ships but cannot run.

Four subsystems were deleted in one change because each had the same shape:
a complete, plausible implementation with no path from a running process to
it. ``SqliteBackend`` + ``InProcessQueue`` + ``ConfigIdentity`` +
``ManualResolver`` behind four factories no setting named; the
``services/payment`` package behind eight ``PADDLE_*``/``payment_provider``
settings nothing read; ``auth.enforce_org_admin`` and ``auth.hash_key``.

None of that failed a test, because tests were the only callers. These
assertions are about REACHABILITY rather than behaviour: they ask whether a
thing the repo declares can be reached from something that runs, which is the
question the tests those subsystems already had could not ask about
themselves.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]

# Source roots scanned for "is this reachable". Scoped explicitly rather than
# globbed from the repo root: a developer checkout carries ``.worktrees/`` and
# ``.venv/`` inside it, and a scan that wandered into either would answer with
# some other branch's code.
_SOURCE_ROOTS = (
    "common",
    "core-api/src",
    "core-operations/src",
    "core-storage-api/src",
    "core-worker/src",
    "scripts",
    "tests",
)

_CORE_API_CONFIG = _REPO / "core-api/src/core_api/config.py"


_THIS_FILE = pathlib.Path(__file__).resolve()


def _python_files(*, exclude: pathlib.Path | None = None) -> list[pathlib.Path]:
    """Every scanned source file, minus this one.

    Excluding self is not tidiness — the allowlist below NAMES the settings it
    expects to find unread, so a scan that read this file would report every
    one of them as read and pass while proving nothing.
    """
    out: list[pathlib.Path] = []
    for root in _SOURCE_ROOTS:
        for path in (_REPO / root).rglob("*.py"):
            if path.resolve() == _THIS_FILE:
                continue
            if exclude is not None and path == exclude:
                continue
            out.append(path)
    return out


def _is_org_role_admin_compare(node: ast.AST) -> bool:
    """``<x>.org_role == "admin"`` or ``getattr(<x>, "org_role", …) == "admin"``."""
    if not isinstance(node, ast.Compare) or len(node.comparators) != 1:
        return False
    right = node.comparators[0]
    if not (isinstance(right, ast.Constant) and right.value == "admin"):
        return False
    left = node.left
    if isinstance(left, ast.Attribute) and left.attr == "org_role":
        return True
    return (
        isinstance(left, ast.Call)
        and isinstance(left.func, ast.Name)
        and left.func.id == "getattr"
        and len(left.args) >= 2
        and isinstance(left.args[1], ast.Constant)
        and left.args[1].value == "org_role"
    )


def _settings_fields(path: pathlib.Path) -> dict[str, int]:
    """``{field_name: lineno}`` for the ``Settings`` class in *path*."""
    fields: dict[str, int] = {}
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(
                    stmt.target, ast.Name
                ):
                    fields[stmt.target.id] = stmt.lineno
    return fields


# Settings core-api declares that nothing reads. Every entry is a live finding,
# not an exemption: shipping a knob named ``CRYSTALLIZER_ENABLED`` that cannot
# enable or disable anything is the same defect as the ``PADDLE_*`` block this
# change removed. They are NOT deleted here because the fix is a decision this
# change has no business making — whether the crystallizer should honour them
# (wire them up) or has genuinely outgrown them (delete). See oss-0814-m-47.
#
# ``crystallizer_dedup_threshold``, the fourth knob in the same block, IS read,
# which is what makes the other three worth naming individually rather than
# waving at the prefix.
_KNOWN_UNREAD_CORE_API_SETTINGS = frozenset(
    {
        "crystallizer_dedup_sample_size",
        "crystallizer_enabled",
        "crystallizer_stale_days",
    }
)


def test_every_core_api_setting_is_read_by_something() -> None:
    """A declared setting nobody reads is a promise the process cannot keep.

    ``extra="ignore"`` means an operator who sets one gets no error and no
    effect, which is how eight ``PADDLE_*`` settings outlived the package that
    read them by long enough to be audited.
    """
    fields = _settings_fields(_CORE_API_CONFIG)
    assert len(fields) > 50, (
        f"only {len(fields)} Settings fields parsed — the AST walk missed the class"
    )

    # config.py itself counts as a reader (``bridge_credentials_to_environ``
    # copies PLATFORM_*/ENTITY_* values into os.environ for common.llm, and
    # validators read their own siblings) — but only with each field's OWN
    # declaration line blanked, or every field would trivially "read" itself.
    config_lines = _CORE_API_CONFIG.read_text().splitlines()
    for lineno in fields.values():
        config_lines[lineno - 1] = ""
    haystack = ["\n".join(config_lines)]
    haystack.extend(
        p.read_text(errors="ignore") for p in _python_files(exclude=_CORE_API_CONFIG)
    )
    blob = "\n".join(haystack)

    unread = {
        name
        for name in fields
        # ``.name`` covers settings.name and self.name; the quoted forms cover
        # getattr()/monkeypatch.setattr() and the env-var bridge's dict keys.
        if not re.search(rf"\.{re.escape(name)}\b", blob)
        and f'"{name}"' not in blob
        and f"'{name}'" not in blob
    }
    assert unread == _KNOWN_UNREAD_CORE_API_SETTINGS, (
        f"unread core-api settings changed: unexpected={sorted(unread - _KNOWN_UNREAD_CORE_API_SETTINGS)}, "
        f"now-read={sorted(_KNOWN_UNREAD_CORE_API_SETTINGS - unread)}"
    )


def test_the_provider_registry_exports_only_factories_a_setting_selects() -> None:
    """A backend factory with no config name is reachable only by hand.

    That is what the four removed factories had in common, and the reason the
    ``sqlite``/``inprocess``/``config``/``manual`` defaults read as choices
    when they were the only option each would ever return.
    """
    from core_api.providers import _registry

    assert set(_registry.__all__) == {"get_llm_provider", "get_stm_backend"}

    config_text = _CORE_API_CONFIG.read_text()
    # The name each surviving factory dispatches on must exist as a setting.
    assert "stm_backend: str" in config_text
    assert "llm_provider: str" in config_text


def test_the_org_admin_predicate_has_exactly_one_definition() -> None:
    """``is_admin or org_role == 'admin'`` was written out three times.

    Nobody reached for ``AuthContext.enforce_org_admin`` — the copy that could
    have been shared — so it sat unused while the callers that needed exactly
    its logic each rebuilt it. The property is the shared form; this asserts
    the copies did not come back.
    """
    sightings: list[str] = []
    users: list[str] = []
    for path in _python_files():
        if "core-api/src" not in str(path):
            continue
        text = path.read_text(errors="ignore")
        if "is_org_admin" in text:
            users.append(str(path.relative_to(_REPO)))
        # Compared as an AST, not as text. A regex over source would also
        # match this file's own docstring and the two comments that quote the
        # predicate to explain it — prose describing a defect would read as
        # the defect.
        if any(_is_org_role_admin_compare(node) for node in ast.walk(ast.parse(text))):
            sightings.append(str(path.relative_to(_REPO)))

    assert users, (
        "scanned no file mentioning is_org_admin — the scan is looking in the wrong place"
    )
    assert len(users) >= 3, f"expected auth.py plus its callers, found {users}"
    assert sightings == ["core-api/src/core_api/auth.py"], (
        f"the org-admin predicate is written out inline again in: {sightings}"
    )


@pytest.mark.parametrize(
    "module",
    [
        "core_api.providers.sqlite_backend",
        "core_api.providers.inprocess_queue",
        "core_api.providers.config_identity",
        "core_api.providers.manual_resolver",
        "core_api.services.payment",
    ],
)
def test_the_removed_subsystems_are_gone_and_unimported(module: str) -> None:
    """Deleted, and no import left pointing at the hole."""
    with pytest.raises(ModuleNotFoundError):
        __import__(module)

    scanned = _python_files()
    assert len(scanned) > 100, (
        f"only {len(scanned)} files scanned — the source roots are wrong"
    )

    tail = module.rsplit(".", 1)[-1]
    importers = [
        str(p.relative_to(_REPO))
        for p in _python_files()
        if re.search(
            rf"\b(import|from)\b[^\n]*\b{re.escape(tail)}\b",
            p.read_text(errors="ignore"),
        )
    ]
    assert importers == [], f"{module} is imported by {importers}"


def test_aiosqlite_is_declared_only_if_something_imports_it() -> None:
    """The dependency outlived its single importer by exactly one commit.

    ``requirements.txt`` even documented what it was for — "for OSS standalone
    storage backend" — which stopped being true when that backend stopped
    being reachable, not when it was deleted.
    """
    imports_it = [
        str(p.relative_to(_REPO))
        for p in _python_files()
        if re.search(
            r"^\s*import aiosqlite\b|^\s*from aiosqlite\b",
            p.read_text(errors="ignore"),
            re.M,
        )
    ]
    declared_in = [
        name
        for name, path in (
            ("requirements.txt", _REPO / "requirements.txt"),
            ("core-api/pyproject.toml", _REPO / "core-api/pyproject.toml"),
        )
        if "aiosqlite" in path.read_text()
    ]
    assert bool(imports_it) == bool(declared_in), (
        f"aiosqlite imported by {imports_it} but declared in {declared_in}"
    )
