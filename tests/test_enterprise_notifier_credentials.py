"""Notifier admission and payload contracts; synthetic CLI, no live dispatch."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github/workflows/notify-enterprise-staging.yml"
)


def workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text())


def test_notifier_key_has_a_main_only_workflow_entry_point() -> None:
    document = workflow()
    triggers = document.get("on", document.get(True))
    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"]["branches"] == ["main"]
    job = document["jobs"]["dispatch"]
    assert job["environment"] == "enterprise-staging-notifier"
    assert document["permissions"] == {"contents": "read"}
    assert job["timeout-minutes"] == 5
    assert job["steps"][0]["name"] == "Require trusted OSS main source"
    assert not any("checkout@" in step.get("uses", "") for step in job["steps"])
    assert "DEPLOY_APP" not in WORKFLOW.read_text()


def test_notifier_can_request_only_enterprise_contents_write() -> None:
    steps = workflow()["jobs"]["dispatch"]["steps"]
    mint = next(step for step in steps if step.get("id") == "app-token")
    assert mint["with"] == {
        "app-id": "${{ secrets.ENTERPRISE_NOTIFIER_APP_ID }}",
        "private-key": "${{ secrets.ENTERPRISE_NOTIFIER_APP_PRIVATE_KEY }}",
        "owner": "caura-ai",
        "repositories": "caura-enterprise",
        "permission-contents": "write",
    }
    assert "skip-token-revoke" not in mint["with"]
    assert steps.index(mint) == 1
    dispatch = steps[2]
    assert dispatch["env"] == {
        "GH_TOKEN": "${{ steps.app-token.outputs.token }}",
        "OSS_REF": "${{ inputs.oss_ref || github.sha }}",
        "TRIGGERED_BY": "${{ github.actor }}",
        "EVENT_NAME": "${{ github.event_name }}",
    }
    assert "${{" not in dispatch["run"]
    assert len(steps) == 3


@pytest.mark.parametrize(
    ("override", "accepted"),
    [
        ({}, True),
        ({"GITHUB_EVENT_NAME": "workflow_dispatch"}, True),
        ({"GITHUB_REF": "refs/heads/feature"}, False),
        ({"GITHUB_REF": "refs/tags/main"}, False),
        ({"GITHUB_EVENT_NAME": "pull_request_target"}, False),
        ({"GITHUB_REPOSITORY": "attacker/caura"}, False),
        ({"REPOSITORY_ID": "1"}, False),
        ({"OWNER_ID": "1"}, False),
    ],
)
def test_source_guard_rejects_unreviewed_refs(
    override: dict[str, str], accepted: bool
) -> None:
    guard = workflow()["jobs"]["dispatch"]["steps"][0]["run"]
    env = dict(
        os.environ,
        GITHUB_REPOSITORY="caura-ai/caura",
        REPOSITORY_ID="1222639711",
        OWNER_ID="229147182",
        GITHUB_REF="refs/heads/main",
        GITHUB_EVENT_NAME="push",
    )
    env.update(override)
    result = subprocess.run(["bash", "-c", guard], env=env, check=False)
    assert (result.returncode == 0) is accepted


@pytest.mark.parametrize("failures", [0, 1, 3])
def test_payload_is_data_and_retry_exhaustion_fails(
    tmp_path: Path, failures: int
) -> None:
    # Execute only against a fake gh/sleep. The input contains command syntax
    # and a newline: it must remain one argument and never execute.
    binary = tmp_path / "bin"
    binary.mkdir()
    capture = tmp_path / "calls.json"
    marker = tmp_path / "must-not-exist"
    payload = f'$(touch "{marker}")\nref-with-special-characters'
    fake = binary / "gh"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "p = pathlib.Path(os.environ['CAPTURE'])\n"
        "calls = json.loads(p.read_text()) if p.exists() else []\n"
        "calls.append(sys.argv[1:])\n"
        "p.write_text(json.dumps(calls))\n"
        "sys.exit(1 if len(calls) <= int(os.environ['FAILURES']) else 0)\n"
    )
    fake.chmod(0o755)
    sleep = binary / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n")
    sleep.chmod(0o755)
    env = dict(
        os.environ,
        PATH=f"{binary}:{os.environ['PATH']}",
        CAPTURE=str(capture),
        FAILURES=str(failures),
        GH_TOKEN="synthetic-no-real-token",
        OSS_REF=payload,
        TRIGGERED_BY="test-operator",
        EVENT_NAME="workflow_dispatch",
    )
    script = workflow()["jobs"]["dispatch"]["steps"][2]["run"]
    result = subprocess.run(
        ["bash", "-c", script], env=env, check=False, capture_output=True, text=True
    )
    calls = json.loads(capture.read_text())
    assert len(calls) == min(failures + 1, 3)
    assert (result.returncode == 0) is (failures < 3)
    assert not marker.exists()
    for arguments in calls:
        assert arguments == [
            "api",
            "--method",
            "POST",
            "-H",
            "Accept: application/vnd.github+json",
            "/repos/caura-ai/caura-enterprise/dispatches",
            "-f",
            "event_type=oss-main-pushed",
            "-f",
            f"client_payload[oss_ref]={payload}",
            "-f",
            "client_payload[triggered_by]=test-operator",
            "-f",
            "client_payload[event_name]=workflow_dispatch",
        ]
