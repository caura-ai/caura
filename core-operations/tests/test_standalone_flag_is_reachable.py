"""09/02 L-45 — the scheduler short-circuit could not be switched on.

``Settings.standalone`` binds the environment variable ``STANDALONE``. Nothing
in the tree sets that name. The variable operators actually set is
``IS_STANDALONE`` — ``.env.example``, ``env.dev``, ``.env.test``, ``README.md``,
``AGENT-INSTALL.md``, ``docs/``, ``ci.yml`` and core-api's own
``is_standalone`` all use it — and ``model_config`` here sets
``extra="ignore"``, so pydantic accepted ``IS_STANDALONE`` and discarded it.

The guard it disabled is not cosmetic. Its own comment says an
accidentally-started core-operations in a standalone deployment should exit
rather than fire cron jobs against a single-tenant DB. With the flag unreachable
it did fire them, and logged ``standalone: False`` while doing it — the
misconfiguration reported itself as correct configuration.

The field is renamed rather than aliased: the old spelling was never set by
anything, so nothing loses an override, and one name per setting is what stops
this recurring.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from core_operations import app
from core_operations.config import Settings

_REPO = Path(__file__).resolve().parents[2]


def test_the_variable_operators_actually_set_reaches_the_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole defect in one assertion.

    ``extra="ignore"`` means a name nothing binds is accepted and dropped, so
    this pins the NAME as published rather than the field: renaming the field
    again without updating the docs would make the published variable inert
    once more while every other test here kept passing.
    """
    monkeypatch.setenv("IS_STANDALONE", "true")
    assert Settings().is_standalone is True, (
        "IS_STANDALONE is what .env.example, env.dev, the docs, CI and core-api "
        "all use; if it does not bind here the scheduler short-circuit cannot be "
        "switched on and a standalone deployment fires cron jobs anyway"
    )


def test_the_old_spelling_is_not_quietly_still_in_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``STANDALONE`` must not resurrect as a second way to say the same thing.

    Two spellings for one flag is how the original split happened. This fails
    if a compatibility alias is added without a decision to support both.
    """
    monkeypatch.delenv("IS_STANDALONE", raising=False)
    monkeypatch.setenv("STANDALONE", "true")
    assert Settings().is_standalone is False


def test_core_api_and_core_operations_agree_on_the_name() -> None:
    """The two services gate on the same operator decision.

    core-api refuses to boot with ``IS_STANDALONE=true`` in production; this
    service disables its scheduler. Reading different variables means one can be
    in standalone mode while the other is not, which is exactly the state the
    short-circuit exists to catch.
    """
    fields = set(Settings.model_fields)
    assert "is_standalone" in fields
    assert "standalone" not in fields


def test_the_short_circuit_reads_the_renamed_field() -> None:
    """A rename that misses the consumer leaves the guard reading a field that
    no longer exists — an AttributeError at boot, or worse, a stale copy."""
    source = inspect.getsource(app)
    assert "settings.is_standalone" in source
    assert "settings.standalone" not in source


def test_no_deployment_sets_the_old_name() -> None:
    """The evidence that renaming loses nothing, asserted rather than asserted-in-prose.

    If some env file or workflow did set ``STANDALONE=``, this rename would take
    an override away from it and the change would need an alias instead.
    """
    offenders, sightings = [], []
    for path in _REPO.rglob("*"):
        if not path.is_file() or ".git" in path.parts or ".venv" in path.parts:
            continue
        if path.suffix not in {".yml", ".yaml", ".md", ".example", ".dev", ".test", ".sh", ""}:
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if "IS_STANDALONE=" in stripped:
                sightings.append(f"{path.relative_to(_REPO)}:{number}")
            elif "STANDALONE=" in stripped:
                offenders.append(f"{path.relative_to(_REPO)}:{number}")
    # Vacuity check first: a scan whose filters exclude every relevant file
    # finds no offenders either, and would pass exactly like a clean tree.
    assert sightings, "scanned no file that sets IS_STANDALONE — the filters are wrong, not the tree"
    assert not offenders, f"these set the pre-rename name: {offenders}"


def test_every_setting_this_service_reads_is_one_it_declares() -> None:
    """Added in review: the mechanical answer to "did that deletion break a reader?"

    #1600 also removed ``core_storage_api_url``, dead since CAURA-655 moved every
    cron task onto core-api's admin endpoints (08/14 L-51). The review read that
    as an accidental deletion inside the rename hunk and called it a High — a
    reasonable read of a diff where two unrelated fields sit four lines apart.
    Answering it took a grep, and a grep is a weak answer to "does anything still
    read this": it is an argument from absence, and it misses the reader added
    tomorrow.

    So the property is asserted instead of argued. Every ``settings.<name>`` in
    this package's source must name a declared field, which fails on a deleted
    setting that still has a reader and on a typo'd one that never had a value.

    Static attribute access only. The dynamic form here is
    ``getattr(settings, hour_attr)`` behind ``_daily_at``/``_weekly_at``, where
    the literal is an argument to the helper rather than to ``getattr`` and AST
    cannot link the two — that path is covered behaviourally by
    ``test_all_lifecycle_jobs_registered_and_wall_clock_aligned``, which invokes
    every registered task's ``delay_provider`` and so would raise on a bad name.
    """
    import ast

    declared = set(Settings.model_fields)
    src_root = _REPO / "core-operations" / "src" / "core_operations"
    unknown = []
    for path in sorted(src_root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "settings"
                and node.attr not in declared
            ):
                unknown.append(f"{path.relative_to(_REPO)}:{node.lineno} settings.{node.attr}")
    assert not unknown, f"these read a setting the class does not declare: {unknown}"
