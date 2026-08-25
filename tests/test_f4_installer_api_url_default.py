"""The published install one-liner must work as published — F4 / AX-03.

``curl https://caura.ai/api/v1/install-plugin | bash`` is the documented cloud
install. It could not work: the generated script hardcoded
``CAURA_API_URL=http://localhost:8000`` unless the caller appended an
``?api_url=`` that no published copy of the command mentions. So the script
installed a plugin pointed at port 8000 on the installing machine — the one
place the API certainly is not — and the failure surfaced later as a heartbeat
that never arrived, not as an install error.

The sibling ``/install-skill`` endpoint already derived the URL from the
request. These tests pin that ``/install-plugin`` does the same, and that the
proxy case works, since in production the app never sees the public host on
``request.url`` — only in ``X-Forwarded-*``.
"""

from __future__ import annotations

import shlex

import pytest

pytestmark = pytest.mark.integration

CLOUD_HEADERS = {"x-forwarded-proto": "https", "x-forwarded-host": "caura.ai"}


def assignment(url: str) -> str:
    """The exact line the script carries.

    Built with ``shlex.quote`` like the generator, which only adds quotes when
    the value needs them — so a hand-written literal matches for one URL and
    silently not for another.
    """
    return f"CAURA_API_URL={shlex.quote(url)}"


async def test_the_published_cloud_one_liner_produces_a_cloud_url(client) -> None:
    """The exact shape of the documented command: no api_url, behind a proxy."""
    resp = await client.get("/api/v1/install-plugin", headers=CLOUD_HEADERS)
    assert resp.status_code == 200, resp.text
    script = resp.text
    assert assignment("https://caura.ai") in script, script[:400]
    # The specific regression: a cloud install must never be told to talk to
    # the installing machine's own port 8000. Asserted on the ASSIGNMENT, not on
    # the string anywhere — the script legitimately mentions localhost in a
    # comment about OSS installs, and a blanket check would fail on prose.
    assert assignment("http://localhost:8000") not in script


async def test_the_post_variant_derives_it_too(client) -> None:
    """POST is the preferred form (no secrets in the URL) and had the same default."""
    resp = await client.post("/api/v1/install-plugin", json={}, headers=CLOUD_HEADERS)
    assert resp.status_code == 200, resp.text
    assert assignment("https://caura.ai") in resp.text
    assert assignment("http://localhost:8000") not in resp.text


@pytest.mark.parametrize("method", ["get", "post"])
async def test_an_explicit_api_url_still_wins(client, method: str) -> None:
    """Deriving is a default, not an override — the escape hatch that made the
    cloud install possible at all must keep working."""
    override = "https://caura.example.internal"
    if method == "get":
        resp = await client.get(
            f"/api/v1/install-plugin?api_url={override}", headers=CLOUD_HEADERS
        )
    else:
        resp = await client.post(
            "/api/v1/install-plugin", json={"api_url": override}, headers=CLOUD_HEADERS
        )
    assert resp.status_code == 200, resp.text
    assert assignment(override) in resp.text


async def test_a_self_hosted_install_still_points_at_itself(client) -> None:
    """The old default was right for exactly one caller — someone running the
    API locally. Deriving from the request keeps that caller working, which is
    why this is a fix rather than a trade."""
    resp = await client.get(
        "/api/v1/install-plugin", headers={"host": "localhost:8000"}
    )
    assert resp.status_code == 200, resp.text
    assert assignment("http://localhost:8000") in resp.text
