"""The installer explains the outbound hold without selecting an auth mode."""

import re
import subprocess

import pytest

from core_api.routes.plugin import _generate_install_script

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("existing_mode", [None, "manual"])
def test_installer_prints_auth_mode_notice_without_selecting_mode(
    tmp_path, existing_mode
):
    script = _generate_install_script(
        api_url="http://localhost:8000",
        api_key="",
        fleet_id="fixture-fleet",
        tenant_id="",
        node_name="fixture-node",
        tls_bootstrap="verify",
    )
    # Execute only the real completion output; no downloads or installation.
    completion = script[script.index('echo "=== Installation complete ==="') :]
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "PLUGIN_DIR": str(tmp_path / "plugin"),
        "CAURA_API_URL": "http://localhost:8000",
        "CAURA_FLEET_ID": "fixture-fleet",
    }
    if existing_mode is not None:
        env["CAURA_AUTH_MODE"] = existing_mode
    result = subprocess.run(
        [
            "bash",
            "-eu",
            "-c",
            completion + '\nprintf "MODE_AFTER=%s\\n" "${CAURA_AUTH_MODE-unset}"',
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert f"MODE_AFTER={existing_mode or 'unset'}" in result.stdout
    assert not re.search(r"(?:^|\n)\s*(?:export\s+)?CAURA_AUTH_MODE=", script)
    env_block = re.search(
        r'cat > "\$PLUGIN_DIR/\.env" << ENV_EOF\n(.*?)\nENV_EOF', script, re.S
    )
    assert env_block and "CAURA_AUTH_MODE" not in env_block.group(1)
    assert not list(tmp_path.iterdir()), (
        "completion output must not write configuration"
    )
    notices = [line for line in result.stdout.splitlines() if "CAURA_AUTH_MODE" in line]
    assert len(notices) == 1
    assert "docs/plugin-upgrade.md" in notices[0]
    assert all(mode in notices[0] for mode in ("managed", "standalone", "manual"))
    assert "outbound" in notices[0].lower()
