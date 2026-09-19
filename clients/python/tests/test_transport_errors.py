"""Transport failures belong to the SDK error family on every request path."""

import httpx
import pytest

from caura_client import Caura, CauraError, TransportError


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadError, httpx.ReadTimeout])
@pytest.mark.parametrize(
    "method, args, kwargs",
    [
        ("write", ("hello",), {}),
        ("search", ("query",), {}),
        ("recall", ("query",), {}),
        ("health", (), {}),
        ("get_document", ("doc-1",), {"collection": "interviews"}),
        (
            "submit_interview",
            (),
            {"node_id": "n1", "agent_id": "a1", "cursor_from": 0, "cursor_to": 1, "events": []},
        ),
    ],
)
def test_transport_failure_is_a_client_error(error_type, method, args, kwargs):
    cause = error_type("connection failed")
    calls = []

    def handler(request):
        calls.append(request)
        raise cause

    with Caura("test-key", tenant_id="t1", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CauraError) as caught:
            getattr(client, method)(*args, **kwargs)

    assert isinstance(caught.value, TransportError)
    assert caught.value.__cause__ is cause
    assert "connection failed" in str(caught.value)
    assert len(calls) == 1


def test_transport_mapping_does_not_wrap_serialization_errors():
    def handler(request):
        pytest.fail("an unserializable request must not reach the transport")

    with Caura("test-key", tenant_id="t1", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TypeError):
            client.write("hello", metadata={"invalid": object()})


def test_transport_mapping_does_not_wrap_invalid_json():
    def handler(request):
        return httpx.Response(200, content=b"not json")

    with Caura("test-key", tenant_id="t1", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            client.health()
