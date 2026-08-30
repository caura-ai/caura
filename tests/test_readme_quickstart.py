"""Contract tests for the executable README keyless quickstart."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_readme_quickstart as checker
from check_readme_quickstart import (
    END_MARKER,
    START_MARKER,
    CurlRequest,
    QuickstartError,
    assert_round_trip,
    extract_curl_argv,
    load_requests,
    run_curl,
    validate_base_url,
    validate_curl,
)


def _marked_block(command: str) -> str:
    return f"{START_MARKER}\n```bash\n{command}\n```\n{END_MARKER}\n"


def test_readme_keyless_quickstart_is_the_expected_keyword_round_trip() -> None:
    write, search = load_requests()

    assert write.payload == {
        "tenant_id": "default",
        "agent_id": "quickstart",
        "write_mode": "strong",
        "content": "Our auth service uses JWT with 15-minute expiry.",
    }
    assert search.payload == {"tenant_id": "default", "query": "JWT expiry"}


def test_marked_block_rejects_non_curl_shell_commands() -> None:
    markdown = _marked_block(
        "curl -X POST \\\n  http://localhost:8000/api/v1/memories\nrm -rf /tmp/example"
    )

    with pytest.raises(QuickstartError, match="only curl commands"):
        extract_curl_argv(markdown)


def test_marked_block_rejects_reversed_markers() -> None:
    markdown = f"{END_MARKER}\n{START_MARKER}\n```bash\n# empty\n```\n"

    with pytest.raises(QuickstartError, match="end marker must follow"):
        extract_curl_argv(markdown)


def test_marked_block_rejects_whitespace_after_continuation() -> None:
    markdown = _marked_block(
        "curl -X POST \\   \n  http://localhost:8000/api/v1/memories"
    )

    with pytest.raises(QuickstartError, match="final character"):
        extract_curl_argv(markdown)


def test_curl_validation_rejects_remote_targets() -> None:
    argv = (
        "curl",
        "-X",
        "POST",
        "https://example.com/api/v1/memories",
        "-H",
        "X-API-Key: standalone",
        "-H",
        "Content-Type: application/json",
        "-d",
        '{"tenant_id":"default"}',
    )

    with pytest.raises(QuickstartError, match="localhost:8000"):
        validate_curl(argv)


def test_curl_validation_rejects_additional_options() -> None:
    argv = (
        "curl",
        "-X",
        "POST",
        "http://localhost:8000/api/v1/memories",
        "-H",
        "X-API-Key: standalone",
        "-H",
        "Content-Type: application/json",
        "-d",
        '{"tenant_id":"default"}',
        "--output",
        "/tmp/leak",
    )

    with pytest.raises(QuickstartError, match="unsupported curl argument"):
        validate_curl(argv)


def test_curl_validation_rejects_duplicate_data_flags() -> None:
    argv = (
        "curl",
        "-X",
        "POST",
        "http://localhost:8000/api/v1/memories",
        "-H",
        "X-API-Key: standalone",
        "-H",
        "Content-Type: application/json",
        "-d",
        "@/etc/passwd",
        "-d",
        '{"tenant_id":"default"}',
    )

    with pytest.raises(QuickstartError, match="duplicate -d"):
        validate_curl(argv)


def test_curl_execution_reconstructs_instead_of_replaying_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, ...]] = []

    def fake_run(argv, **_kwargs):
        captured.append(argv)
        return SimpleNamespace(returncode=0, stdout='{"id":"created-id"}', stderr="")

    monkeypatch.setattr(checker.subprocess, "run", fake_run)
    request = CurlRequest(
        path="/api/v1/memories",
        payload={"tenant_id": "default", "content": "@/proc/self/environ"},
    )

    assert run_curl(request, "http://localhost:18001") == {"id": "created-id"}
    assert captured[0][1:4] == ("--disable", "--noproxy", "*")
    assert captured[0].count("-d") == 1
    assert captured[0][-1] == (
        '{"tenant_id":"default","content":"@/proc/self/environ"}'
    )


def test_runtime_override_stays_on_localhost() -> None:
    assert validate_base_url("http://localhost:18001/") == "http://localhost:18001"
    assert validate_base_url("http://localhost:18001?") == "http://localhost:18001"
    assert validate_base_url("http://localhost:18001#") == "http://localhost:18001"

    with pytest.raises(QuickstartError, match="localhost"):
        validate_base_url("https://example.com")


def test_round_trip_requires_search_to_return_the_created_memory() -> None:
    with pytest.raises(QuickstartError, match="did not return"):
        assert_round_trip(
            {"id": "created-id", "title": "Generated title"},
            {"items": [{"id": "other-id"}]},
        )


def test_round_trip_requires_the_title_promised_by_the_readme() -> None:
    with pytest.raises(QuickstartError, match="title"):
        assert_round_trip(
            {"id": "created-id", "title": None},
            {"items": [{"id": "created-id"}]},
        )
