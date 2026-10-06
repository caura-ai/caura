"""M-50: an LLM outage must not pass for a synthesized interview window.

``_interview_chunk`` gave ``call_with_fallback`` a ``fake_fn`` that returns a
stub report, one "LLM unavailable; unsynthesized" episode, whatever the tenant's
provider. ``call_with_fallback`` calls ``fake_fn`` when every provider failed,
and straight away for ``none``. The stub went through the bulk write like a real
report, so the window counted as synthesized. On the default async path the job
was marked ``done``, which is terminal, and its stored events were never
synthesized; inline, the watermark moved past the window.

Only a deliberately configured ``fake`` provider (dev and CI) still gets the
stub: the line ``deliberate_fake_provider`` draws for the crystallizer, evolve
and insights.
"""

import pytest

import core_api.routes.interview as interview_route
import core_api.services.interview_service as interview_service
from core_api.providers import _retry
from core_api.services.interview_service import (
    enqueue_interview_job,
    process_interview_job,
)
from tests.conftest import get_test_auth, new_tenant_id, uid
from tests.test_api_interview import _node, _payload
from tests.test_interview_async_submit import (
    _enqueue_kwargs,
    _interviewer_memories,
    _job_doc,
)


@pytest.fixture
def every_provider_fails(monkeypatch):
    """``call_with_fallback`` ends in ``fake_fn``, as when every provider failed.

    Patched where ``_interview_chunk`` imports it, so no provider is contacted.
    For ``none`` and ``fake`` the real chain calls ``fake_fn`` straight away too.
    """

    async def _fall_through(*, fake_fn, **_kw):
        return fake_fn()

    monkeypatch.setattr(_retry, "call_with_fallback", _fall_through)


async def _tenant(client, provider: str) -> tuple[str, dict]:
    """A tenant with the interviewer on, synthesizing with ``provider``.

    Named explicitly: the suite's default provider is ``none`` in CI and
    ``fake`` locally.
    """
    tenant_id, headers = get_test_auth(new_tenant_id())
    resp = await client.put(
        f"/api/v1/settings?tenant_id={tenant_id}",
        json={"interviewer": {"enabled": True}, "enrichment": {"provider": provider}},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return tenant_id, headers


@pytest.mark.parametrize("provider", ["openai", "none"])
async def test_no_llm_leaves_the_async_job_pending(
    client, every_provider_fails, provider
):
    """The default path. ``done`` is terminal: the sweep never re-synthesizes
    it, so the window's stored events would never become memories."""
    tenant_id, headers = await _tenant(client, provider)
    node_id, agent_id = f"node-{uid()}", f"agent-{uid()}"
    doc_id = await enqueue_interview_job(
        **_enqueue_kwargs(tenant_id, node_id, agent_id)
    )

    result = await process_interview_job(tenant_id, doc_id)

    assert result is not None and result["status"] == "failed", result
    job = (await _job_doc(tenant_id, doc_id))["data"]
    assert job["status"] == "pending"
    assert job["events"], "the masked window stays for the retry"
    assert await _interviewer_memories(client, tenant_id, agent_id, headers) == []


@pytest.mark.parametrize("provider", ["openai", "none"])
async def test_no_llm_fails_the_inline_submit_and_keeps_the_watermark(
    client, monkeypatch, every_provider_fails, provider
):
    """The legacy inline path: a 500 keeps the plugin's buffer for the next
    tick, as for a bulk write that failed."""
    monkeypatch.setattr(interview_route.app_settings, "interview_async_submit", False)
    tenant_id, headers = await _tenant(client, provider)
    node_id, agent_id = await _node(client, tenant_id, headers), f"agent-{uid()}"

    resp = await client.post(
        "/api/v1/interview/submit",
        json=_payload(tenant_id, node_id, agent_id),
        headers=headers,
    )

    assert resp.status_code == 500, resp.text
    assert await interview_service.read_watermark(tenant_id, node_id) == -1
    assert await _interviewer_memories(client, tenant_id, agent_id, headers) == []


async def test_a_deliberate_fake_provider_still_gets_the_stub(
    client, every_provider_fails
):
    """Control. In dev and CI the stub is the intent, and the only way the
    interview runs end to end without a key."""
    tenant_id, headers = await _tenant(client, "fake")
    node_id, agent_id = f"node-{uid()}", f"agent-{uid()}"
    doc_id = await enqueue_interview_job(
        **_enqueue_kwargs(tenant_id, node_id, agent_id)
    )

    result = await process_interview_job(tenant_id, doc_id)

    assert result is not None and result["status"] == "committed", result
    assert (await _job_doc(tenant_id, doc_id))["data"]["status"] == "done"
    rows = await _interviewer_memories(client, tenant_id, agent_id, headers)
    assert len(rows) == 1
    assert "unsynthesized" in rows[0]["content"]
