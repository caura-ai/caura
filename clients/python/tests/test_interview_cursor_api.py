"""Owned cursor transport: only an authorized response may mean no stream."""

from __future__ import annotations

import json

import httpx
import pytest

from caura_client import Caura, CauraAPIError, TransportError
from caura_client.interviewer.discovery import Transcript
from caura_client.interviewer.runner import RunConfig, read_watermark, run_all

NODE = "cc:machine:session"


def client(handler):
    return Caura(
        "cursor-test-fixture",
        tenant_id="cursor-tenant",
        agent_id="asserted-agent-label",
        base_url="https://cursor.test",
        transport=httpx.MockTransport(handler),
        retries=0,
    )


@pytest.mark.parametrize("last_seq", [-1, 0, 23])
def test_cursor_client_uses_owned_post_without_asserted_agent_proof(last_seq):
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/api/v1/interview/cursor"
        assert json.loads(request.content) == {"node_id": NODE}
        return httpx.Response(200, json={"last_seq": last_seq})

    with client(handler) as mc:
        assert mc.get_interview_cursor(NODE) == last_seq


def test_cursor_client_can_name_a_tenant_for_system_admin_calls():
    def handler(request):
        assert json.loads(request.content) == {"node_id": NODE, "tenant_id": "target-tenant"}
        return httpx.Response(200, json={"last_seq": 8})

    with client(handler) as mc:
        assert mc.get_interview_cursor(NODE, tenant_id="target-tenant") == 8


@pytest.mark.parametrize("last_seq", [-1, 0, 23])
def test_runner_uses_cursor_endpoint_and_never_generic_document_fallback(last_seq):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/api/v1/interview/cursor":
            return httpx.Response(200, json={"last_seq": last_seq})
        # A tempting legacy answer must never be read by the migrated runner.
        return httpx.Response(200, json={"data": {"last_seq": 999}})

    with client(handler) as mc:
        assert read_watermark(mc, NODE) == last_seq
    assert calls == ["/api/v1/interview/cursor"]


@pytest.mark.parametrize("status", [401, 403, 404, 409, 429, 500, 503])
def test_cursor_http_errors_propagate_instead_of_resetting_the_runner(status):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/api/v1/interview/cursor":
            return httpx.Response(status, json={"detail": "cursor unavailable"})
        return httpx.Response(200, json={"data": {"last_seq": -1}})

    with client(handler) as mc, pytest.raises(CauraAPIError) as err:
        read_watermark(mc, NODE)
    assert err.value.status_code == status
    assert calls == ["/api/v1/interview/cursor"]


@pytest.mark.parametrize(
    "payload",
    [{}, {"last_seq": None}, {"last_seq": True}, {"last_seq": -2}, {"last_seq": "7"}, {"last_seq": 1.5}],
)
def test_malformed_cursor_response_is_an_error_not_an_empty_stream(payload):
    def handler(request):
        if request.url.path == "/api/v1/interview/cursor":
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"data": {}})

    with client(handler) as mc, pytest.raises(CauraAPIError):
        read_watermark(mc, NODE)


def test_cursor_transport_failure_has_no_legacy_fallback():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/api/v1/interview/cursor":
            raise httpx.ConnectError("synthetic disconnect", request=request)
        return httpx.Response(200, json={"data": {"last_seq": -1}})

    with client(handler) as mc, pytest.raises(TransportError):
        read_watermark(mc, NODE)
    assert calls == ["/api/v1/interview/cursor"]


def test_forbidden_cursor_aborts_with_actionable_credential_guidance(tmp_path, capsys):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(403, json={"detail": "cursor unavailable"})

    transcripts = [Transcript(tmp_path / name, "project") for name in ["first.jsonl", "second.jsonl"]]
    with client(handler) as mc:
        summary = run_all(mc, transcripts, RunConfig(agent_id="asserted", machine12="machine"))
    assert summary.aborted
    assert calls == ["/api/v1/interview/cursor"]
    message = capsys.readouterr().err
    assert "Cursor read refused" in message
    assert "owning agent credential" in message
    assert "Install credentials" in message
    assert "--agent-id" in message
