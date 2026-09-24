"""ax-0917-l-22 — one response, two different ``X-RateLimit-Limit`` values.

Two unrelated layers wrote the same two header names onto the same response:

* ``middleware/rate_limit.py`` (slowapi, ``headers_enabled=True``) publishes
  the PER-SECOND THROTTLE as ``X-RateLimit-Limit``/``-Remaining``/``-Reset``.
  That is the meaning ``README.md`` and ``docs/api-reference.md`` document,
  and the one a client backs off on.
* ``routes/memories.py`` published the PER-PERIOD PLAN QUOTA under the same
  first two names.

slowapi appends rather than sets, so a metered route answered with both:
``x-ratelimit-limit: None`` followed by ``x-ratelimit-limit: 10``. Clients
join repeated headers with a comma (httpx, requests and fetch all do), so
what a caller actually read back was ``"None, 10"`` — ``int()`` raises on it,
and the back-off signal is unusable on exactly the two routes most likely to
be throttled.

The ``None`` half is its own defect: ``str(usage.get("limit", "unlimited"))``
looks like it falls back, but ``UsageCheckResult.get`` is
``getattr(self, key, default)`` and the field EXISTS holding ``None``, so the
default is unreachable and every unmetered response advertised the literal
string ``"None"`` as its limit.

These tests assert on the wire: how many times a header occurs and what its
value is. Counting matters — a single-value assertion passes on the broken
response too, because httpx hands back the joined string only when you ask
for the joined string.
"""

from __future__ import annotations

import uuid

import pytest

from core_api.middleware.rate_limit import limiter
from core_api.services.hooks import ServiceHooks, configure_hooks
from core_api.services.usage_service import UsageCheckResult

pytestmark = pytest.mark.asyncio


@pytest.fixture
def enabled_limiter():
    """The session fixture disables the limiter; without it slowapi injects
    nothing and the collision under test cannot occur."""
    prev = limiter.enabled
    limiter.enabled = True
    yield
    limiter.enabled = prev
    limiter.reset()


@pytest.fixture
def wired_meter():
    """A platform-style usage meter that reports real counters.

    OSS standalone wires none, which is the ``None``-valued case; this is the
    case where the quota genuinely has numbers to publish, and so the one that
    collided with a real throttle value rather than with a placeholder.
    """
    from core_api.services.audit_service import log_action

    async def _meter(*, tenant_id: str, operation: str, count: int):
        return UsageCheckResult(
            allowed=True, operation=operation, limit=100, remaining=93
        )

    configure_hooks(ServiceHooks(audit_log=log_action, usage_meter=_meter))
    yield
    # Back to what the autouse ``_reset_hooks`` fixture installs.
    configure_hooks(ServiceHooks(audit_log=log_action))


def _occurrences(response, name: str) -> list[str]:
    """Every value sent under ``name``, unjoined."""
    return [v for k, v in response.headers.multi_items() if k.lower() == name.lower()]


async def _write(client, headers):
    return await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": "default",
            "agent_id": "l22-header-agent",
            "memory_type": "fact",
            "content": f"l22 header probe {uuid.uuid4().hex}",
        },
        headers=headers,
    )


async def _search(client, headers):
    return await client.post(
        "/api/v1/search",
        json={"tenant_id": "default", "query": "l22 header probe"},
        headers=headers,
    )


@pytest.mark.parametrize("call", [_write, _search], ids=["write", "search"])
async def test_throttle_headers_are_sent_once_with_a_numeric_value(
    client, enabled_limiter, wired_meter, call
):
    resp = await call(client, {"x-api-key": f"mc_l22_once_{uuid.uuid4().hex[:8]}"})
    assert resp.status_code in (200, 201), resp.text

    for name in ("x-ratelimit-limit", "x-ratelimit-remaining"):
        sent = _occurrences(resp, name)
        assert len(sent) == 1, f"{name} sent {len(sent)}x: {sent}"
        assert sent[0].isdigit(), f"{name} is not an integer: {sent[0]!r}"

    # What a client actually parses. Pre-fix this was "None, 10".
    assert int(resp.headers["x-ratelimit-limit"]) > 0


@pytest.mark.parametrize("call", [_write, _search], ids=["write", "search"])
async def test_no_header_value_is_the_string_None(client, enabled_limiter, call):
    """No meter wired (the OSS default) — the quota has nothing to report, so
    it must report nothing rather than stringifying its unset ``None``."""
    resp = await call(client, {"x-api-key": f"mc_l22_none_{uuid.uuid4().hex[:8]}"})
    assert resp.status_code in (200, 201), resp.text

    offenders = [(k, v) for k, v in resp.headers.multi_items() if v == "None"]
    assert offenders == [], f"headers advertising the literal 'None': {offenders}"

    # Specifically: absence, not a placeholder like "unlimited".
    assert _occurrences(resp, "x-usage-limit") == []
    assert _occurrences(resp, "x-usage-remaining") == []


@pytest.mark.parametrize("call", [_write, _search], ids=["write", "search"])
async def test_quota_is_published_under_its_own_names(
    client, enabled_limiter, wired_meter, call
):
    """The quota signal survives the rename — it just no longer squats on the
    throttle's names."""
    resp = await call(client, {"x-api-key": f"mc_l22_quota_{uuid.uuid4().hex[:8]}"})
    assert resp.status_code in (200, 201), resp.text

    assert _occurrences(resp, "x-usage-limit") == ["100"]
    assert _occurrences(resp, "x-usage-remaining") == ["93"]


async def test_bulk_write_quota_headers_reach_the_client(client, wired_meter):
    """The bulk route RETURNS a Response, and FastAPI drops the injected
    ``response`` param's headers in that case — so the quota headers it set
    there never left the process."""
    tag = uuid.uuid4().hex[:8]
    resp = await client.post(
        "/api/v1/memories/bulk",
        json={
            "tenant_id": "default",
            "agent_id": f"l22-bulk-{tag}",
            "items": [{"content": f"l22 bulk header probe {tag}"}],
        },
        headers={
            "x-api-key": f"mc_l22_bulk_{tag}",
            "X-Bulk-Attempt-Id": f"l22-bulk-{tag}",
        },
    )
    assert resp.status_code == 200, resp.text

    assert _occurrences(resp, "x-usage-limit") == ["100"]
    assert _occurrences(resp, "x-usage-remaining") == ["93"]


async def test_every_security_header_is_sent_exactly_once(client, enabled_limiter):
    """The other half of the report — "several security and rate-limit headers
    duplicated" — checked rather than assumed.

    This one PASSES pre-fix, and is kept as a pin rather than as evidence of a
    bug: ``SecurityHeadersMiddleware`` strips its own keys from the downstream
    message before re-adding them, so it cannot double up the way slowapi's
    ``append`` did. Losing that strip is the regression this catches.
    """
    from core_api.app import _SECURITY_HEADERS

    resp = await _write(client, {"x-api-key": f"mc_l22_sec_{uuid.uuid4().hex[:8]}"})
    assert resp.status_code == 201, resp.text

    duplicated = {
        name: _occurrences(resp, name)
        for name in _SECURITY_HEADERS
        if len(_occurrences(resp, name)) != 1
    }
    assert duplicated == {}, f"security headers not sent exactly once: {duplicated}"

    # Same for the two throttle headers slowapi sets rather than appends.
    for name in ("x-ratelimit-reset", "retry-after"):
        assert len(_occurrences(resp, name)) == 1, _occurrences(resp, name)
