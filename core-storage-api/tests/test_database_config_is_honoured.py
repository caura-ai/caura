"""OSS audit 09/02 M-01 + M-02 — database settings that documented themselves
into being ignored.

**M-01** — ``POSTGRES_REQUIRE_SSL`` was documented in ``.env.example``,
``AGENT-INSTALL.md`` and both compose files as "Set true in production", and
read by nothing: no ``connect_args``, no ``sslmode``, no ``ssl=`` existed
anywhere in the tree. ``Settings.model_config`` sets ``extra="ignore"``, so
pydantic accepted the variable and dropped it — the operator got neither TLS
nor an error.

**M-02** — ``.env.example`` recommended ``DB_POOL_SIZE=50`` /
``DB_MAX_OVERFLOW=50``, ten times the 5 + 5 the source defaults were lowered
to after over-allocating produced ``asyncpg.TooManyConnectionsError``. Copying
``.env.example`` is how a deployment starts, so the recommendation reached
every fresh operator and reintroduced exactly the environment-side correction
that change removed.

The tests below are about the WIRING, not about TLS itself: that
``ssl="require"`` really refuses a server which will not upgrade is asyncpg's
behaviour, measured once against a non-TLS Postgres (it raises
``ConnectionError: ... rejected SSL upgrade`` where the default connects
fine).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

import core_storage_api.config as config_module
import core_storage_api.scripts.preflight_012 as preflight
from core_storage_api.config import Settings, db_connect_args
from core_storage_api.db_tls import tls_required_from_env

_ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"


def test_the_documented_env_var_name_actually_reaches_the_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole defect in one assertion.

    ``extra="ignore"`` means a name nothing binds is accepted and discarded, so
    this must pin the NAME as published, not the field. Renaming the field
    without updating the docs would leave the published variable inert again
    and every other test here would still pass.
    """
    monkeypatch.setenv("POSTGRES_REQUIRE_SSL", "true")
    assert Settings().postgres_require_ssl is True, (
        "POSTGRES_REQUIRE_SSL is published in .env.example, AGENT-INSTALL.md "
        "and both compose files; if it does not bind here, setting it is a "
        "no-op and the operator is told they have TLS when they do not"
    )


def test_tls_off_leaves_the_connection_call_exactly_as_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default deployments must be untouched — an empty dict, not ``ssl=None``
    or ``ssl="prefer"``, either of which would be a behaviour change smuggled
    in under a fix for a setting nobody had working."""
    # Patched on the settings object, not the environment: the helper reads
    # the module singleton, which pydantic builds once at import — which is
    # correct for a value read at boot, and is why the env-var test above
    # constructs its own ``Settings()`` instead.
    monkeypatch.setattr(config_module.settings, "postgres_require_ssl", False)
    assert db_connect_args() == {}


def test_tls_on_requires_rather_than_prefers(monkeypatch: pytest.MonkeyPatch) -> None:
    """``prefer`` silently falls back to cleartext, which is the failure this
    setting exists to prevent — it would leave the same false promise in place
    while looking fixed."""
    monkeypatch.setattr(config_module.settings, "postgres_require_ssl", True)
    assert db_connect_args() == {"ssl": "require"}


def test_every_engine_this_service_opens_carries_the_policy() -> None:
    """Three separate call sites open connections: the app's engines, the
    migration runner, and the preflight script. A migration running in
    cleartext against a database the app reaches over TLS would defeat the
    setting while looking configured, so this asserts on the source rather
    than on one engine — the failure mode is a site being MISSED, and a test
    of the site you remembered cannot catch that.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "core_storage_api"
    # The preflight script reaches the policy through its own wrapper, which
    # falls back to the environment when service config cannot be built — see
    # ``_tls_connect_args`` there and the test below.
    sites = {
        "database/init.py": "db_connect_args",
        "database/migrations/env.py": "db_connect_args",
        "scripts/preflight_012.py": "_tls_connect_args",
    }
    missing, dsnless = [], []
    for path, helper in sites.items():
        called = re.compile(rf"connect_args={helper}\((?P<dsn>[^)]*)\)")
        match = called.search((src / path).read_text())
        if match is None:
            missing.append(path)
        elif not match.group("dsn").strip():
            dsnless.append(path)
    assert not missing, f"these open a connection without the configured TLS policy: {missing}"
    assert not dsnless, (
        "these pass no DSN, so a URL asking for ?ssl=verify-full is silently "
        f"downgraded to require at this site: {dsnless}"
    )


