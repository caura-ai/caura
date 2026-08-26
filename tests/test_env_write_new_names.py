"""Phase 5.2: the generated installer writes the new env names, and only those.

New installs get ``CAURA_*`` alone. That is safe because every reader accepts
both spellings (5.1), so nothing we ship needs the old name in a file we are
creating from scratch — and rule 7 says not to mint one.

Existing installs are untouched: their ``.env`` keeps the old names and keeps
working. This file pins the *new-file* contract, not a migration.
"""

import re

import pytest

pytestmark = pytest.mark.integration

# The connection identity the installer persists. Suffixes only — each is
# checked under both prefixes below.
CONNECTION_KEYS = ("API_URL", "API_KEY", "FLEET_ID", "TENANT_ID", "NODE_NAME")


def _env_block(script: str) -> str:
    """The body of the ``.env`` heredoc the install script writes.

    Scoped deliberately: the script still carries the old brand elsewhere — in
    the plugin's on-disk install path, which is floor-class, and in product
    prose — so a whole-script search would fail for reasons that have nothing
    to do with the env contract.
    """
    match = re.search(r"cat > \"\$PLUGIN_DIR/\.env\" << ENV_EOF\n(.*?)\nENV_EOF", script, re.S)
    assert match, "could not locate the .env heredoc in the generated install script"
    return match.group(1)


async def _script(client) -> str:
    resp = await client.post(
        "/api/v1/install-plugin",
        # No ``tenant_id``: ``InstallPluginRequest`` has never declared one — the
        # route resolves the tenant from the credential (``_resolve_tenant_id``),
        # so this key was accepted and discarded on every call, and the endpoint
        # now rejects it (SAFE-01). The ``CAURA_TENANT_ID=`` line the tests below
        # assert on comes from that resolution, not from the request body.
        json={"fleet_id": "f1", "api_key": "mc_testkey123456"},
    )
    assert resp.status_code == 200
    return resp.text


async def test_env_block_writes_every_connection_key_under_the_new_name(client):
    block = _env_block(await _script(client))
    for suffix in CONNECTION_KEYS:
        assert f"CAURA_{suffix}=" in block, f"CAURA_{suffix} must be written to a new .env"


async def test_env_block_writes_no_old_names(client):
    block = _env_block(await _script(client))
    for suffix in CONNECTION_KEYS:
        old = f"MEMCLAW_{suffix}="  # legacy-name-ok: rule 3 — asserts a NEW file does not mint it
        assert old not in block, (
            f"{old} must not appear in a newly written .env — readers accept both "
            "spellings, so writing the old one only mints a name to carry forever"
        )
