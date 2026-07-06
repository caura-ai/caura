"""Skill Factory Phase 0 acceptance tests (SF-008).

Pure-unit coverage — no DB required. Exercises the
:mod:`core_api.services.skill_lifecycle.validate_and_normalize_skill_write`
contract and the configuration plumbing
(:mod:`core_api.services.organization_settings`) introduced by
SF-002 + SF-006.

Maps to plan §15 Phase 0 acceptance criteria:

  - ``memclaw_doc`` ``op=write`` against ``skills`` with
    ``description > 160 bytes`` returns 422
  - ``memclaw_doc`` ``op=write`` against ``skills`` without
    ``name``/``slug``/``description``/``domain``/``kind``/``source``
    returns 422
  - ``kind='update'`` rejects on hash mismatch (409) and on missing
    live target (404)
  - ``source='forge'`` is rejected from non-Forge callers (403)
  - ``status='active'`` is rejected from non-admin callers (403)
  - eToro pointer-only docs (no content field) are still
    representable post-migration (source='imported' skips the
    content-presence requirement)

Integration tests against a live storage-api + the migration apply
path live separately under ``tests/integration/`` and are not
required to pass in this file.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest
from fastapi import HTTPException

from core_api.services.skill_lifecycle import (
    ADMIN_ONLY_SOURCES,
    ADMIN_ONLY_STATUSES,
    ALLOWED_KINDS,
    ALLOWED_SOURCES,
    ALLOWED_STATUSES,
    INTERNAL_ONLY_SOURCES,
    INTERNAL_ONLY_STATUSES,
    REQUIRED_TOP_LEVEL_KEYS,
    SYSTEM_ONLY_STATUSES,
    SkillWriteContext,
    validate_and_normalize_skill_write,
)


# --- Test fixtures --------------------------------------------------------


def _agent_ctx(**overrides) -> SkillWriteContext:
    """Regular agent (non-admin, non-Forge) caller."""
    base = {
        "caller_agent_id": "alice",
        "is_admin": False,
        "is_internal_forge": False,
    }
    base.update(overrides)
    return SkillWriteContext(**base)


def _admin_ctx(**overrides) -> SkillWriteContext:
    base = {
        "caller_agent_id": "admin-bob",
        "is_admin": True,
        "is_internal_forge": False,
    }
    base.update(overrides)
    return SkillWriteContext(**base)


def _forge_ctx(**overrides) -> SkillWriteContext:
    base = {
        "caller_agent_id": "forge",
        "is_admin": False,
        "is_internal_forge": True,
    }
    base.update(overrides)
    return SkillWriteContext(**base)


def _valid_doc(**overrides) -> dict:
    """A minimally valid skills-write body — happy-path baseline.
    Tests override individual keys to provoke specific failures.
    """
    base = {
        "name": "Test Skill",
        "slug": "test-skill",
        "description": "Short trigger description.",
        "domain": "dev",
        "kind": "create",
        "source": "agent",
        "content": "## When to use\nStep 1.\nStep 2.\n",
    }
    base.update(overrides)
    return base


# --- Enums + constants ----------------------------------------------------


@pytest.mark.unit
class TestEnumConstants:
    def test_allowed_sources(self):
        assert ALLOWED_SOURCES == frozenset({"forge", "agent", "manual", "imported"})

    def test_allowed_kinds(self):
        assert ALLOWED_KINDS == frozenset({"create", "update"})

    def test_allowed_statuses(self):
        # Mirrors plan §5 lifecycle states.
        assert ALLOWED_STATUSES == frozenset(
            {
                "candidate",
                "staged",
                "active",
                "rejected",
                "quarantined",
                "stale",
                "deprecated",
            }
        )

    def test_admin_only_partitions(self):
        # source/status RBAC partitioning is mutually exclusive — no
        # value can be both admin-only AND internal-only.
        assert ADMIN_ONLY_SOURCES.isdisjoint(INTERNAL_ONLY_SOURCES)
        assert ADMIN_ONLY_STATUSES.isdisjoint(INTERNAL_ONLY_STATUSES)
        assert ADMIN_ONLY_SOURCES <= ALLOWED_SOURCES
        assert INTERNAL_ONLY_SOURCES <= ALLOWED_SOURCES
        assert ADMIN_ONLY_STATUSES <= ALLOWED_STATUSES
        assert INTERNAL_ONLY_STATUSES <= ALLOWED_STATUSES

    def test_system_only_statuses_partition(self):
        # System-managed terminal/hold states. Disjoint from admin and
        # internal sets — system-only means "no HTTP caller may set
        # these directly". Subset of ALLOWED_STATUSES.
        assert SYSTEM_ONLY_STATUSES == frozenset(
            {"quarantined", "rejected", "stale", "deprecated"}
        )
        assert SYSTEM_ONLY_STATUSES.isdisjoint(ADMIN_ONLY_STATUSES)
        assert SYSTEM_ONLY_STATUSES.isdisjoint(INTERNAL_ONLY_STATUSES)
        assert SYSTEM_ONLY_STATUSES <= ALLOWED_STATUSES

    def test_required_keys_contract(self):
        # The schema contract the route promises agents.
        assert "name" in REQUIRED_TOP_LEVEL_KEYS
        assert "slug" in REQUIRED_TOP_LEVEL_KEYS
        assert "description" in REQUIRED_TOP_LEVEL_KEYS
        assert "domain" in REQUIRED_TOP_LEVEL_KEYS


# --- Adjustment 1: schema validator hook ----------------------------------


@pytest.mark.unit
class TestSchemaValidator:
    @pytest.mark.parametrize("missing_key", list(REQUIRED_TOP_LEVEL_KEYS))
    @pytest.mark.asyncio
    async def test_missing_required_field_rejected(self, missing_key):
        doc = _valid_doc()
        doc.pop(missing_key)
        with pytest.raises(HTTPException) as exc:
            await validate_and_normalize_skill_write(doc, ctx=_agent_ctx())
        assert exc.value.status_code == 422
        assert missing_key in str(exc.value.detail)

    @pytest.mark.asyncio
    async def test_invalid_slug_rejected(self):
        with pytest.raises(HTTPException) as exc:
            await validate_and_normalize_skill_write(
                _valid_doc(slug="UPPER_CASE_SLUG"), ctx=_agent_ctx()
            )
        assert exc.value.status_code == 422
        assert "slug" in str(exc.value.detail).lower()

    @pytest.mark.asyncio
    async def test_invalid_kind_rejected(self):
        with pytest.raises(HTTPException) as exc:
            await validate_and_normalize_skill_write(
                _valid_doc(kind="patch"), ctx=_agent_ctx()
            )
        assert exc.value.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_source_rejected(self):
        with pytest.raises(HTTPException) as exc:
            await validate_and_normalize_skill_write(
                _valid_doc(source="bogus"), ctx=_agent_ctx()
            )
        assert exc.value.status_code == 422

    @pytest.mark.asyncio
    async def test_tags_must_be_list_of_strings(self):
        with pytest.raises(HTTPException) as exc:
            await validate_and_normalize_skill_write(
                _valid_doc(tags=[1, 2, 3]), ctx=_agent_ctx()
            )
        assert exc.value.status_code == 422

    @pytest.mark.asyncio
    async def test_non_dict_data_rejected(self):
        with pytest.raises(HTTPException) as exc:
            await validate_and_normalize_skill_write(
                "not-a-dict",  # type: ignore[arg-type]
                ctx=_agent_ctx(),
            )
        assert exc.value.status_code == 422


# --- Adjustment 2: description cap ----------------------------------------


@pytest.mark.unit
class TestDescriptionCap:
    @pytest.mark.asyncio
    async def test_default_cap_is_160_bytes(self):
        # 161 bytes — one over the default 160 cap.
        doc = _valid_doc(description="A" * 161)
        with pytest.raises(HTTPException) as exc:
            await validate_and_normalize_skill_write(doc, ctx=_agent_ctx())
        assert exc.value.status_code == 422
        assert "160" in str(exc.value.detail) or "bytes" in str(exc.value.detail)

    @pytest.mark.asyncio
    async def test_at_exact_cap_allowed(self):
        # 160 bytes — exactly at the cap, must pass.
        doc = _valid_doc(description="A" * 160)
        out, _ = await validate_and_normalize_skill_write(doc, ctx=_agent_ctx())
        assert out["description"] == "A" * 160

    @pytest.mark.asyncio
    async def test_multibyte_chars_count_as_bytes_not_codepoints(self):
        # The cap is in UTF-8 BYTES, not character count. Three-byte
        # codepoints (e.g. "€" = 3 bytes) burn through faster.
        char = "€"  # 3 UTF-8 bytes each
        s = char * 54  # 162 bytes > 160
        with pytest.raises(HTTPException):
            await validate_and_normalize_skill_write(
                _valid_doc(description=s), ctx=_agent_ctx()
            )
        s_ok = char * 53  # 159 bytes
        out, _ = await validate_and_normalize_skill_write(
            _valid_doc(description=s_ok), ctx=_agent_ctx()
        )
        assert out["description"] == s_ok

    @pytest.mark.asyncio
    async def test_configurable_cap_via_ctx(self):
        # A tenant lowers the cap to 40 bytes via org settings.
        ctx = _agent_ctx(description_max_bytes=40)
        with pytest.raises(HTTPException):
            await validate_and_normalize_skill_write(
                _valid_doc(description="A" * 41), ctx=ctx
            )
        out, _ = await validate_and_normalize_skill_write(
            _valid_doc(description="A" * 40), ctx=ctx
        )
        assert out["description"] == "A" * 40


# --- Adjustment 3: body cap -----------------------------------------------


@pytest.mark.unit
class TestBodyCap:
    @pytest.mark.asyncio
    async def test_default_body_cap_40k(self):
        with pytest.raises(HTTPException) as exc:
            await validate_and_normalize_skill_write(
                _valid_doc(content="A" * (40_000 + 1)), ctx=_agent_ctx()
            )
        assert exc.value.status_code == 422

    @pytest.mark.asyncio
    async def test_content_required_for_non_imported(self):
        doc = _valid_doc()
        del doc["content"]
        with pytest.raises(HTTPException) as exc:
            await validate_and_normalize_skill_write(doc, ctx=_agent_ctx())
        assert exc.value.status_code == 422
        assert "content" in str(exc.value.detail).lower()

    @pytest.mark.asyncio
    async def test_content_must_be_string(self):
        with pytest.raises(HTTPException):
            await validate_and_normalize_skill_write(
                _valid_doc(content={"not": "a string"}), ctx=_agent_ctx()
            )


# --- Adjustment 4: source defaulting + RBAC -------------------------------


@pytest.mark.unit
class TestSourceRbac:
    @pytest.mark.asyncio
    async def test_source_forge_rejected_from_agent(self):
        with pytest.raises(HTTPException) as exc:
            await validate_and_normalize_skill_write(
                _valid_doc(source="forge"), ctx=_agent_ctx()
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_source_forge_rejected_from_admin(self):
        # Even admin can't mint source=forge via API — it's reserved
        # for the internal lifecycle worker.
        with pytest.raises(HTTPException) as exc:
            await validate_and_normalize_skill_write(
                _valid_doc(source="forge"), ctx=_admin_ctx()
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_source_forge_allowed_from_internal_forge(self):
        # The internal Forge worker IS allowed.
        out, _ = await validate_and_normalize_skill_write(
            _valid_doc(source="forge"), ctx=_forge_ctx()
        )
        assert out["source"] == "forge"

    @pytest.mark.asyncio
    async def test_source_manual_rejected_from_agent(self):
        with pytest.raises(HTTPException) as exc:
            await validate_and_normalize_skill_write(
                _valid_doc(source="manual"), ctx=_agent_ctx()
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_source_manual_allowed_from_admin(self):
        out, _ = await validate_and_normalize_skill_write(
            _valid_doc(source="manual"), ctx=_admin_ctx()
        )
        assert out["source"] == "manual"

    @pytest.mark.asyncio
    async def test_source_agent_allowed_from_everyone(self):
        out, _ = await validate_and_normalize_skill_write(
            _valid_doc(source="agent"), ctx=_agent_ctx()
        )
        assert out["source"] == "agent"

    @pytest.mark.asyncio
    async def test_source_imported_pointer_only_path(self):
        """eToro 1,402 pointer-only docs (no content) survive
        post-migration as source='imported'. This is the
        backwards-compat path."""
        doc = _valid_doc(source="imported")
        del doc["content"]
        out, _ = await validate_and_normalize_skill_write(doc, ctx=_admin_ctx())
        assert out["source"] == "imported"
        assert "content_hash" not in out  # nothing to hash


# --- Adjustment 5: status defaulting + RBAC -------------------------------


@pytest.mark.unit
class TestStatusRbac:
    @pytest.mark.asyncio
    async def test_status_defaults_to_staged_for_agent(self):
        doc = _valid_doc()
        # No status in body.
        out, _ = await validate_and_normalize_skill_write(doc, ctx=_agent_ctx())
        assert out["status"] == "staged"

    @pytest.mark.asyncio
    async def test_status_defaults_to_candidate_for_forge(self):
        doc = _valid_doc(source="forge")
        out, _ = await validate_and_normalize_skill_write(doc, ctx=_forge_ctx())
        assert out["status"] == "candidate"

    @pytest.mark.asyncio
    async def test_status_active_rejected_from_agent(self):
        with pytest.raises(HTTPException) as exc:
            await validate_and_normalize_skill_write(
                _valid_doc(status="active"), ctx=_agent_ctx()
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_status_active_allowed_from_admin(self):
        out, _ = await validate_and_normalize_skill_write(
            _valid_doc(source="manual", status="active"), ctx=_admin_ctx()
        )
        assert out["status"] == "active"

    @pytest.mark.asyncio
    async def test_status_candidate_rejected_from_agent(self):
        with pytest.raises(HTTPException) as exc:
            await validate_and_normalize_skill_write(
                _valid_doc(status="candidate"), ctx=_agent_ctx()
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_invalid_status_rejected(self):
        with pytest.raises(HTTPException) as exc:
            await validate_and_normalize_skill_write(
                _valid_doc(status="bogus"), ctx=_agent_ctx()
            )
        assert exc.value.status_code == 422

    @pytest.mark.parametrize("system_status", sorted(SYSTEM_ONLY_STATUSES))
    @pytest.mark.asyncio
    async def test_system_only_status_rejected_from_agent(self, system_status):
        """Regular agent cannot mint quarantined / rejected / stale /
        deprecated directly. These are system-managed transitions
        (Sentinel, Inbox Reject, hash-binding, deprecation flow)."""
        with pytest.raises(HTTPException) as exc:
            await validate_and_normalize_skill_write(
                _valid_doc(status=system_status), ctx=_agent_ctx()
            )
        assert exc.value.status_code == 403
        assert "system-managed" in str(exc.value.detail).lower()

    @pytest.mark.parametrize("system_status", sorted(SYSTEM_ONLY_STATUSES))
    @pytest.mark.asyncio
    async def test_system_only_status_rejected_from_admin(self, system_status):
        # Even an admin cannot mint these directly. They land via the
        # internal lifecycle flow only.
        with pytest.raises(HTTPException) as exc:
            await validate_and_normalize_skill_write(
                _valid_doc(source="manual", status=system_status), ctx=_admin_ctx()
            )
        assert exc.value.status_code == 403

    @pytest.mark.parametrize("system_status", sorted(SYSTEM_ONLY_STATUSES))
    @pytest.mark.asyncio
    async def test_system_only_status_rejected_from_forge(self, system_status):
        # The internal Forge worker can mint ``candidate`` but cannot
        # mint terminal/hold states either — the lifecycle worker for
        # those (Sentinel + Inbox + drift detector) is separate.
        with pytest.raises(HTTPException) as exc:
            await validate_and_normalize_skill_write(
                _valid_doc(source="forge", status=system_status), ctx=_forge_ctx()
            )
        assert exc.value.status_code == 403


# --- Adjustment 6: auto-fill server-controlled fields ---------------------


@pytest.mark.unit
class TestAutoFill:
    @pytest.mark.asyncio
    async def test_content_hash_computed(self):
        out, _ = await validate_and_normalize_skill_write(
            _valid_doc(content="hello world"), ctx=_agent_ctx()
        )
        # Same content → same hash, prefixed.
        assert out["content_hash"].startswith("sha256:")
        # Deterministic.
        out2, _ = await validate_and_normalize_skill_write(
            _valid_doc(content="hello world"), ctx=_agent_ctx()
        )
        assert out["content_hash"] == out2["content_hash"]

    @pytest.mark.asyncio
    async def test_origin_agent_id_overrides_client_value(self):
        # The client tries to claim agent_id='attacker'; server
        # overrides with the auth context.
        out, _ = await validate_and_normalize_skill_write(
            _valid_doc(origin={"agent_id": "attacker"}),
            ctx=_agent_ctx(caller_agent_id="alice"),
        )
        assert out["origin"]["agent_id"] == "alice"

    @pytest.mark.asyncio
    async def test_origin_other_fields_preserved(self):
        # session_key, run_id, message_id are client-provided and
        # should pass through unchanged.
        out, _ = await validate_and_normalize_skill_write(
            _valid_doc(
                origin={"session_key": "sk-1", "run_id": "r-2", "message_id": "m-3"}
            ),
            ctx=_agent_ctx(caller_agent_id="alice"),
        )
        assert out["origin"]["session_key"] == "sk-1"
        assert out["origin"]["run_id"] == "r-2"
        assert out["origin"]["message_id"] == "m-3"
        assert out["origin"]["agent_id"] == "alice"

    @pytest.mark.asyncio
    async def test_timestamps_filled(self):
        out, _ = await validate_and_normalize_skill_write(
            _valid_doc(), ctx=_agent_ctx()
        )
        assert "created_at" in out
        assert "updated_at" in out

    @pytest.mark.asyncio
    async def test_updated_at_always_server_set(self):
        # Client supplies their own updated_at — server overwrites.
        out, _ = await validate_and_normalize_skill_write(
            _valid_doc(updated_at="1970-01-01T00:00:00+00:00"),
            ctx=_agent_ctx(),
        )
        assert out["updated_at"] != "1970-01-01T00:00:00+00:00"


# --- Adjustment 7: Sentinel scan + kind=update hash-binding ---------------


@pytest.mark.unit
class TestHashBindingAndScan:
    @pytest.mark.asyncio
    async def test_update_without_target_field_rejected(self):
        with pytest.raises(HTTPException) as exc:
            await validate_and_normalize_skill_write(
                _valid_doc(kind="update"), ctx=_agent_ctx()
            )
        assert exc.value.status_code == 422
        assert "target" in str(exc.value.detail).lower()

    @pytest.mark.asyncio
    async def test_update_without_live_target_returns_404(self):
        with pytest.raises(HTTPException) as exc:
            await validate_and_normalize_skill_write(
                _valid_doc(
                    kind="update",
                    target={"slug": "test-skill", "target_content_hash": "sha256:abc"},
                ),
                ctx=_agent_ctx(),
                live_skill_doc=None,
            )
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_update_hash_mismatch_returns_409(self):
        with pytest.raises(HTTPException) as exc:
            await validate_and_normalize_skill_write(
                _valid_doc(
                    kind="update",
                    target={
                        "slug": "test-skill",
                        "target_content_hash": "sha256:CALLER",
                    },
                ),
                ctx=_agent_ctx(),
                live_skill_doc={"data": {"content_hash": "sha256:LIVE"}},
            )
        assert exc.value.status_code == 409
        assert (
            "mismatch" in str(exc.value.detail).lower()
            or "changed" in str(exc.value.detail).lower()
        )

    @pytest.mark.asyncio
    async def test_update_hash_match_succeeds(self):
        out, _ = await validate_and_normalize_skill_write(
            _valid_doc(
                kind="update",
                target={"slug": "test-skill", "target_content_hash": "sha256:LIVE"},
            ),
            ctx=_agent_ctx(),
            live_skill_doc={"data": {"content_hash": "sha256:LIVE"}},
        )
        assert out["kind"] == "update"

    @pytest.mark.asyncio
    async def test_update_against_imported_pointer_only_returns_409(self):
        # Live doc is an imported pointer-only skill (no content_hash).
        # We can't bind — reject.
        with pytest.raises(HTTPException) as exc:
            await validate_and_normalize_skill_write(
                _valid_doc(
                    kind="update",
                    target={
                        "slug": "test-skill",
                        "target_content_hash": "sha256:WHATEVER",
                    },
                ),
                ctx=_agent_ctx(),
                live_skill_doc={"data": {"name": "Imported", "source": "imported"}},
            )
        assert exc.value.status_code == 409

    @pytest.mark.asyncio
    async def test_scan_result_attached_clean(self):
        out, scan = await validate_and_normalize_skill_write(
            _valid_doc(), ctx=_agent_ctx()
        )
        assert "scan" in out
        assert out["scan"]["state"] == "clean"
        assert out["scan"]["critical"] == 0
        assert scan.state == "clean"


# --- Migration chain integrity --------------------------------------------


@pytest.mark.unit
class TestRouteSlugRegex:
    """The ``routes/documents.py`` slug regex governs which ``doc_id``
    values are accepted on a ``collection='skills'`` write. The
    Skill Factory namespaces Forge candidates as ``forge/<slug>``
    and synchronous agent-direct writes as ``agent/<slug>`` (plan
    §3); without the prefix path Forge writes 422 themselves at the
    route boundary."""

    def _re(self):
        from core_api.routes.documents import _SKILL_SLUG_RE

        return _SKILL_SLUG_RE

    def test_plain_slug_accepted(self):
        assert self._re().fullmatch("deploy-eu-west-dns")

    def test_forge_namespaced_slug_accepted(self):
        assert self._re().fullmatch("forge/deploy-eu-west-dns")

    def test_agent_namespaced_slug_accepted(self):
        assert self._re().fullmatch("agent/morning-catchup")

    def test_other_namespaces_rejected(self):
        # Only ``forge/`` and ``agent/`` are accepted; arbitrary
        # prefixes still 422 to prevent rogue namespacing.
        assert self._re().fullmatch("system/x") is None
        assert self._re().fullmatch("admin/y") is None
        assert self._re().fullmatch("nested/path/slug") is None

    def test_uppercase_still_rejected(self):
        # The filesystem-safe rule still applies.
        assert self._re().fullmatch("FORGE/DEPLOY") is None
        assert self._re().fullmatch("Deploy") is None

    def test_leading_punctuation_rejected(self):
        assert self._re().fullmatch("-deploy") is None
        assert self._re().fullmatch(".deploy") is None
        assert self._re().fullmatch("forge/-deploy") is None

    def test_max_length_after_prefix(self):
        # The 100-char body limit applies AFTER the optional prefix.
        assert self._re().fullmatch("forge/" + "a" * 100)
        assert self._re().fullmatch("forge/" + "a" * 101) is None


@pytest.mark.unit
class TestMigrationChain:
    """Sanity check that the Phase 0 migrations (020 / 021 / 022) chain
    correctly off the prior head (019). Detects accidental down_revision
    typos at PR time without needing a live database."""

    def _load(self) -> dict[str, str | None]:
        chain: dict[str, str | None] = {}
        versions = pathlib.Path(
            "core-storage-api/src/core_storage_api/database/migrations/versions"
        )
        for f in sorted(versions.glob("*.py")):
            spec = importlib.util.spec_from_file_location(f.stem, f)
            assert spec is not None and spec.loader is not None
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            chain[mod.revision] = mod.down_revision
        return chain

    def test_single_head(self):
        chain = self._load()
        heads = set(chain) - {dr for dr in chain.values() if dr is not None}
        assert heads == {"029"}, f"Expected single head '029', got {sorted(heads)}"

    def test_skill_factory_chain_links(self):
        chain = self._load()
        assert chain.get("020") == "019", "020 must follow 019"
        assert chain.get("021") == "020", "021 must follow 020"
        assert chain.get("022") == "021", "022 must follow 021"
        assert chain.get("023") == "022", "023 must follow 022"
        # 024: fleet_commands auto-upgrade partial index (CAURA-000)
        assert chain.get("024") == "023", "024 must follow 023"
        # 025: tamper-evident audit hash chain (eToro governance)
        assert chain.get("025") == "024", "025 must follow 024"
        # 026: per-event audit idempotency (client_event_id + partial unique)
        assert chain.get("026") == "025", "026 must follow 025"
        # 027: opt-in recall logging (recall_event + recall_candidate)
        assert chain.get("027") == "026", "027 must follow 026"
        # 028: agent belonging model (belonging_type + owner_ref)
        assert chain.get("028") == "027", "028 must follow 027"
        # 029: agent activity digest (cached per-agent summaries, CAURA-222)
        assert chain.get("029") == "028", "029 must follow 028"

    def test_no_plain_create_index_on_large_tables(self):
        """Indexes on large, pre-existing tables MUST be built ``CONCURRENTLY``
        (inside an ``op.get_context().autocommit_block()``). A plain,
        in-transaction ``CREATE INDEX`` takes an AccessExclusive lock that blocks
        writes AND holds the migration advisory lock for the whole build — which
        crashed 6 storage-writer boots on 2026-06-16 (migration 025 indexed
        ``audit_log`` without CONCURRENTLY). This guards the raw-SQL
        ``op.execute("CREATE INDEX ...")`` path on the known-large tables; indexes
        created on a brand-new table in the same migration are unaffected."""
        import re

        large_tables = {
            "audit_log",
            "memories",
            "entities",
            "documents",
            "memory_entity_links",
            "relations",
        }
        # The CONCURRENTLY convention for indexes on already-populated large
        # tables is enforced from migration 005 onward (see 005/007/011/016/017).
        # 001–004 build the initial schema and index tables that are empty at
        # creation, so a plain CREATE INDEX there is harmless. 025 postdates the
        # convention but predates enforcement and is already applied in prod (the
        # index exists; the migration can't be rewritten) — documented debt.
        convention_from = 5
        applied_debt = {25}
        # CREATE [UNIQUE] INDEX [CONCURRENTLY] [IF NOT EXISTS] <name> ON <table>
        pat = re.compile(
            r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(CONCURRENTLY\s+)?"
            r"(?:IF\s+NOT\s+EXISTS\s+)?\w+\s+ON\s+(\w+)",
            re.IGNORECASE,
        )
        versions = pathlib.Path(
            "core-storage-api/src/core_storage_api/database/migrations/versions"
        )
        violations: list[str] = []
        for f in sorted(versions.glob("*.py")):
            prefix = f.stem.split("_")[0]
            if (
                not prefix.isdigit()
                or int(prefix) < convention_from
                or int(prefix) in applied_debt
            ):
                continue
            for concurrently, table in pat.findall(f.read_text()):
                if table.lower() in large_tables and not concurrently:
                    violations.append(f"{f.name}: plain CREATE INDEX on '{table}'")
        assert not violations, (
            "Plain (non-CONCURRENTLY) CREATE INDEX on a large table blocks writes "
            "and holds the migration advisory lock for the whole build (crashed "
            "storage-writer boots on 2026-06-16). Use CREATE INDEX CONCURRENTLY in "
            "an op.get_context().autocommit_block() — see migration 007 / 026. "
            f"Violations: {'; '.join(violations)}"
        )


@pytest.mark.unit
class TestForgeEventPayloadNaming:
    """The Forge run-knob field names must spell the same thing across
    org_settings, ForgeConfig, and the event payload. Drift between
    these three is a real bug: an operator who tunes
    ``skills_factory.forge.max_writes_per_run`` would expect the
    publisher kwarg to spell it identically. A test pin keeps the
    three in lockstep."""

    def test_event_payload_field_name_matches_config(self):
        # Payload field, ForgeConfig dataclass field, and the
        # org_settings key all spell ``max_writes_per_run``.
        from common.events.lifecycle_forge_request import LifecycleForgeDistillRequest
        from core_api.services.forge.forge_service import ForgeConfig
        from core_api.services.organization_settings import DEFAULT_SETTINGS

        # Payload (pydantic): the field exists with the right name.
        assert "max_writes_per_run" in LifecycleForgeDistillRequest.model_fields, (
            "LifecycleForgeDistillRequest.max_writes_per_run must match the "
            "ForgeConfig + org_settings name; legacy ``max_writes`` was "
            "renamed for consistency."
        )
        assert "max_writes" not in LifecycleForgeDistillRequest.model_fields, (
            "legacy ``max_writes`` field must be removed — drift trap"
        )
        # ForgeConfig dataclass.
        cfg_fields = {f for f in vars(ForgeConfig()).keys()}
        assert "max_writes_per_run" in cfg_fields
        # Settings key.
        forge_settings = DEFAULT_SETTINGS["skills_factory"]["forge"]
        assert "max_writes_per_run" in forge_settings

    def test_publisher_kwarg_matches_payload_field(self):
        # The publisher's keyword argument also spells max_writes_per_run.
        import inspect
        from common.events.lifecycle_publishers import publish_forge_distill_request

        sig = inspect.signature(publish_forge_distill_request)
        assert "max_writes_per_run" in sig.parameters
        assert "max_writes" not in sig.parameters, (
            "publish_forge_distill_request's legacy ``max_writes`` "
            "parameter must be removed"
        )

    def test_llm_tokens_field_name_matches_across_layers(self):
        # Same drift-trap test for ``llm_tokens_per_run`` — the event
        # payload, the publisher kwarg, and the org_settings key must
        # all spell it identically. The legacy ``llm_tokens_budget``
        # name was renamed for consistency with the
        # ``*_per_run`` settings-layer convention.
        import inspect

        from common.events.lifecycle_forge_request import LifecycleForgeDistillRequest
        from common.events.lifecycle_publishers import publish_forge_distill_request
        from core_api.services.organization_settings import DEFAULT_SETTINGS

        # Payload (pydantic).
        assert "llm_tokens_per_run" in LifecycleForgeDistillRequest.model_fields, (
            "LifecycleForgeDistillRequest.llm_tokens_per_run must match "
            "the org_settings name; legacy ``llm_tokens_budget`` was "
            "renamed for consistency with ``max_writes_per_run``."
        )
        assert "llm_tokens_budget" not in LifecycleForgeDistillRequest.model_fields, (
            "legacy ``llm_tokens_budget`` field must be removed — drift trap"
        )
        # Settings key.
        forge_settings = DEFAULT_SETTINGS["skills_factory"]["forge"]
        assert "llm_tokens_per_run" in forge_settings
        # Publisher kwarg.
        sig = inspect.signature(publish_forge_distill_request)
        assert "llm_tokens_per_run" in sig.parameters
        assert "llm_tokens_budget" not in sig.parameters, (
            "publish_forge_distill_request's legacy ``llm_tokens_budget`` "
            "parameter must be removed"
        )


@pytest.mark.unit
class TestMigration020Index:
    """The forge_rejected_fingerprints lookup index must include
    fleet_id (with NULLS FIRST) so the hot-path predicate
    ``(fleet_id = :f OR fleet_id IS NULL)`` is index-supported.
    A missing fleet_id column forces PG to filter every (tenant,
    fp) match in memory."""

    def _migration_020_source(self) -> str:
        path = pathlib.Path(
            "core-storage-api/src/core_storage_api/database/migrations/versions/"
            "020_forge_rejected_fingerprints.py"
        )
        return path.read_text()

    def test_lookup_index_includes_fleet_id(self):
        src = self._migration_020_source()
        # The CREATE INDEX statement must reference fleet_id.
        index_block = src[src.index("idx_forge_rejected_fp_lookup") :]
        # Stop at the next non-string line so we only inspect the
        # actual index DDL.
        index_block = index_block[: index_block.index('"\n    )')]
        assert "fleet_id" in index_block, (
            "idx_forge_rejected_fp_lookup must include fleet_id "
            "so the hot-path '(fleet_id = :f OR fleet_id IS NULL)' "
            "predicate is index-supported"
        )

    def test_lookup_index_uses_nulls_first_on_fleet_id(self):
        src = self._migration_020_source()
        # NULLS FIRST clusters the NULL-fleet rows at the head of
        # each (tenant, fp) group, matching the
        # 'fleet_id = :f OR fleet_id IS NULL' shape.
        assert "fleet_id NULLS FIRST" in src

    def test_lookup_index_still_sorts_rejected_at_desc(self):
        # The cooloff predicate is "is ANY rejection still active";
        # DESC on rejected_at lets PG short-circuit at the newest
        # hit per (tenant, fp, fleet) tuple.
        src = self._migration_020_source()
        assert "rejected_at DESC" in src


@pytest.mark.unit
class TestMigration022Sentinel:
    """The 022 downgrade must NOT strip ``source``/``status`` from
    rows that the migration did not write — only from rows
    carrying the ``_migrated_by='022'`` sentinel that Branches 1+2
    stamp during ``upgrade()``. This pins the SQL string so a
    refactor of the migration body can't silently regress the
    downgrade safety contract."""

    def _migration_source(self) -> str:
        path = pathlib.Path(
            "core-storage-api/src/core_storage_api/database/migrations/versions/"
            "022_skills_backfill_source_status.py"
        )
        return path.read_text()

    def test_branch_1_stamps_migrated_by_sentinel(self):
        src = self._migration_source()
        # Branch 1's UPDATE includes ``_migrated_by`` in its
        # jsonb_build_object call alongside source='manual'.
        assert "'_migrated_by', '022'" in src or '"_migrated_by", "022"' in src, (
            "Branches 1+2 must stamp _migrated_by='022' so the "
            "downgrade can safely identify rows we wrote"
        )

    def test_branch_3_and_4_do_not_stamp_sentinel(self):
        # Branch 3 (status backfill on already-sourced rows) and
        # Branch 4 (legacy source normalization) intentionally do
        # NOT carry the sentinel — they are not reversed by the
        # downgrade in the same way (Branch 4 restores via
        # legacy_source; Branch 3 is intentionally non-reversed).
        src = self._migration_source()
        # Count how many times _migrated_by appears in the upgrade.
        # Exactly 2 (Branch 1 + Branch 2) — Branch 3 / Branch 4
        # adding it would create false-positive downgrade strips.
        upgrade_section = src.split("def upgrade")[1].split("def downgrade")[0]
        assert upgrade_section.count("'_migrated_by'") == 2, (
            "Only Branches 1+2 should stamp _migrated_by; "
            f"got {upgrade_section.count(chr(39) + chr(95) + 'migrated_by' + chr(39))} occurrences"
        )

    def test_downgrade_filters_by_sentinel(self):
        # Downgrade's strip-step must filter on _migrated_by='022',
        # not just on source IN ('manual', 'imported').
        src = self._migration_source()
        downgrade_section = src.split("def downgrade")[1]
        assert "'_migrated_by'" in downgrade_section, (
            "Downgrade must reference _migrated_by in its WHERE clause"
        )
        assert "= '022'" in downgrade_section, (
            "Downgrade must check _migrated_by = '022' specifically"
        )
        # And it must STRIP the sentinel itself (so a re-upgrade
        # lands cleanly).
        assert "- '_migrated_by'" in downgrade_section, (
            "Downgrade must drop _migrated_by after using it"
        )