_NO_TLS_DSN = "postgresql+asyncpg://u:p@127.0.0.1:5432/db"
_VERIFIED_DSN = _NO_TLS_DSN + "?ssl=verify-full"


@pytest.mark.parametrize("weaker", ["", "?ssl=disable", "?ssl=allow", "?ssl=prefer", "?ssl=yes-please"])
def test_a_dsn_that_does_not_already_guarantee_tls_gets_the_policy(
    monkeypatch: pytest.MonkeyPatch, weaker: str
) -> None:
    """``allow`` and ``prefer`` fall back to cleartext, and an unrecognised
    value is not evidence of anything — none of them is a reason to stand
    down. The setting is a floor, so all of these land on ``require``."""
    monkeypatch.setattr(config_module.settings, "postgres_require_ssl", True)
    assert db_connect_args(_NO_TLS_DSN + weaker) == {"ssl": "require"}


@pytest.mark.parametrize("stronger", ["require", "verify-ca", "verify-full"])
def test_a_dsn_that_already_guarantees_tls_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, stronger: str
) -> None:
    """The floor must not become a ceiling. Returning ``{"ssl": "require"}``
    here is what downgrades ``verify-full`` — see the next test for why an
    empty dict is the only thing that preserves it."""
    monkeypatch.setattr(config_module.settings, "postgres_require_ssl", True)
    assert db_connect_args(f"{_NO_TLS_DSN}?ssl={stronger}") == {}


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [(_VERIFIED_DSN, "verify-full"), (_NO_TLS_DSN, "require")],
)
async def test_what_sqlalchemy_actually_hands_asyncpg(
    monkeypatch: pytest.MonkeyPatch, dsn: str, expected: str
) -> None:
    """The assertion the unit tests above cannot make for themselves.

    ``create_engine`` merges ``connect_args`` **on top of** the parameters it
    parsed out of the URL (``immutabledict(cparams).union(connect_args)``), so
    the kwarg wins over the DSN — the opposite of what an earlier revision of
    this PR documented. A helper that returned ``{"ssl": "require"}``
    unconditionally therefore turned certificate verification OFF for exactly
    the operator who had asked for it, in silence.

    So this asserts on the value asyncpg is really called with, taken from
    SQLAlchemy's own ``do_connect`` event, rather than on what
    ``db_connect_args`` returns in isolation. It fails if the helper stops
    reading the DSN, and it also fails if a future SQLAlchemy changes the
    merge direction and makes the helper's care unnecessary — either way the
    comment explaining all this needs rewriting, which is the point.
    """
    monkeypatch.setattr(config_module.settings, "postgres_require_ssl", True)
    engine = create_async_engine(dsn, connect_args=db_connect_args(dsn))
    captured: dict[str, object] = {}

    class _Captured(Exception):
        pass

    @event.listens_for(engine.sync_engine, "do_connect")
    def _capture(dialect, conn_rec, cargs, cparams):  # type: ignore[no-untyped-def]
        captured.update(cparams)
        raise _Captured  # fires before the DBAPI connect: no server needed

    try:
        with pytest.raises(_Captured):
            await engine.connect()
    finally:
        await engine.dispose()

    assert captured.get("ssl") == expected


@pytest.mark.parametrize("cased", ["Verify-Full", "VERIFY-FULL", " verify-full "])
def test_a_mis_cased_dsn_mode_is_still_recognised_as_stronger(
    monkeypatch: pytest.MonkeyPatch, cased: str
) -> None:
    """Recognition is case-insensitive; the DSN's own text is never rewritten.

    asyncpg's parsing IS case-sensitive — measured, ``ssl="Verify-Full"`` raises
    ``ClientConfigurationError: 'sslmode' parameter must be one of: disable,
    allow, prefer, require, verify-ca, verify-full``. So leaving the value
    alone gives the operator that error, naming the valid spellings. Treating
    it as unrecognised would instead have overridden it with ``require``, which
    CONNECTS — trading a precise, loud config error for a working connection at
    a weaker mode than was asked for, silently.
    """
    monkeypatch.setattr(config_module.settings, "postgres_require_ssl", True)
    assert db_connect_args(f"{_NO_TLS_DSN}?ssl={cased}") == {}


