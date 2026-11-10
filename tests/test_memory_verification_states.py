"""Evidence contract v1 (EC-13/EC-14): verification states and the verify operation.

EC-13, set by the server alone:

- a write that carries a ``tool_call_id`` or a real ``source_uri`` is
  ``tool_observed``; ingest's ``text-input`` placeholder and benchmark schemes
  (``longmemeval:``) are not real sources;
- ``conflicted`` makes a row ``disputed``; a live status undoes that, back to
  ``human_verified`` when a person had verified it, else to what its evidence
  says; ``outdated`` and the rest keep the state;
- a content edit drops a person's verification; a source edit recomputes.

EC-14, the verify operation (REST ``POST /memories/{id}/verify`` and
``/unverify``, MCP ``caura_manage`` op=verify/unverify): a person, a tenant or
admin key, or an agent at ``trust_level >= 3``; no agent may verify a
``disputed`` row; every call is audited.

See ``docs/plans/evidence-contract-v1.md``.
"""

from __future__ import annotations

import uuid

import pytest

from common.models.memory import automatic_verification_status, is_real_source_uri
from core_api.schemas import MemoryCreate
from core_api.services import agent_service
from core_api.services import verification as verification_service
from core_api.services.evidence import evidence_payload
from tests.conftest import get_admin_headers, parse_envelope, uid

# Long enough to clear CheckContentLength's minimum-length quality gate.
_PADDING = (
    " This memory carries enough surrounding context to pass the content-length gate."
)
_FLEET = "fleet-verify"


# ── EC-13: the state a write starts in ─────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    ("source_uri", "real"),
    [
        ("https://example.com/doc", True),
        ("upload:report.pdf", True),
        ("caura-doc:abc", True),
        ("text-input", False),
        ("longmemeval:q12", False),
        ("LoCoMo:conv-3", False),
        ("TEST:fixture-1", False),
        ("lme:q4", False),
        ("Bench:run-7", False),
        ("seed:corpus", False),
        ("benchmark:locomo-10", False),
        ("  text-input ", False),
        ("", False),
        ("   ", False),
        (None, False),
    ],
)
def test_real_source_uri(source_uri, real) -> None:
    assert is_real_source_uri(source_uri) is real


@pytest.mark.unit
@pytest.mark.parametrize(
    ("source_uri", "tool_call_id", "expected"),
    [
        (None, "toolu_01", "tool_observed"),
        ("https://example.com", None, "tool_observed"),
        ("text-input", "toolu_01", "tool_observed"),
        ("text-input", None, "unverified"),
        ("longmemeval:q1", None, "unverified"),
        (None, "  ", "unverified"),
        (None, None, "unverified"),
    ],
)
def test_automatic_verification_status(source_uri, tool_call_id, expected) -> None:
    assert automatic_verification_status(source_uri, tool_call_id) == expected


@pytest.mark.unit
def test_evidence_payload_carries_the_derived_state() -> None:
    body = MemoryCreate(
        tenant_id="t1", agent_id="a1", content="x", source_uri="https://example.com"
    )
    assert evidence_payload(body)["verification_status"] == "tool_observed"
    placeholder = MemoryCreate(
        tenant_id="t1", agent_id="a1", content="x", source_uri="text-input"
    )
    assert evidence_payload(placeholder)["verification_status"] == "unverified"
    # A stored parent row (an atomic-fact fan-out): its source decides.
    assert (
        evidence_payload({"source_uri": "https://example.com", "tool_call_id": None})[
            "verification_status"
        ]
        == "tool_observed"
    )


async def _write(client, tenant_id: str, **fields) -> dict:
    body = {
        "tenant_id": tenant_id,
        "agent_id": fields.pop("agent_id", "writer"),
        "fleet_id": _FLEET,
        "memory_type": "fact",
        "content": f"verification row {uid()}." + _PADDING,
        **fields,
    }
    r = await client.post("/api/v1/memories", json=body, headers=get_admin_headers())
    assert r.status_code in (200, 201), r.text
    return r.json()


