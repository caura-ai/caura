"""The quickstart stack's default credentials stay off the network.

``docker compose up -d`` is the first command in both README.md and
AGENT-INSTALL.md. It starts Postgres as ``caura``/``changeme`` with no TLS and
Redis with no password at all, and both were published as ``"5432:5432"`` /
``"6379:6379"`` — which Docker binds to 0.0.0.0, not to loopback. Anything that
could route to the host could reach them: other machines on the LAN, and on a
cloud VM with a permissive security group, the internet.

Loopback is also what the documentation already promised —
``docs/self-hosting.md`` lists these as ``localhost:5432`` and
``localhost:6379`` — so this closes a gap between the compose file and the page
describing it, rather than changing the supported surface.
"""

from __future__ import annotations

import pathlib
import re

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]

# Services whose published port must not reach beyond the host. The API is
# deliberately absent: it is the surface you install the stack to reach, and it
# authenticates. These two authenticate with a published default or not at all.
_MUST_BE_LOOPBACK = ("db", "redis")

_COMPOSE_FILES = ("docker-compose.yml", "docker-compose.dev.yml")

# ``- "[<bind>:]<host>:<container>"`` inside a ports: list.
_PORT_LINE = re.compile(r'^\s*-\s*"(?P<mapping>[^"]+)"\s*$')


def _published_ports(compose: str) -> list[tuple[str, str]]:
    """``(service, mapping)`` for every published port in *compose*."""
    out: list[tuple[str, str]] = []
    service = ""
    in_ports = False
    for line in compose.splitlines():
        stripped = line.strip()
        if (
            re.match(r"^[a-z0-9-]+:$", stripped)
            and line.startswith("  ")
            and not line.startswith("    ")
        ):
            service = stripped[:-1]
            in_ports = False
            continue
        if stripped == "ports:":
            in_ports = True
            continue
        if in_ports:
            match = _PORT_LINE.match(line)
            if match:
                out.append((service, match.group("mapping")))
                continue
            if stripped and not stripped.startswith("#"):
                in_ports = False
    return out


def _split_mapping(mapping: str) -> list[str]:
    """Split a Compose port mapping on its TOP-LEVEL colons.

    ``str.split(":")`` is wrong here and quietly so: ``${DB_BIND:-127.0.0.1}``
    contains a colon of its own, so a plain split turns a correct three-part
    mapping into five pieces and the bind address is never recognised. Every
    mapping in both files then read as non-loopback — including the plain
    ``127.0.0.1:...`` one that has been correct all along.
    """
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for index, char in enumerate(mapping):
        if char == "$" and mapping[index + 1 : index + 2] == "{":
            depth += 1
        elif char == "}" and depth:
            depth -= 1
        if char == ":" and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return parts


def _binds_to_loopback(mapping: str) -> bool:
    """True when ``mapping`` names a loopback bind address.

    A Compose mapping is ``[<bind>:]<host>:<container>``, so ONLY the
    three-part form names an address at all — ``"5432:5432"`` publishes on
    0.0.0.0. An earlier version of this accepted any mapping beginning with
    ``${``, meaning to allow ``${DB_BIND:-127.0.0.1}``; it also accepted
    ``${DB_PORT:-5432}:5432``, which is the bug itself wearing a variable.
    """
    parts = _split_mapping(mapping)
    if len(parts) < 3:
        return False
    bind = parts[0]
    if bind == "127.0.0.1":
        return True
    # ``${VAR:-default}`` — the DEFAULT is what ships.
    match = re.fullmatch(r"\$\{[A-Z_]+:-(?P<default>[^}]+)\}", bind)
    return bool(match) and match.group("default") == "127.0.0.1"


def test_the_mapping_splitter_handles_variables_with_defaults() -> None:
    """The splitter is the part that can be wrong, so it is asserted directly."""
    assert _split_mapping("5432:5432") == ["5432", "5432"]
    assert _split_mapping("127.0.0.1:8002:8002") == ["127.0.0.1", "8002", "8002"]
    assert _split_mapping("${DB_BIND:-127.0.0.1}:${DB_PORT:-5432}:5432") == [
        "${DB_BIND:-127.0.0.1}",
        "${DB_PORT:-5432}",
        "5432",
    ]
    assert _binds_to_loopback("${DB_BIND:-127.0.0.1}:${DB_PORT:-5432}:5432")
    assert _binds_to_loopback("127.0.0.1:${STORAGE_API_PORT:-8002}:8002")
    assert not _binds_to_loopback("${DB_PORT:-5432}:5432")
    assert not _binds_to_loopback("5432:5432")
    assert not _binds_to_loopback("${DB_BIND:-0.0.0.0}:${DB_PORT:-5432}:5432")


def test_the_scan_finds_published_ports_at_all() -> None:
    """Vacuity: a parser that matched nothing would pass every case below."""
    found = _published_ports((_REPO / "docker-compose.yml").read_text())
    services = {service for service, _ in found}
    assert len(found) >= 3, f"parsed only {found}"
    assert {"db", "redis"} <= services, f"parsed services {services}"


@pytest.mark.parametrize("compose_file", _COMPOSE_FILES)
def test_default_credentialed_services_publish_only_to_loopback(
    compose_file: str,
) -> None:
    path = _REPO / compose_file
    if not path.exists():  # pragma: no cover - both ship today
        pytest.skip(f"{compose_file} not present")

    offenders = [
        (service, mapping)
        for service, mapping in _published_ports(path.read_text())
        if service in _MUST_BE_LOOPBACK and not _binds_to_loopback(mapping)
    ]
    assert not offenders, (
        f"{compose_file} publishes {offenders} beyond loopback. Postgres here is "
        "caura/changeme and Redis has no password; bind them to 127.0.0.1 (the "
        "bind address stays overridable via DB_BIND / REDIS_BIND)."
    )


@pytest.mark.parametrize("service,var", [("db", "DB_BIND"), ("redis", "REDIS_BIND")])
def test_the_loopback_default_is_overridable_and_defaults_closed(
    service: str, var: str
) -> None:
    """A default nobody can change is a fork waiting to happen; a default that
    opens is the bug. Both halves, so neither can be lost."""
    compose = (_REPO / "docker-compose.yml").read_text()
    mapping = next(m for s, m in _published_ports(compose) if s == service)
    assert mapping.startswith(f"${{{var}:-127.0.0.1}}:"), mapping


@pytest.mark.parametrize("var", ["DB_BIND", "REDIS_BIND"])
def test_the_override_is_documented_where_operators_look(var: str) -> None:
    """A mechanism documented only in the file that implements it.

    ``.env.example`` is what an operator copies; the compose comments are not.
    The override existed and worked, and was discoverable only by reading the
    file it was meant to save you from reading.
    """
    env_example = (_REPO / ".env.example").read_text()
    assert var in env_example, (
        f"{var} controls where docker-compose publishes a datastore but is absent "
        "from .env.example"
    )
