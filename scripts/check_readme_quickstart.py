#!/usr/bin/env python3
"""Run the curl-only portion of README's keyless quickstart.

The marked README block is user-facing documentation and therefore untrusted
input in pull-request CI.  This runner accepts only two localhost POSTs with
JSON bodies and the headers used by the quickstart; it never invokes a shell.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
START_MARKER = "<!-- readme-quickstart-ci:start -->"
END_MARKER = "<!-- readme-quickstart-ci:end -->"
EXPECTED_PATHS = ("/api/v1/memories", "/api/v1/search")
DEFAULT_BASE_URL = "http://localhost:8000"
EXPECTED_HEADERS = {
    "content-type": "application/json",
    "x-api-key": "standalone",
}


class QuickstartError(RuntimeError):
    """The documented quickstart is unsafe, malformed, or unsuccessful."""


@dataclass(frozen=True)
class CurlRequest:
    path: str
    payload: dict[str, Any]


def _marked_bash_lines(markdown: str) -> list[str]:
    if markdown.count(START_MARKER) != 1 or markdown.count(END_MARKER) != 1:
        raise QuickstartError("README must contain exactly one quickstart marker pair")

    start = markdown.index(START_MARKER) + len(START_MARKER)
    end = markdown.index(END_MARKER)
    if end <= start:
        raise QuickstartError(
            "README quickstart end marker must follow its start marker"
        )
    section = markdown[start:end]
    lines = section.strip().splitlines()
    if len(lines) < 3 or lines[0].strip() != "```bash" or lines[-1].strip() != "```":
        raise QuickstartError("quickstart markers must wrap one bash code fence")
    return lines[1:-1]


def extract_curl_argv(markdown: str) -> tuple[tuple[str, ...], ...]:
    """Extract logical curl commands without evaluating shell syntax."""
    commands: list[tuple[str, ...]] = []
    parts: list[str] = []

    for raw_line in _marked_bash_lines(markdown):
        line = raw_line.strip()
        if not line or (not parts and line.startswith("#")):
            continue
        if not parts and not line.startswith("curl "):
            raise QuickstartError(
                f"only curl commands are allowed in the marked block: {line!r}"
            )

        if line.endswith("\\") and not raw_line.endswith("\\"):
            raise QuickstartError(
                "a continuation backslash must be the line's final character"
            )
        continued = raw_line.endswith("\\")
        parts.append(line[:-1].rstrip() if continued else line)
        if continued:
            continue

        try:
            commands.append(tuple(shlex.split(" ".join(parts), posix=True)))
        except ValueError as exc:
            raise QuickstartError(
                f"invalid shell quoting in quickstart: {exc}"
            ) from exc
        parts.clear()

    if parts:
        raise QuickstartError("quickstart curl ends with an unfinished continuation")
    return tuple(commands)


def _next_value(argv: tuple[str, ...], index: int, flag: str) -> tuple[str, int]:
    try:
        return argv[index + 1], index + 2
    except IndexError as exc:
        raise QuickstartError(f"{flag} is missing its value") from exc


def validate_curl(argv: tuple[str, ...]) -> CurlRequest:
    """Accept the narrow curl grammar documented by the keyless quickstart."""
    if not argv or argv[0] != "curl":
        raise QuickstartError("quickstart command must invoke curl by name")

    method: str | None = None
    url: str | None = None
    body: str | None = None
    headers: dict[str, str] = {}
    index = 1

    while index < len(argv):
        token = argv[index]
        if token == "-X":
            if method is not None:
                raise QuickstartError("quickstart curl contains duplicate -X flags")
            method, index = _next_value(argv, index, token)
        elif token == "-H":
            raw_header, index = _next_value(argv, index, token)
            if ":" not in raw_header:
                raise QuickstartError(f"malformed curl header: {raw_header!r}")
            name, value = raw_header.split(":", 1)
            key = name.strip().lower()
            if key in headers:
                raise QuickstartError(f"duplicate curl header: {name.strip()!r}")
            headers[key] = value.strip()
        elif token == "-d":
            if body is not None:
                raise QuickstartError("quickstart curl contains duplicate -d flags")
            body, index = _next_value(argv, index, token)
        elif "://" in token:
            if url is not None:
                raise QuickstartError("quickstart curl contains more than one URL")
            url = token
            index += 1
        else:
            raise QuickstartError(f"unsupported curl argument: {token!r}")

    if method != "POST":
        raise QuickstartError("quickstart curl must use POST")
    if headers != EXPECTED_HEADERS:
        raise QuickstartError(f"quickstart curl headers changed: {headers!r}")
    if url is None or body is None:
        raise QuickstartError("quickstart curl requires one URL and one JSON body")

    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise QuickstartError(f"quickstart curl has an invalid URL: {url!r}") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "localhost"
        or port != 8000
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise QuickstartError(
            f"quickstart curl may target only localhost:8000: {url!r}"
        )

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise QuickstartError(f"quickstart curl body is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise QuickstartError("quickstart curl body must be a JSON object")
    return CurlRequest(path=parsed.path, payload=payload)


def load_requests(readme: Path = README) -> tuple[CurlRequest, CurlRequest]:
    commands = extract_curl_argv(readme.read_text())
    requests = tuple(validate_curl(command) for command in commands)
    paths = tuple(request.path for request in requests)
    if paths != EXPECTED_PATHS:
        raise QuickstartError(
            f"quickstart must write then search via {EXPECTED_PATHS!r}; found {paths!r}"
        )
    write, search = requests
    return write, search


def validate_base_url(value: str) -> str:
    parsed = urlsplit(value.rstrip("/"))
    try:
        port = parsed.port
    except ValueError as exc:
        raise QuickstartError("--base-url has an invalid port") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "localhost"
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise QuickstartError("--base-url must be an http://localhost:<port> origin")
    return f"http://localhost:{port}"


def run_curl(request: CurlRequest, base_url: str = DEFAULT_BASE_URL) -> dict[str, Any]:
    """Execute one already-validated command and decode its JSON response."""
    argv = (
        "curl",
        "--disable",
        "--noproxy",
        "*",
        "--silent",
        "--show-error",
        "--fail-with-body",
        "--max-time",
        "30",
        "-X",
        "POST",
        f"{base_url}{request.path}",
        "-H",
        "X-API-Key: standalone",
        "-H",
        "Content-Type: application/json",
        "-d",
        json.dumps(request.payload, separators=(",", ":")),
    )
    completed = subprocess.run(argv, check=False, capture_output=True, text=True)
    if completed.returncode:
        detail = (completed.stdout or completed.stderr).strip()
        raise QuickstartError(f"{request.path} curl failed: {detail[:500]}")
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise QuickstartError(
            f"{request.path} did not return JSON: {completed.stdout[:500]!r}"
        ) from exc
    if not isinstance(response, dict):
        raise QuickstartError(f"{request.path} returned a non-object JSON response")
    return response


def assert_round_trip(
    write_response: dict[str, Any], search_response: dict[str, Any]
) -> None:
    memory_id = write_response.get("id")
    if not isinstance(memory_id, str) or not memory_id:
        raise QuickstartError(f"write response has no memory id: {write_response!r}")
    title = write_response.get("title")
    if not isinstance(title, str) or not title.strip():
        raise QuickstartError(
            f"write response has no generated title: {write_response!r}"
        )

    items = search_response.get("items")
    if not isinstance(items, list):
        raise QuickstartError(f"search response has no items list: {search_response!r}")
    if not any(
        isinstance(item, dict) and item.get("id") == memory_id for item in items
    ):
        raise QuickstartError(
            "search did not return the memory created by the preceding curl"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help="localhost origin to use while retaining the documented request paths",
    )
    args = parser.parse_args()
    try:
        base_url = validate_base_url(args.base_url)
        write, search = load_requests()
        write_response = run_curl(write, base_url)
        search_response = run_curl(search, base_url)
        assert_round_trip(write_response, search_response)
    except (OSError, QuickstartError) as exc:
        print(f"README quickstart check failed: {exc}", file=sys.stderr)
        return 1

    print("README keyless quickstart wrote and found its memory")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