async def _row(sc, tenant_id: str, memory_id: str) -> dict:
    row = await sc.get_memory(memory_id, tenant_id)
    assert row is not None
    return row


@pytest.mark.integration
async def test_write_with_a_tool_call_is_tool_observed(client, sc, tenant_id) -> None:
    out = await _write(client, tenant_id, tool_call_id="toolu_write")
    assert out["verification_status"] == "tool_observed"
    assert (await _row(sc, tenant_id, out["id"]))[
        "verification_status"
    ] == "tool_observed"


@pytest.mark.integration
@pytest.mark.parametrize(
    ("source_uri", "expected"),
    [
        ("https://example.com/runbook", "tool_observed"),
        ("text-input", "unverified"),
        ("longmemeval:q7", "unverified"),
    ],
)
async def test_write_with_a_source_uri(
    client, sc, tenant_id, source_uri, expected
) -> None:
    out = await _write(client, tenant_id, source_uri=source_uri)
    assert (await _row(sc, tenant_id, out["id"]))["verification_status"] == expected


@pytest.mark.integration
async def test_write_without_evidence_is_unverified(client, sc, tenant_id) -> None:
    out = await _write(client, tenant_id)
    assert (await _row(sc, tenant_id, out["id"]))["verification_status"] == "unverified"


@pytest.mark.integration
async def test_bulk_items_get_their_own_state(client, sc, tenant_id) -> None:
    r = await client.post(
        "/api/v1/memories/bulk",
        json={
            "tenant_id": tenant_id,
            "agent_id": "writer",
            "items": [
                {"content": f"bulk tool {uid()}." + _PADDING, "tool_call_id": "call_9"},
                {"content": f"bulk plain {uid()}." + _PADDING},
            ],
        },
        headers={**get_admin_headers(), "X-Bulk-Attempt-Id": str(uuid.uuid4())},
    )
    assert r.status_code in (200, 201, 207), r.text
    ids = [res["id"] for res in r.json()["results"]]
    states = [(await _row(sc, tenant_id, i))["verification_status"] for i in ids]
    assert states == ["tool_observed", "unverified"]


# ── EC-13: status changes ──────────────────────────────────────────────────


@pytest.mark.integration
async def test_conflicted_disputes_and_active_restores_the_evidence_state(
    client, sc, tenant_id
) -> None:
    out = await _write(client, tenant_id, tool_call_id="toolu_c")
    await sc.update_memory_status(out["id"], "conflicted", tenant_id=tenant_id)
    assert (await _row(sc, tenant_id, out["id"]))["verification_status"] == "disputed"
    await sc.update_memory_status(out["id"], "active", tenant_id=tenant_id)
    assert (await _row(sc, tenant_id, out["id"]))[
        "verification_status"
    ] == "tool_observed"


@pytest.mark.integration
async def test_an_unverified_row_comes_back_unverified(client, sc, tenant_id) -> None:
    out = await _write(client, tenant_id)
    await sc.update_memory_status(out["id"], "conflicted", tenant_id=tenant_id)
    assert (await _row(sc, tenant_id, out["id"]))["verification_status"] == "disputed"
    await sc.update_memory_status(out["id"], "confirmed", tenant_id=tenant_id)
    assert (await _row(sc, tenant_id, out["id"]))["verification_status"] == "unverified"


@pytest.mark.integration
@pytest.mark.parametrize(
    ("source_uri", "expected"),
    [
        ("https://example.com/x", "tool_observed"),
        ("text-input", "unverified"),
        ("LongMemEval:q3", "unverified"),
    ],
)
async def test_the_restore_applies_the_real_source_rule_in_sql(
    client, sc, tenant_id, source_uri, expected
) -> None:
    out = await _write(client, tenant_id, source_uri=source_uri)
    await sc.update_memory_status(out["id"], "conflicted", tenant_id=tenant_id)
    await sc.update_memory_status(out["id"], "active", tenant_id=tenant_id)
    assert (await _row(sc, tenant_id, out["id"]))["verification_status"] == expected


