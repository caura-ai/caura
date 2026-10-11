"""The stock compose stack runs the lifecycle scheduler (M-109), and the
``expires_at`` description says what expiry really does (L-99).

The integration guide promised a background scheduler that expires, archives
and crystallizes every 24 hours, and every write schema's ``expires_at`` said an
expired row "is archived on the next lifecycle tick". Only core-operations ever
fires those ticks. No compose file ran it and no release published an image for
it, so on the documented ``docker compose up`` stack expired rows stayed active
and kept surfacing in recall, stale rows were never archived, soft-deleted rows
were never purged, and crystallization never ran.

The stack now runs core-operations. Its ticks call core-api's admin-only
endpoints, and the stock stack has no admin key, so a one-shot init container
writes a random one to a private volume that both services read through new
``*_FILE`` settings. An ``ADMIN_API_KEY`` the operator sets still wins, for
both. The image is published as ``caura-core-operations``: rule 7 of the rebrand
plan mints nothing new under the old name.

Expiry moves a row to ``outdated``, not ``archived``; the description says so.

L-229 (audit 2026-10-01, B41): the generated key also shadowed a legacy
``API_KEY``. core-api read the file whenever ``ADMIN_API_KEY`` was blank, and
the admin key wins over ``API_KEY``, so a stock-compose ``.env`` carrying only
the legacy key lost admin access with it. The file is now read only when both
are blank, and the scheduler presents the legacy key in that case too.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from core_api.config import Settings as CoreApiSettings
from core_api.constants import EXPIRES_AT_DESCRIPTION
from core_operations.config import Settings as OpsSettings

REPO = Path(__file__).resolve().parents[1]
KEY_PATH = "/run/caura-admin/admin-key"


def _compose() -> dict:
    return yaml.safe_load((REPO / "docker-compose.yml").read_text())


def _env(service: dict) -> dict:
    env = service.get("environment") or {}
    if isinstance(env, list):
        return dict(item.split("=", 1) for item in env)
    return env


# ── the compose stack ─────────────────────────────────────────────────────


def test_the_stack_runs_the_scheduler_against_core_api():
    ops = _compose()["services"]["core-operations"]
    env = _env(ops)
    assert env["CORE_API_URL"] == "http://core-api:8000"
    assert ops["build"]["dockerfile"] == "core-operations/Dockerfile"
    assert ops["depends_on"]["core-api"]["condition"] == "service_healthy"
    assert "healthcheck" in ops
    # env.dev and .env.example both set IS_STANDALONE=true, which is the
    # scheduler's off switch, so the service must not load either.
    assert "env_file" not in ops
    assert str(env.get("IS_STANDALONE", "false")).lower() != "true"


def test_core_api_and_the_scheduler_share_one_generated_admin_key():
    services = _compose()["services"]
    init = services["admin-key-init"]
    assert KEY_PATH in "".join(init["command"])
    volume = init["volumes"][0].split(":")[0]
    readers = (
        ("core-api", "ADMIN_API_KEY_FILE"),
        ("core-operations", "CORE_API_ADMIN_API_KEY_FILE"),
    )
    for name, setting in readers:
        service = services[name]
        assert _env(service)[setting] == KEY_PATH, name
        assert f"{volume}:/run/caura-admin:ro" in service["volumes"], name
        condition = service["depends_on"]["admin-key-init"]["condition"]
        assert condition == "service_completed_successfully", name


def test_an_operator_admin_key_reaches_the_scheduler_too():
    """core-api reads ``.env`` through ``env_file``; Compose interpolates the same
    file, so an ``ADMIN_API_KEY`` set there is what both services present. With
    only the legacy ``API_KEY`` set, core-api's admin key is that one (L-229), so
    the scheduler presents it too rather than the generated file's."""
    env = _env(_compose()["services"]["core-operations"])
    assert env["CORE_API_ADMIN_API_KEY"] == "${ADMIN_API_KEY:-${API_KEY:-}}"


def test_the_scheduler_image_is_published_under_the_new_name():
    image = _compose()["services"]["core-operations"]["image"]
    assert image.startswith("ghcr.io/caura-ai/caura-core-operations:")
    workflow = yaml.safe_load(
        (REPO / ".github" / "workflows" / "publish-docker.yml").read_text()
    )
    matrix = workflow["jobs"]["build-and-push"]["strategy"]["matrix"]["include"]
    images = {entry["name"]: entry.get("image") for entry in matrix}
    assert set(images) == {"core-api", "core-storage-api", "core-operations"}
    assert images["core-operations"] == "caura-core-operations"
    assert all(images.values()), "every image names its published repository"


# ── the admin key file ────────────────────────────────────────────────────


@pytest.fixture
def key_file(tmp_path, monkeypatch) -> Path:
    for name in (
        "API_KEY",
        "ADMIN_API_KEY",
        "ADMIN_API_KEY_FILE",
        "CORE_API_ADMIN_API_KEY",
        "CORE_API_ADMIN_API_KEY_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "admin-key"
    path.write_text("generated-admin-key\n")
    return path


@pytest.mark.parametrize("operator_key", [None, ""], ids=["unset", "blank"])
def test_core_api_reads_the_admin_key_from_its_file(key_file, operator_key):
    """Blank as well as unset: ``.env.example`` ships ``ADMIN_API_KEY=``."""
    settings = CoreApiSettings(
        _env_file=None, admin_api_key=operator_key, admin_api_key_file=str(key_file)
    )
    assert settings.admin_api_key == "generated-admin-key"


@pytest.mark.parametrize("operator_key", ["", None], ids=["blank", "unset"])
def test_the_scheduler_reads_the_admin_key_from_its_file(key_file, operator_key):
    kwargs = {} if operator_key is None else {"core_api_admin_api_key": operator_key}
    settings = OpsSettings(
        _env_file=None, core_api_admin_api_key_file=str(key_file), **kwargs
    )
    assert settings.core_api_admin_api_key == "generated-admin-key"


def test_an_operator_admin_key_wins_over_the_file(key_file):
    """Guard: the generated key is only a fallback."""
    api = CoreApiSettings(
        _env_file=None, admin_api_key="operator-key", admin_api_key_file=str(key_file)
    )
    ops = OpsSettings(
        _env_file=None,
        core_api_admin_api_key="operator-key",
        core_api_admin_api_key_file=str(key_file),
    )
    assert (api.admin_api_key, ops.core_api_admin_api_key) == ("operator-key",) * 2


def test_l229_a_legacy_api_key_wins_over_the_file(key_file):
    """A stock-compose ``.env`` with only the legacy ``API_KEY``: that key stays
    the admin key (``get_admin_key`` is ``admin_api_key or api_key``)."""
    api = CoreApiSettings(
        _env_file=None, api_key="legacy-key", admin_api_key_file=str(key_file)
    )
    assert (api.admin_api_key or api.api_key) == "legacy-key"


# ── L-99 ──────────────────────────────────────────────────────────────────


def test_expires_at_says_an_expired_row_becomes_outdated():
    assert "outdated" in EXPIRES_AT_DESCRIPTION
    assert "archived" not in EXPIRES_AT_DESCRIPTION