@pytest.mark.parametrize(
    ("value", "required"),
    [("true", True), ("1", True), ("YES", True), ("on", True), ("false", False), ("", False), (None, False)],
)
def test_the_environment_fallback_parses_the_same_flag(
    monkeypatch: pytest.MonkeyPatch, value: str | None, required: bool
) -> None:
    if value is None:
        monkeypatch.delenv("POSTGRES_REQUIRE_SSL", raising=False)
    else:
        monkeypatch.setenv("POSTGRES_REQUIRE_SSL", value)
    assert tls_required_from_env() is required


def test_the_preflight_still_applies_tls_when_service_config_cannot_be_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--dsn`` exists to run this against a DB whose service config does not
    load in the invoking shell, and ``_resolve_dsn`` already tolerates that.

    A TLS lookup that hard-depends on ``core_storage_api.config`` would make
    that tolerance unreachable. Falling back to ``{}`` would be no better in
    kind than the original defect: answering a config problem by dropping the
    security control. So the fallback re-reads the same variable.
    """
    monkeypatch.setitem(sys.modules, "core_storage_api.config", None)  # import -> ImportError
    monkeypatch.setenv("POSTGRES_REQUIRE_SSL", "true")
    assert preflight._tls_connect_args(_NO_TLS_DSN) == {"ssl": "require"}
    assert preflight._tls_connect_args(_VERIFIED_DSN) == {}
    monkeypatch.setenv("POSTGRES_REQUIRE_SSL", "false")
    assert preflight._tls_connect_args(_NO_TLS_DSN) == {}


def test_the_policy_module_imports_without_any_service_config() -> None:
    """The property the fallback rests on, asserted rather than assumed.

    Run in a subprocess under an env that makes ``Settings()`` raise, so this
    fails if ``db_tls`` ever grows an import that reaches ``config`` — directly
    or transitively — which would silently re-couple the two.

    ``DATABASE_URL`` has to come OUT of that env, not just ``ALLOYDB_HOST`` go
    in: ``resolve_alloydb_database_url`` returns early when ``database_url`` was
    set explicitly, and the suite sets it. With it left in place the env does
    not break anything and the vacuity check below is what says so.
    """
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    env["ALLOYDB_HOST"] = "an-instance-with-nothing-else-set"
    probe = (
        "import core_storage_api.db_tls as m, sys;"
        "print(m.tls_connect_args(None, require=True), 'core_storage_api.config' in sys.modules)"
    )
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=env)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "{'ssl': 'require'} False", done.stdout

    # ...and that the env really does break Settings, or the check above is
    # vacuous: it would pass just as well against a config that builds fine.
    broken = subprocess.run(
        [sys.executable, "-c", "import core_storage_api.config"], capture_output=True, text=True, env=env
    )
    assert broken.returncode != 0, "expected this env to make Settings() raise"
    assert "incomplete AlloyDB configuration" in broken.stderr


def _uncommented(name: str) -> str | None:
    for line in _ENV_EXAMPLE.read_text().splitlines():
        match = re.match(rf"^\s*{name}\s*=\s*(.*)$", line)
        if match:
            return match.group(1).strip()
    return None


@pytest.mark.parametrize(
    ("variable", "field"),
    [("DB_POOL_SIZE", "db_pool_size"), ("DB_MAX_OVERFLOW", "db_max_overflow")],
)
def test_the_example_env_does_not_recommend_a_pool_above_the_safe_baseline(variable: str, field: str) -> None:
    """``.env.example`` is a starting point that gets copied, so a value set
    here is a value most deployments run.

    The source default is the safe baseline, chosen after over-allocation
    produced ``asyncpg.TooManyConnectionsError``. An example file that sets a
    LARGER number silently overrides that baseline everywhere it is copied —
    which is what 50 + 50 did against 5 + 5. Commented out (or set no higher
    than the default) is what keeps the baseline meaningful.
    """
    recommended = _uncommented(variable)
    if recommended is None:
        return  # commented out: the code default applies, which is the point

    baseline = getattr(Settings(), field)
    assert int(recommended) <= baseline, (
        f".env.example sets {variable}={recommended}, above the source default "
        f"of {baseline}. Every deployment that copies this file inherits it, "
        "which is how the baseline stopped being the baseline."
    )