@pytest.mark.integration
async def test_the_batch_status_route_disputes_too(client, sc, tenant_id) -> None:
    out = await _write(client, tenant_id, tool_call_id="toolu_b")
    await sc.batch_update_status(
        {"updates": [{"memory_id": out["id"], "status": "conflicted"}]},
        tenant_id=tenant_id,
    )
    assert (await _row(sc, tenant_id, out["id"]))["verification_status"] == "disputed"


@pytest.mark.integration
async def test_outdated_keeps_the_state(client, sc, tenant_id) -> None:
    observed = await _write(client, tenant_id, tool_call_id="toolu_o")
    await sc.update_memory_status(observed["id"], "outdated", tenant_id=tenant_id)
    assert (await _row(sc, tenant_id, observed["id"]))[
        "verification_status"
    ] == "tool_observed"

    disputed = await _write(client, tenant_id)
    await sc.update_memory_status(disputed["id"], "conflicted", tenant_id=tenant_id)
    await sc.update_memory_status(disputed["id"], "outdated", tenant_id=tenant_id)
    assert (await _row(sc, tenant_id, disputed["id"]))[
        "verification_status"
    ] == "disputed"


@pytest.mark.integration
async def test_a_retraction_restores_a_disputed_row(client, sc, tenant_id) -> None:
    older = await _write(client, tenant_id)
    newer = await _write(client, tenant_id, tool_call_id="toolu_r")
    await sc.update_memory_status(
        newer["id"], "conflicted", tenant_id=tenant_id, supersedes_id=older["id"]
    )
    assert (await _row(sc, tenant_id, newer["id"]))["verification_status"] == "disputed"
    # The Path C retraction shape: clear the edge, set the status, one statement.
    await sc.update_memory_status(
        newer["id"],
        "active",
        tenant_id=tenant_id,
        unset_supersedes=True,
        expected_supersedes_id=older["id"],
    )
    row = await _row(sc, tenant_id, newer["id"])
    assert row["supersedes_id"] is None
    assert row["verification_status"] == "tool_observed"


@pytest.mark.integration
async def test_a_patched_status_follows_the_same_rule(client, sc, tenant_id) -> None:
    out = await _write(client, tenant_id, tool_call_id="toolu_p")
    await sc.update_memory(out["id"], tenant_id, {"status": "conflicted"})
    assert (await _row(sc, tenant_id, out["id"]))["verification_status"] == "disputed"
    await sc.update_memory(out["id"], tenant_id, {"status": "active"})
    assert (await _row(sc, tenant_id, out["id"]))[
        "verification_status"
    ] == "tool_observed"


@pytest.mark.integration
async def test_a_source_edit_recomputes_the_state(client, sc, tenant_id) -> None:
    out = await _write(client, tenant_id)
    r = await client.patch(
        f"/api/v1/memories/{out['id']}?tenant_id={tenant_id}",
        json={"source_uri": "https://example.com/found-it"},
        headers=get_admin_headers(),
    )
    assert r.status_code == 200, r.text
    assert (await _row(sc, tenant_id, out["id"]))[
        "verification_status"
    ] == "tool_observed"


# ── EC-14: the verify operation, REST ──────────────────────────────────────


@pytest.fixture
def as_caller():
    """Authenticate REST calls as a given credential shape (gateway-resolved)."""
    from core_api.app import app
    from core_api.auth import AuthContext, get_auth_context
    from core_api.tenant_context import set_current_tenant

    def _install(tenant_id: str, **ctx):
        async def _dep():
            set_current_tenant(tenant_id)
            return AuthContext(
                tenant_id=tenant_id, readable_tenant_ids=[tenant_id], **ctx
            )

        app.dependency_overrides[get_auth_context] = _dep

    yield _install
    app.dependency_overrides.pop(get_auth_context, None)


@pytest.fixture
def agent_trust(monkeypatch):
    """Make ``lookup_agent`` answer with an agent of the given trust in ``_FLEET``."""

    def _set(trust_level: int | None):
        async def fake_lookup(tenant_id, agent_id, *, read=True):
            if trust_level is None:
                return None
            return {
                "agent_id": agent_id,
                "fleet_id": _FLEET,
                "trust_level": trust_level,
            }

        monkeypatch.setattr(agent_service, "lookup_agent", fake_lookup)

    return _set


