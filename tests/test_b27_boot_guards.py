"""Settings that core-api refuses at startup (B27: L-08, L-35).

- L-08: the interview and MCP budgets were checked against the 120s platform
  ceiling, but the bulk and blanket request budgets were not. Raised past it
  (``BULK_REQUEST_TIMEOUT_SECONDS=150`` is the natural answer to bulk 504s),
  the platform severs the connection at 120s with its own bare 504 while the
  handler keeps running and may commit, so the app's structured 504 never
  reaches the client.
- L-35: with ``USE_STM=true`` and the in-memory STM backend, each worker keeps
  its own STM, so notes and bulletins read differently request to request. The
  warning for that read ``WEB_CONCURRENCY``, which nothing set: the image ran
  ``--workers 2`` and core-api never learned the count. Decided 2026-10-09
  (Eldad): the image starts its workers from ``WEB_CONCURRENCY``, and core-api
  refuses to start with in-memory STM on more than one worker.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core_api.config import PLATFORM_REQUEST_CEILING_SECONDS, Settings

pytestmark = [pytest.mark.unit]

_DOCKERFILE = Path(__file__).resolve().parents[1] / "core-api" / "Dockerfile"


@pytest.mark.parametrize(
    "field", ["bulk_request_timeout_seconds", "request_timeout_seconds"]
)
def test_l08_a_budget_past_the_platform_ceiling_is_refused(field):
    with pytest.raises(ValueError, match=f"{field} .* must be <= PLATFORM_REQUEST"):
        Settings(**{field: PLATFORM_REQUEST_CEILING_SECONDS + 1})


@pytest.mark.parametrize(
    "field", ["bulk_request_timeout_seconds", "request_timeout_seconds"]
)
def test_l08_a_budget_at_the_ceiling_still_starts(field):
    Settings(**{field: PLATFORM_REQUEST_CEILING_SECONDS})


def test_l35_in_memory_stm_on_several_workers_is_refused(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    with pytest.raises(ValueError, match="STM_BACKEND=redis"):
        Settings(use_stm=True, stm_backend="memory")


def test_l35_the_combinations_that_work_still_start(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    Settings(use_stm=True, stm_backend="redis")
    Settings(use_stm=False, stm_backend="memory")
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    Settings(use_stm=True, stm_backend="memory")


def test_l35_the_image_starts_its_workers_from_web_concurrency():
    dockerfile = _DOCKERFILE.read_text()
    assert re.search(r"^ENV WEB_CONCURRENCY=2$", dockerfile, re.MULTILINE), (
        "the image must say how many workers it runs, where core-api can read it"
    )
    cmd = next(line for line in dockerfile.splitlines() if line.startswith("CMD "))
    # Expanded by the shell the CMD runs in; quoted, so escaped inside JSON.
    assert re.search(r'--workers \\?"\$\{WEB_CONCURRENCY\}', cmd), cmd
    assert '"--workers", "2"' not in dockerfile, "a second, fixed worker count"
