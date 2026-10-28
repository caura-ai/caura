"""The stock compose stack runs the async-embed consumer (#720).

``DEPLOYMENT_MODE=deferred`` publishes ``EMBED_REQUESTED`` /
``ENRICH_REQUESTED`` for core-worker, and core-api subscribes to neither
(M-15): on the in-process bus nothing receives them, so every deferred
write stayed ``enrichment_pending`` / ``embedding=NULL`` forever with no
error anywhere. core-worker shipped a Dockerfile but no compose service
and no published image, so the documented self-hosted path could not run
it at all.

The stack now runs core-worker. It PATCHes embed/enrich results back to
core-storage-api, which requires the shared secret storage-secret-init
generates — mounted read-only, exactly as core-api and core-storage-api
do. The image is published as ``caura-core-worker``: rule 7 of the
rebrand plan mints nothing new under the old name.

The bus is the contract: the worker only sees events a shared bus
delivers cross-process, so the service block names
``EVENT_BUS_BACKEND=pubsub`` as the deferred-mode requirement, and
core-api still refuses a deferred boot on the in-process bus (M-15)
rather than accept writes nothing completes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from core_worker.config import Settings as WorkerSettings

REPO = Path(__file__).resolve().parents[1]
SECRET_PATH = "/run/caura-storage/shared-secret"


def _compose() -> dict:
    return yaml.safe_load((REPO / "docker-compose.yml").read_text())


def _env(service: dict) -> dict:
    env = service.get("environment") or {}
    if isinstance(env, list):
        return dict(item.split("=", 1) for item in env)
    return env


# ── the compose stack ─────────────────────────────────────────────────────


def test_the_stack_runs_the_worker_against_core_storage_api():
    worker = _compose()["services"]["core-worker"]
    env = _env(worker)
    assert env["CORE_STORAGE_API_URL"] == "http://core-storage-api:8002"
    assert worker["build"]["dockerfile"] == "core-worker/Dockerfile"
    # Healthy, not merely started: the worker PATCHes storage on every
    # event it consumes.
    assert worker["depends_on"]["core-storage-api"]["condition"] == "service_healthy"
    assert "healthcheck" in worker
    # Provider keys (and the EVENT_BUS_* settings) arrive through env_file;
    # env.dev's fake providers are what let a keyless stack boot the worker.
    assert "env.dev" in worker["env_file"]


def test_the_worker_shares_the_storage_secret_volume():
    """The worker refuses to start without CORE_STORAGE_SHARED_SECRET, so it
    reads the same generated file core-storage-api does, read-only."""
    worker = _compose()["services"]["core-worker"]
    assert _env(worker)["CORE_STORAGE_SHARED_SECRET_FILE"] == SECRET_PATH
    assert "storage_secret:/run/caura-storage:ro" in worker["volumes"]


def test_the_worker_block_names_the_shared_bus_requirement():
    """deferred mode needs core-worker on a bus core-api also publishes to
    (M-15); the service block must say so instead of leaving an operator to
    discover the in-process default the hard way."""
    text = (REPO / "docker-compose.yml").read_text()
    assert "EVENT_BUS_BACKEND=pubsub" in text


def test_the_worker_image_is_published_under_the_new_name():
    image = _compose()["services"]["core-worker"]["image"]
    assert image.startswith("ghcr.io/caura-ai/caura-core-worker:")
    workflow = yaml.safe_load(
        (REPO / ".github" / "workflows" / "publish-docker.yml").read_text()
    )
    matrix = workflow["jobs"]["build-and-push"]["strategy"]["matrix"]["include"]
    images = {entry["name"]: entry.get("image") for entry in matrix}
    assert set(images) == {
        "core-api",
        "core-storage-api",
        "core-operations",
        "core-worker",
    }
    assert images["core-worker"] == "caura-core-worker"
    assert all(images.values()), "every image names its published repository"


def test_env_example_documents_the_worker_image_pin():
    text = (REPO / ".env.example").read_text()
    assert "ghcr.io/caura-ai/caura-core-worker:v1.2.3" in text


# ── the storage secret file ───────────────────────────────────────────────


@pytest.fixture
def secret_file(tmp_path, monkeypatch) -> Path:
    for name in (
        "CORE_STORAGE_SHARED_SECRET",
        "CORE_STORAGE_SHARED_SECRET_FILE",
        "CORE_STORAGE_API_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "shared-secret"
    path.write_text("generated-shared-secret\n")
    return path


def test_the_worker_reads_the_shared_secret_from_its_file(secret_file):
    settings = WorkerSettings(
        _env_file=None,
        core_storage_api_url="http://core-storage-api:8002",
        core_storage_shared_secret_file=str(secret_file),
    )
    assert (
        settings.core_storage_shared_secret.get_secret_value()
        == "generated-shared-secret"
    )


def test_an_explicit_shared_secret_wins_over_the_file(secret_file):
    """Guard: the generated secret is only a fallback — an operator's
    CORE_STORAGE_SHARED_SECRET must still reach the worker."""
    settings = WorkerSettings(
        _env_file=None,
        core_storage_api_url="http://core-storage-api:8002",
        core_storage_shared_secret="operator-secret",
        core_storage_shared_secret_file=str(secret_file),
    )
    assert settings.core_storage_shared_secret.get_secret_value() == "operator-secret"