@pytest.fixture
def audit(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    async def fake_log_action(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(verification_service, "log_action", fake_log_action)
    return calls


async def _verify(client, tenant_id: str, memory_id: str, *, undo: bool = False):
    action = "unverify" if undo else "verify"
    return await client.post(
        f"/api/v1/memories/{memory_id}/{action}?tenant_id={tenant_id}"
    )


@pytest.mark.integration
async def test_a_trust_3_agent_verifies(
    client, sc, tenant_id, as_caller, agent_trust, audit
) -> None:
    out = await _write(client, tenant_id, tool_call_id="toolu_v")
    as_caller(tenant_id, agent_id="checker")
    agent_trust(3)
    r = await _verify(client, tenant_id, out["id"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["old_verification_status"] == "tool_observed"
    assert body["verification_status"] == "human_verified"
    assert body["verified_by"] == "agent:checker"
    assert body["verified_at"]
    row = await _row(sc, tenant_id, out["id"])
    assert (row["verification_status"], row["verified_by"]) == (
        "human_verified",
        "agent:checker",
    )
    assert row["verified_at"] is not None
    [entry] = audit
    assert entry["action"] == "memory_verify"
    assert entry["agent_id"] == "checker"
    assert entry["resource_id"] == out["id"]
    assert entry["detail"]["old_verification_status"] == "tool_observed"
    assert entry["detail"]["new_verification_status"] == "human_verified"
    assert entry["detail"]["via"] == "rest"


@pytest.mark.integration
@pytest.mark.parametrize("trust", [0, 1, 2])
async def test_a_low_trust_agent_is_refused(
    client, sc, tenant_id, as_caller, agent_trust, audit, trust
) -> None:
    out = await _write(client, tenant_id)
    as_caller(tenant_id, agent_id="junior")
    agent_trust(trust)
    for undo in (False, True):
        r = await _verify(client, tenant_id, out["id"], undo=undo)
        assert r.status_code == 403, r.text
        detail = r.json()["error"]
        assert detail["code"] == "AGENT_TRUST_TOO_LOW"
        assert "needs trust level 3" in detail["message"]
    assert (await _row(sc, tenant_id, out["id"]))["verification_status"] == "unverified"
    assert audit == []


@pytest.mark.integration
async def test_an_unregistered_agent_is_refused(
    client, tenant_id, as_caller, agent_trust
) -> None:
    out = await _write(client, tenant_id)
    as_caller(tenant_id, agent_id="ghost")
    agent_trust(None)
    r = await _verify(client, tenant_id, out["id"])
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "AGENT_NOT_REGISTERED"


@pytest.mark.integration
async def test_an_agent_may_not_verify_a_disputed_row(
    client, sc, tenant_id, as_caller, agent_trust
) -> None:
    out = await _write(client, tenant_id)
    await sc.update_memory_status(out["id"], "conflicted", tenant_id=tenant_id)
    as_caller(tenant_id, agent_id="checker")
    agent_trust(3)
    r = await _verify(client, tenant_id, out["id"])
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "AGENT_CREDENTIAL_FORBIDDEN"
    assert "disputed" in r.json()["error"]["message"]
    assert (await _row(sc, tenant_id, out["id"]))["verification_status"] == "disputed"


@pytest.mark.integration
async def test_a_person_verifies_a_disputed_row_and_a_conflict_keeps_it(
    client, sc, tenant_id, as_caller, audit
) -> None:
    out = await _write(client, tenant_id)
    await sc.update_memory_status(out["id"], "conflicted", tenant_id=tenant_id)
    as_caller(tenant_id, is_person=True, org_role="member", user_id="user-42")
    r = await _verify(client, tenant_id, out["id"])
    assert r.status_code == 200, r.text
    assert (r.json()["verification_status"], r.json()["verified_by"]) == (
        "human_verified",
        "user-42",
    )
    assert audit[0]["detail"]["user_id"] == "user-42"
    # A later contradiction disputes it again; the verifier is kept, so the
    # dispute's resolution brings the person's verification back.
    await sc.update_memory_status(out["id"], "conflicted", tenant_id=tenant_id)
    row = await _row(sc, tenant_id, out["id"])
    assert (row["verification_status"], row["verified_by"]) == ("disputed", "user-42")
    await sc.update_memory_status(out["id"], "active", tenant_id=tenant_id)
    assert (await _row(sc, tenant_id, out["id"]))[
        "verification_status"
    ] == "human_verified"


@pytest.mark.integration
async def test_a_tenant_key_verifies_and_names_itself(
    client, sc, tenant_id, as_caller
) -> None:
    out = await _write(client, tenant_id)
    as_caller(tenant_id)
    r = await _verify(client, tenant_id, out["id"])
    assert r.status_code == 200, r.text
    assert r.json()["verified_by"] == "tenant_key"


@pytest.mark.integration
async def test_an_install_credential_is_refused(client, tenant_id, as_caller) -> None:
    out = await _write(client, tenant_id)
    as_caller(tenant_id, is_install_credential=True, install_uuid=str(uuid.uuid4()))
    r = await _verify(client, tenant_id, out["id"])
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "CREDENTIAL_KIND_FORBIDDEN"


@pytest.mark.integration
async def test_a_read_only_credential_is_refused(client, tenant_id, as_caller) -> None:
    out = await _write(client, tenant_id)
    as_caller(tenant_id, capabilities={"read"})
    r = await _verify(client, tenant_id, out["id"])
    assert r.status_code == 403, r.text


@pytest.mark.integration
async def test_a_missing_memory_is_404(client, tenant_id, as_caller) -> None:
    as_caller(tenant_id)
    r = await _verify(client, tenant_id, str(uuid.uuid4()))
    assert r.status_code == 404, r.text


@pytest.mark.integration
async def test_an_agent_outside_the_rows_scope_is_refused(
    client, tenant_id, as_caller, agent_trust
) -> None:
    out = await _write(client, tenant_id, agent_id="owner", visibility="scope_agent")
    as_caller(tenant_id, agent_id="checker")
    agent_trust(3)
    r = await _verify(client, tenant_id, out["id"])
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "FLEET_SCOPE_FORBIDDEN"


@pytest.mark.integration
async def test_unverify_returns_to_the_evidence_state(
    client, sc, tenant_id, as_caller, agent_trust, audit
) -> None:
    out = await _write(client, tenant_id, tool_call_id="toolu_u")
    as_caller(tenant_id, agent_id="checker")
    agent_trust(3)
    assert (await _verify(client, tenant_id, out["id"])).status_code == 200
    r = await _verify(client, tenant_id, out["id"], undo=True)
    assert r.status_code == 200, r.text
    assert r.json()["old_verification_status"] == "human_verified"
    assert r.json()["verification_status"] == "tool_observed"
    row = await _row(sc, tenant_id, out["id"])
    assert row["verified_by"] is None and row["verified_at"] is None
    assert [a["action"] for a in audit] == ["memory_verify", "memory_unverify"]


@pytest.mark.integration
async def test_unverify_keeps_a_dispute(
    client, sc, tenant_id, as_caller, agent_trust
) -> None:
    out = await _write(client, tenant_id)
    as_caller(tenant_id, is_person=True, org_role="admin", user_id="user-7")
    assert (await _verify(client, tenant_id, out["id"])).status_code == 200
    await sc.update_memory_status(out["id"], "conflicted", tenant_id=tenant_id)
    # Withdrawing only lowers what readers believe: a trusted agent may, disputed or not.
    as_caller(tenant_id, agent_id="checker")
    agent_trust(3)
    r = await _verify(client, tenant_id, out["id"], undo=True)
    assert r.status_code == 200, r.text
    assert r.json()["verification_status"] == "disputed"
    # With the verifier gone, the dispute resolves to the evidence state.
    await sc.update_memory_status(out["id"], "active", tenant_id=tenant_id)
    assert (await _row(sc, tenant_id, out["id"]))["verification_status"] == "unverified"


@pytest.mark.integration
async def test_a_content_edit_drops_a_verification(
    client, sc, tenant_id, as_caller
) -> None:
    out = await _write(client, tenant_id, source_uri="https://example.com/a")
    as_caller(tenant_id)
    assert (await _verify(client, tenant_id, out["id"])).status_code == 200
    r = await client.patch(
        f"/api/v1/memories/{out['id']}?tenant_id={tenant_id}",
        json={"content": f"rewritten {uid()}." + _PADDING},
    )
    assert r.status_code == 200, r.text
    row = await _row(sc, tenant_id, out["id"])
    assert row["verification_status"] == "tool_observed"
    assert row["verified_by"] is None and row["verified_at"] is None


# ── EC-14: the verify operation, MCP ───────────────────────────────────────


@pytest.fixture
def mcp_agent(monkeypatch):
    from core_api import mcp_server

    def _install(tenant_id: str, agent_id: str | None):
        monkeypatch.setattr(mcp_server, "_check_auth", lambda: None)
        monkeypatch.setattr(mcp_server, "_check_write_scope", lambda: None)
        monkeypatch.setattr(mcp_server, "_get_tenant", lambda: tenant_id)
        monkeypatch.setattr(mcp_server, "_get_agent_id", lambda: agent_id)

        async def _owned(_agent_id):
            return None

        monkeypatch.setattr(mcp_server, "_refuse_unowned_agent", _owned)

    return _install


@pytest.mark.integration
async def test_mcp_verify_and_unverify_as_a_trust_3_agent(
    client, sc, tenant_id, mcp_agent, agent_trust, audit
) -> None:
    from core_api import mcp_server

    out = await _write(client, tenant_id, tool_call_id="toolu_m")
    mcp_agent(tenant_id, "checker")
    agent_trust(3)
    env = parse_envelope(
        await mcp_server.caura_manage(op="verify", memory_id=out["id"])
    )
    assert "error" not in env, env
    assert (env["verification_status"], env["verified_by"]) == (
        "human_verified",
        "agent:checker",
    )
    env = parse_envelope(
        await mcp_server.caura_manage(op="unverify", memory_id=out["id"])
    )
    assert env["verification_status"] == "tool_observed"
    assert [(a["action"], a["detail"]["via"]) for a in audit] == [
        ("memory_verify", "mcp"),
        ("memory_unverify", "mcp"),
    ]


@pytest.mark.integration
async def test_mcp_verify_refuses_a_trust_1_agent(
    client, sc, tenant_id, mcp_agent, agent_trust, audit
) -> None:
    from core_api import mcp_server

    out = await _write(client, tenant_id)
    mcp_agent(tenant_id, "junior")
    agent_trust(1)
    env = parse_envelope(
        await mcp_server.caura_manage(op="verify", memory_id=out["id"])
    )
    assert env["error"]["code"] == "AGENT_TRUST_TOO_LOW"
    assert "needs trust level 3" in env["error"]["message"]
    assert (await _row(sc, tenant_id, out["id"]))["verification_status"] == "unverified"
    assert audit == []


@pytest.mark.integration
async def test_mcp_verify_refuses_an_agent_on_a_disputed_row(
    client, sc, tenant_id, mcp_agent, agent_trust
) -> None:
    from core_api import mcp_server

    out = await _write(client, tenant_id)
    await sc.update_memory_status(out["id"], "conflicted", tenant_id=tenant_id)
    mcp_agent(tenant_id, "checker")
    agent_trust(3)
    env = parse_envelope(
        await mcp_server.caura_manage(op="verify", memory_id=out["id"])
    )
    assert env["error"]["code"] == "AGENT_CREDENTIAL_FORBIDDEN"


@pytest.mark.integration
async def test_mcp_verify_by_a_tenant_key(client, sc, tenant_id, mcp_agent) -> None:
    from core_api import mcp_server

    out = await _write(client, tenant_id)
    mcp_agent(tenant_id, None)
    env = parse_envelope(
        await mcp_server.caura_manage(op="verify", memory_id=out["id"])
    )
    assert "error" not in env, env
    assert (env["verification_status"], env["verified_by"]) == (
        "human_verified",
        "tenant_key",
    )
