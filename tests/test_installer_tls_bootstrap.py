"""The plugin installer verifies TLS unless it is explicitly told not to.

The script served at ``/api/v1/install-plugin`` downloads its manifest (sending
the API key), then the plugin source it builds and runs, then a certificate it
trusts for every Node process the user starts. Until this change it did all
three with ``curl -k`` whenever the API URL was ``https://`` -- caura.ai
included, whose certificate is valid -- so anyone able to intercept an install
got the key, code execution, and a trust anchor that outlives the install.

``?tls_bootstrap=tofu`` keeps the old behaviour for the one case that needs it:
an on-prem server with a self-signed certificate the machine does not trust yet.

The unit tests run the script's own TLS sections in bash with ``curl`` replaced
by a recorder, so they check what the shell does rather than how it reads.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from core_api.routes.plugin import TlsBootstrap, _generate_install_script
from tests.conftest import get_test_auth

HTTPS = "https://caura.example.com"

# ``curl`` records its arguments (one line per call, fields split by \x1f) and,
# asked to save a file, saves something shaped like a certificate -- enough
# for step 8 to take its install branch. ``systemctl`` is a no-op.
_STUBS = r"""
curl() {
  printf '%s\037' "$@" >> "$CURL_LOG"; printf '\n' >> "$CURL_LOG"
  _out=""; _prev=""
  for _a in "$@"; do
    [ "$_prev" = "-o" ] && _out="$_a"
    _prev="$_a"
  done
  if [ -n "$_out" ] && [ "$_out" != /dev/null ]; then
    printf '%s\n' '-----BEGIN CERTIFICATE-----' 'MIIB' '-----END CERTIFICATE-----' > "$_out"
  fi
  return "${CURL_RC:-0}"
}
systemctl() { :; }
"""


def _script(tls_bootstrap: TlsBootstrap, api_url: str = HTTPS) -> str:
    return _generate_install_script(
        api_url=api_url,
        api_key="sk-test-key-1234",
        fleet_id="my-fleet",
        tenant_id="",
        node_name="node-alpha",
        tls_bootstrap=tls_bootstrap,
    )


def _between(script: str, start: str, end: str) -> str:
    i = script.index(start)
    return script[i : script.index(end, i)]


def _run(script: str, tmp_path: Path, curl_rc: int = 0):
    """Run the script's variable block, its TLS decision and step 8."""
    log = tmp_path / "curl.log"
    body = "\n".join(
        [
            "set -euo pipefail",
            _STUBS,
            _between(script, "CAURA_API_URL=", "\n\n"),
            _between(script, 'PLUGIN_DIR="$HOME', "# 1. Create directory structure"),
            'echo "CURL_INSECURE=[$CURL_INSECURE]"',
            'mkdir -p "$PLUGIN_DIR"',
            _between(
                script,
                "# 8. TLS trust bootstrap",
                'echo "=== Installation complete ==="',
            ),
        ]
    )
    proc = subprocess.run(
        ["bash", "-c", body],
        capture_output=True,
        text=True,
        env={
            "HOME": str(tmp_path),
            "PATH": os.environ["PATH"],
            "CURL_LOG": str(log),
            "CURL_RC": str(curl_rc),
        },
    )
    calls = (
        [line.split("\x1f")[:-1] for line in log.read_text().splitlines()]
        if log.exists()
        else []
    )
    return proc, calls


def _skips_verification(args: list[str]) -> bool:
    return any(
        a in ("-k", "--insecure") or (a[:1] == "-" and a[1:2] != "-" and "k" in a[1:])
        for a in args
    )


def test_by_default_no_download_skips_certificate_verification(tmp_path):
    proc, calls = _run(_script("verify"), tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "CURL_INSECURE=[]" in proc.stdout
    assert any(f"{HTTPS}/onprem-ca.pem" in c for c in calls), calls
    assert not [c for c in calls if _skips_verification(c)]


@pytest.mark.parametrize("curl_rc", [60, 51])
def test_by_default_an_untrusted_certificate_stops_the_install(tmp_path, curl_rc):
    """curl exit 60 is a failed verification (51 for a name mismatch before
    curl 7.62). Stop there, say what to do, and never reach step 8, which
    would trust whatever it downloads."""
    proc, _ = _run(_script("verify"), tmp_path, curl_rc=curl_rc)
    assert proc.returncode == 1
    assert "does not trust" in proc.stdout
    assert "?tls_bootstrap=tofu" in proc.stdout
    assert "[8/8]" not in proc.stdout


def test_an_unreachable_server_does_not_stop_at_the_certificate_check(tmp_path):
    """Only a certificate failure stops the install there; anything else (here
    curl exit 28, a timeout) falls through to the downloads' own errors."""
    proc, calls = _run(_script("verify"), tmp_path, curl_rc=28)
    assert "does not trust" not in proc.stdout
    assert "--max-time" in calls[0]


def test_tofu_skips_verification_and_says_so(tmp_path):
    proc, calls = _run(_script("tofu"), tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "CURL_INSECURE=[-k]" in proc.stdout
    assert "NOT verified" in proc.stdout
    ca_fetch = [c for c in calls if f"{HTTPS}/onprem-ca.pem" in c]
    assert ca_fetch and all(_skips_verification(c) for c in ca_fetch)


@pytest.mark.parametrize("mode", ["verify", "tofu"])
def test_plain_http_makes_no_tls_calls(tmp_path, mode: TlsBootstrap):
    proc, calls = _run(_script(mode, api_url="http://localhost:8000"), tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "CURL_INSECURE=[]" in proc.stdout
    assert calls == []


@pytest.mark.parametrize("mode", ["verify", "tofu"])
def test_no_curl_hardcodes_skipping_verification(mode: TlsBootstrap):
    """Every download takes its TLS mode from ``$CURL_INSECURE``; a literal
    ``-k`` on any curl line would bypass the choice this change introduces."""
    for line in _script(mode).splitlines():
        code = line.split("#", 1)[0]
        if "curl " in code:
            assert not _skips_verification(code.split()), line


# -- The endpoint: the mode is a query parameter on GET and POST alike. ------


@pytest.mark.integration
@pytest.mark.parametrize("method", ["get", "post"])
async def test_the_endpoint_verifies_by_default(client, method):
    _, headers = get_test_auth()
    resp = await client.request(
        method.upper(),
        "/api/v1/install-plugin",
        headers=headers,
        **(
            {"json": {"api_url": HTTPS}}
            if method == "post"
            else {"params": {"api_url": HTTPS}}
        ),
    )
    assert resp.status_code == 200
    assert "\nTLS_BOOTSTRAP=verify\n" in resp.text


@pytest.mark.integration
@pytest.mark.parametrize("path", ["/api/v1/install-plugin", "/api/install-plugin"])
@pytest.mark.parametrize("method", ["get", "post"])
async def test_tofu_is_asked_for_in_the_query_string(client, method, path):
    """A query parameter even on POST: the body model forbids unknown fields,
    so a body field would be refused by every server that predates it."""
    _, headers = get_test_auth()
    resp = await client.request(
        method.upper(),
        path,
        params={"tls_bootstrap": "tofu"},
        headers=headers,
        **({"json": {"api_url": HTTPS}} if method == "post" else {}),
    )
    assert resp.status_code == 200
    assert "\nTLS_BOOTSTRAP=tofu\n" in resp.text


@pytest.mark.integration
async def test_an_unknown_mode_is_refused(client):
    _, headers = get_test_auth()
    resp = await client.get(
        "/api/v1/install-plugin", params={"tls_bootstrap": "off"}, headers=headers
    )
    assert resp.status_code == 422
