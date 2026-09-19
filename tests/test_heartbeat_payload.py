"""The heartbeat payload: buckets, the schema contract and the never-sent list.

``docs/telemetry-schema-v1.json`` is the contract. A hand-written structural
validator checks every payload here so the test does not depend on a
validator library being installed; when ``jsonschema`` happens to be present
the same payloads are run through it too.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from core_api.heartbeat import payload as payload_mod
from core_api.heartbeat.clients import FAMILIES
from core_api.heartbeat.payload import (
    BUCKETS,
    Counts,
    bucket,
    build_payload,
    collect_counts,
    deploy_kind,
    major_minor,
    uptime_bucket,
)

pytestmark = [pytest.mark.unit]

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "docs" / "telemetry-schema-v1.json"
DOC_PATH = Path(__file__).resolve().parents[1] / "docs" / "telemetry.md"
DEPLOYMENT_ID = "6f0a3c2e-1b4d-4e8f-9a7c-2d5e8b1f4a90"


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _settings(**overrides):
    base = {
        "is_standalone": True,
        "embedding_provider": "openai",
        "entity_extraction_provider": "none",
        "redis_url": "",
        "sentry_dsn": "",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# ── a small JSON Schema validator (the subset the contract uses) ────────


def _resolve(ref: str, root: dict) -> dict:
    assert ref.startswith("#/"), ref
    node: dict = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def _validate(instance, schema: dict, root: dict, path: str = "$") -> list[str]:
    errors: list[str] = []
    if "$ref" in schema:
        return _validate(instance, _resolve(schema["$ref"], root), root, path)
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: {instance!r} != const {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not in enum")
    typ = schema.get("type")
    if typ == "object":
        if not isinstance(instance, dict):
            return [f"{path}: expected object"]
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required {key!r}")
        if schema.get("additionalProperties") is False:
            for key in instance:
                if key not in props:
                    errors.append(f"{path}: unexpected key {key!r}")
        for key, sub in props.items():
            if key in instance:
                errors.extend(_validate(instance[key], sub, root, f"{path}.{key}"))
    elif typ == "array":
        if not isinstance(instance, list):
            return [f"{path}: expected array"]
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        if schema.get("uniqueItems") and len(set(map(json.dumps, instance))) != len(
            instance
        ):
            errors.append(f"{path}: duplicate items")
        for i, item in enumerate(instance):
            errors.extend(_validate(item, schema["items"], root, f"{path}[{i}]"))
    elif typ == "string":
        if not isinstance(instance, str):
            return [f"{path}: expected string, got {type(instance).__name__}"]
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append(f"{path}: {instance!r} does not match {schema['pattern']}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']}")
    elif typ == "boolean":
        if not isinstance(instance, bool):
            errors.append(f"{path}: expected boolean")
    elif typ == "integer":
        if not isinstance(instance, int) or isinstance(instance, bool):
            errors.append(f"{path}: expected integer")
    return errors


def assert_valid(instance: dict) -> None:
    root = _schema()
    errors = _validate(instance, root, root)
    assert errors == [], "\n".join(errors)
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - optional cross-check
        return
    jsonschema.Draft202012Validator(root).validate(instance)


# ── buckets ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (-3, "0"),
        (0, "0"),
        (1, "1"),
        (2, "2-5"),
        (5, "2-5"),
        (6, "6-20"),
        (20, "6-20"),
        (21, "21-100"),
        (100, "21-100"),
        (101, "101-1k"),
        (1_000, "101-1k"),
        (1_001, "1k-10k"),
        (10_000, "1k-10k"),
        (10_001, "10k-100k"),
        (100_000, "10k-100k"),
        (100_001, ">100k"),
        (10**9, ">100k"),
    ],
)
def test_bucket_scale(n, expected):
    assert bucket(n) == expected
    assert expected in BUCKETS


def test_bucket_scale_matches_schema():
    assert list(BUCKETS) == _schema()["$defs"]["bucket"]["enum"]


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "<1h"),
        (3599, "<1h"),
        (3600, "1h-1d"),
        (86_399, "1h-1d"),
        (86_400, "1d-7d"),
        (7 * 86_400 - 1, "1d-7d"),
        (7 * 86_400, "7d-30d"),
        (30 * 86_400 - 1, "7d-30d"),
        (30 * 86_400, ">30d"),
    ],
)
def test_uptime_bucket(seconds, expected):
    assert uptime_bucket(seconds) == expected
    assert expected in _schema()["properties"]["uptime_bucket"]["enum"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2.21.3", "2.21"),
        ("2.21", "2.21"),
        ("v1.2.0-beta", "1.2"),
        ("garbage", None),
        ("", None),
        (None, None),
    ],
)
def test_major_minor(raw, expected):
    assert major_minor(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("3.17.0", "3.17.0"),
        (
            "v3.17.0",
            "3.17.0",
        ),  # CAURA_VERSION=v3.17.0, the pinning form docs/self-hosting.md uses
        ("V2.0", "2.0"),
        (" v1.2.3 ", "1.2.3"),
        ("dev", "dev"),
        ("vendor-build", "vendor-build"),
        ("", "dev"),
        (None, "dev"),
    ],
)
def test_normalise_version(raw, expected):
    assert payload_mod.normalise_version(raw) == expected


def test_payload_version_strips_the_tag_prefix():
    payload = build_payload(
        settings=_settings(),
        deployment_id=str(uuid.uuid4()),
        counts=Counts(),
        version="v3.17.0",
    )
    assert payload["version"] == "3.17.0"


def test_deploy_kind(tmp_path):
    assert deploy_kind(tmp_path / "VERSION") == "source"
    (tmp_path / "VERSION").write_text("3.16.0")
    assert deploy_kind(tmp_path / "VERSION") == "docker"


# ── the schema contract ──────────────────────────────────────────────────


def _example_payload() -> dict:
    return build_payload(
        settings=_settings(),
        deployment_id=DEPLOYMENT_ID,
        counts=Counts(
            memories=4321,
            agents=3,
            tenants=1,
            plugin_nodes_7d=1,
            plugin_versions=["2.21"],
        ),
        client_counts={"openclaw-plugin": 1, "caura-client-python": 1, "mcp": 4},
        version="3.16.0",
        uptime_seconds=3 * 86_400,
        env={"RANK_ENABLED": "", "EVENT_BUS_BACKEND": "inprocess"},
    )


def test_payload_validates_against_schema():
    assert_valid(_example_payload())


def test_payload_matches_the_documented_example_shape():
    """The example in docs/telemetry.md is a real payload, not an illustration."""
    doc = DOC_PATH.read_text()
    block = doc.split("```json", 1)[1].split("```", 1)[0]
    documented = json.loads(block)
    assert_valid(documented)
    built = _example_payload()
    assert set(documented) == set(built)
    for section in ("runtime", "mode", "providers", "counts", "clients_24h"):
        assert set(documented[section]) == set(built[section]), section


def test_every_count_is_a_bucket_string():
    p = _example_payload()
    for key, value in p["counts"].items():
        if key == "plugin_versions":
            continue
        assert value in BUCKETS, key
    assert set(p["clients_24h"]) == set(FAMILIES)
    for value in p["clients_24h"].values():
        assert value in BUCKETS


def test_allowlist_walk():
    """Every leaf is a schema key with a bucket, boolean, enum or constant value."""
    p = _example_payload()
    schema = _schema()
    assert set(p) == set(schema["properties"])
    assert p["schema"] == 1
    assert p["product"] == "caura-server"
    assert p["sent_at"].endswith("Z")
    for section in ("runtime", "mode", "providers", "counts", "clients_24h"):
        assert set(p[section]) == set(schema["properties"][section]["properties"]), (
            section
        )


def test_fake_providers_report_as_none():
    p = build_payload(
        settings=_settings(
            embedding_provider="fake", entity_extraction_provider="fake"
        ),
        deployment_id=DEPLOYMENT_ID,
        counts=Counts(),
        client_counts={},
        version="dev",
        uptime_seconds=1,
        env={"RANK_ENABLED": "true", "RANK_PROVIDER": "fake"},
    )
    assert p["providers"]["embedding"] == "none"
    assert p["providers"]["entity_extraction"] == "none"
    assert p["providers"]["rank"] == "noop"
    assert_valid(p)


def test_rank_reports_noop_when_disabled_regardless_of_provider():
    p = build_payload(
        settings=_settings(),
        deployment_id=DEPLOYMENT_ID,
        counts=Counts(),
        client_counts={},
        version="dev",
        uptime_seconds=1,
        env={"RANK_ENABLED": "false", "RANK_PROVIDER": "local"},
    )
    assert p["providers"]["rank"] == "noop"
    p = build_payload(
        settings=_settings(),
        deployment_id=DEPLOYMENT_ID,
        counts=Counts(),
        client_counts={},
        version="dev",
        uptime_seconds=1,
        env={
            "RANK_ENABLED": "true",
            "RANK_PROVIDER": "local",
            "EVENT_BUS_BACKEND": "pubsub",
        },
    )
    assert p["providers"]["rank"] == "local"
    assert p["providers"]["event_bus"] == "pubsub"
    assert_valid(p)


# ── never sent ───────────────────────────────────────────────────────────

POISON = {
    "embedding_provider": "db.internal.example.com",
    "entity_extraction_provider": "my-secret-model-gpt-9",
    "redis_url": "redis://:s3cr3tpass@redis.internal:6379/0",
    "sentry_dsn": "https://abc123def@o1.ingest.sentry.io/42",
}
POISON_ENV = {
    "RANK_ENABLED": "true",
    "RANK_PROVIDER": "http://rerank.internal:8080",
    "EVENT_BUS_BACKEND": "kafka.internal:9092",
    "OPENAI_API_KEY": "sk-live-000000",
}
EXACT_COUNTS = Counts(
    memories=1_234_567,
    agents=98_765,
    tenants=54_321,
    plugin_nodes_7d=678_901,
    plugin_versions=["2.21", "2.20"],
)


def test_forbidden_value_scan():
    p = build_payload(
        settings=_settings(**POISON),
        deployment_id=DEPLOYMENT_ID,
        counts=EXACT_COUNTS,
        client_counts={"other": 246_813, "mcp": 135_791},
        version="3.16.0",
        uptime_seconds=42,
        env=POISON_ENV,
    )
    body = json.dumps(p)
    for needle in (
        "db.internal",
        "example.com",
        "my-secret",
        "gpt-9",
        "s3cr3tpass",
        "redis.internal",
        "redis://",
        "sentry.io",
        "abc123def",
        "rerank.internal",
        "kafka.internal",
        "sk-live",
        "1234567",
        "98765",
        "54321",
        "678901",
        "246813",
        "135791",
    ):
        assert needle not in body, needle
    for value in POISON.values():
        assert value not in body
    # ``RANK_ENABLED=true`` is a switch, not a secret; the other env values
    # must not leak.
    for key, value in POISON_ENV.items():
        if key != "RANK_ENABLED":
            assert value not in body
    # The poison folded into the closed enums / booleans, and the result is
    # still a valid schema-1 payload.
    assert p["providers"] == {
        "embedding": "other",
        "entity_extraction": "other",
        "rank": "other",
        "event_bus": "inprocess",
        "redis": True,
        "sentry": True,
    }
    assert p["counts"]["memories"] == ">100k"
    assert p["clients_24h"]["other"] == ">100k"
    assert_valid(p)


def test_plugin_versions_capped_at_ten():
    versions = [f"2.{i}" for i in range(15)]
    p = build_payload(
        settings=_settings(),
        deployment_id=DEPLOYMENT_ID,
        counts=Counts(plugin_versions=versions),
        client_counts={},
        version="dev",
        uptime_seconds=1,
        env={},
    )
    assert len(p["counts"]["plugin_versions"]) == 10
    assert_valid(p)


# ── counts collection ────────────────────────────────────────────────────


class _FakeStorage:
    def __init__(self, *, summaries: dict[str, dict], fail: set[str] = frozenset()):
        self.summaries = summaries
        self.fail = fail
        self.summary_calls: list[str] = []

    async def count_distinct_tenants(self):
        if "tenants" in self.fail:
            raise RuntimeError("boom")
        return 2

    async def count_all(self, tenant_id: str):
        assert tenant_id == ""
        return 1234

    async def count_distinct_agents(self):
        return 7

    async def list_active_tenants(self):
        if "active" in self.fail:
            raise RuntimeError("boom")
        return list(self.summaries)

    async def fleet_nodes_summary(self, tenant_id: str, *, days: int = 7):
        assert days == 7
        self.summary_calls.append(tenant_id)
        if tenant_id in self.fail:
            raise RuntimeError("boom")
        return self.summaries.get(tenant_id, {"nodes_7d": 0, "plugin_versions": []})


async def test_collect_counts_fans_out_over_active_tenants():
    sc = _FakeStorage(
        summaries={
            "t1": {"nodes_7d": 1, "plugin_versions": ["2.21.3", "2.21.0"]},
            "t2": {"nodes_7d": 2, "plugin_versions": ["2.20.1", "bogus", None]},
        }
    )
    counts = await collect_counts(sc, standalone_tenant_id="default")
    assert counts.memories == 1234
    assert counts.agents == 7
    assert counts.tenants == 2
    assert counts.plugin_nodes_7d == 3
    assert counts.plugin_versions == ["2.20", "2.21"]
    # The standalone tenant is always asked, even with no memories yet.
    assert sc.summary_calls[0] == "default"
    assert set(sc.summary_calls) == {"default", "t1", "t2"}


async def test_collect_counts_reads_zero_on_failure():
    sc = _FakeStorage(
        summaries={"t1": {"nodes_7d": 5, "plugin_versions": ["1.0.0"]}},
        fail={"tenants", "active"},
    )
    counts = await collect_counts(sc)
    assert counts.tenants == 0
    assert counts.plugin_nodes_7d == 0
    assert counts.plugin_versions == []
    assert counts.memories == 1234


async def test_collect_counts_one_tenant_failure_does_not_zero_the_rest():
    sc = _FakeStorage(
        summaries={
            "t1": {"nodes_7d": 5, "plugin_versions": ["1.0.0"]},
            "t2": {"nodes_7d": 1, "plugin_versions": ["1.1.0"]},
        },
        fail={"t1"},
    )
    counts = await collect_counts(sc)
    assert counts.plugin_nodes_7d == 1
    assert counts.plugin_versions == ["1.1"]


async def test_collect_counts_fanout_is_capped(monkeypatch):
    monkeypatch.setattr(payload_mod, "TENANT_FANOUT_CAP", 3)
    sc = _FakeStorage(
        summaries={f"t{i}": {"nodes_7d": 1, "plugin_versions": []} for i in range(10)}
    )
    counts = await collect_counts(sc)
    assert counts.plugin_nodes_7d == 3
    assert len(sc.summary_calls) == 3
