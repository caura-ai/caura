"""M-67: the v1 upgrade guide moves an old volume to the names Compose uses.

PostgreSQL applies ``POSTGRES_USER`` and ``POSTGRES_DB`` only when it
initialises an empty data directory. v1's Compose file created the role and
database under the old name, and the current file connects as ``caura``, so on
a volume first started before release 2.46.2 core-storage-api cannot log in and
the guide's migration never runs. The guide must create the role and rename the
database, to the names ``docker-compose.yml`` connects with, before it runs
anything that connects as them.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

pytestmark = [pytest.mark.unit]

_REPO = Path(__file__).resolve().parents[1]
_GUIDE = _REPO / "docs" / "upgrading-from-v1.md"
_V1_NAME = "memclaw"  # legacy-name-floor: the role and database a v1 volume has


def _storage_dsn() -> tuple[str, str, str]:
    """The role, password and database core-storage-api connects with."""
    compose = yaml.safe_load((_REPO / "docker-compose.yml").read_text())
    env = compose["services"]["core-storage-api"]["environment"]
    dsn = urlsplit(env["DATABASE_URL"])
    assert dsn.username and dsn.password, env["DATABASE_URL"]
    return dsn.username, dsn.password, dsn.path.lstrip("/")


def _commands() -> list[str]:
    """The guide's shell commands in order, continuation lines joined."""
    blocks = re.findall(r"```bash\n(.*?)```", _GUIDE.read_text(), re.S)
    lines = "\n".join(blocks).replace("\\\n", " ").splitlines()
    return [line.strip() for line in lines if line.strip()]


def _first(commands: list[str], pattern: str) -> int | None:
    return next(
        (i for i, c in enumerate(commands) if re.search(pattern, c, re.I)), None
    )


def test_the_guide_moves_an_old_volume_before_the_migration_connects() -> None:
    user, password, database = _storage_dsn()
    commands = _commands()
    migration = _first(commands, r"init_database\(")
    assert migration is not None, "the guide no longer runs the migration"
    create = _first(
        commands, rf"CREATE ROLE {user} LOGIN SUPERUSER PASSWORD '{password}'"
    )
    rename = _first(commands, rf"ALTER DATABASE {_V1_NAME} RENAME TO {database}\b")
    assert create is not None and create < migration, (
        f"the guide never creates the role {user!r} the migration connects as"
    )
    assert rename is not None and rename < migration, (
        f"the guide never renames the v1 database to {database!r}, so the "
        "migration connects to a database that does not exist"
    )


def test_the_rename_connects_to_another_database() -> None:
    commands = _commands()
    rename = _first(commands, rf"ALTER DATABASE {_V1_NAME} RENAME")
    assert rename is not None, "the guide never renames the v1 database"
    connected = re.search(r"\s-d\s+(\S+)", commands[rename])
    assert connected and connected.group(1) != _V1_NAME, (
        "PostgreSQL cannot rename the database a session is connected to"
    )
