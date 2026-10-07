"""Contract tests for the public evidence registry and generated mirrors."""

from __future__ import annotations

import copy
import json
import runpy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "evidence_registry.py"
REGISTRY = ROOT / "evidence" / "claims.json"
SCHEMA = ROOT / "evidence" / "claims.schema.json"
SCRIPT_API = runpy.run_path(str(SCRIPT))


def _registry() -> dict[str, Any]:
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


def _schema() -> dict[str, Any]:
    return json.loads(SCHEMA.read_text(encoding="utf-8"))


def _validate(registry: dict[str, Any]) -> None:
    SCRIPT_API["validate_registry"](registry, _schema())


def test_registry_validates_against_committed_schema() -> None:
    registry = _registry()
    schema = _schema()

    assert registry["$schema"] == "./claims.schema.json"
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["properties"]["registry_version"]["const"] == 1
    assert {claim["status"] for claim in registry["claims"]} == {
        "active",
        "withdrawn",
        "withheld",
    }
    _validate(registry)


def test_generated_evidence_mirrors_are_current() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_generated_block_replacement_treats_backslashes_literally() -> None:
    begin = SCRIPT_API["BEGIN"]
    end = SCRIPT_API["END"]
    current = f"before\n{begin}\nold\n{end}\nafter\n"
    block = f"{begin}\nliteral \\1 and \\g<claim>\n{end}"

    assert SCRIPT_API["replace_block"](current, block, ROOT / "README.md") == (
        f"before\n{block}\nafter\n"
    )


def test_every_claim_has_structured_evidence_metadata() -> None:
    required = {
        "last_verified_at",
        "approved_wording",
        "evaluation",
        "answering_model",
        "judge",
        "configuration",
        "code_commit",
        "methodology_url",
        "raw_result_url",
        "reproducible_harness_url",
    }
    for claim in _registry()["claims"]:
        assert required <= claim.keys()
        assert {
            "population",
            "sample_size",
            "numerator",
            "denominator",
            "dataset",
            "dataset_version",
            "sampling_notes",
            "null_reason",
        } <= claim["evaluation"].keys()
        for model_field in ("answering_model", "judge"):
            assert {
                "applicable",
                "provider",
                "model",
                "immutable_snapshot",
                "notes",
            } <= claim[model_field].keys()
        assert {"description", "parameters"} <= claim["configuration"].keys()
        assert {"repository", "commit", "null_reason"} <= claim["code_commit"].keys()
        if claim["status"] == "active":
            assert claim["approved_wording"]
            assert claim["methodology_url"]
            assert claim["raw_result_url"] or claim["reproducible_harness_url"]


def test_percentage_must_match_its_fraction() -> None:
    broken = copy.deepcopy(_registry())
    claim = next(
        item
        for item in broken["claims"]
        if item["id"] == "locomo_caura_retrieval_accuracy_2026_09"
    )
    claim["measurements"][0]["value"] = 87.27

    with pytest.raises(ValueError, match="disagrees with 1199/1540"):
        _validate(broken)


def test_active_percentage_must_appear_in_approved_wording() -> None:
    broken = copy.deepcopy(_registry())
    claim = next(
        item
        for item in broken["claims"]
        if item["id"] == "locomo_caura_retrieval_accuracy_2026_09"
    )
    claim["approved_wording"] = "Caura passed the documented evaluation."

    with pytest.raises(ValueError, match=r"approved_wording must include 77\.9%"):
        _validate(broken)


@pytest.mark.parametrize(
    ("label", "value", "expected"),
    [
        ("Median retrieved context", 30_000, "Context token savings disagrees"),
        (
            "All-reader-call token savings",
            88.8,
            "All-reader-call token savings disagrees",
        ),
    ],
)
def test_token_savings_must_match_token_counts(
    label: str, value: float, expected: str
) -> None:
    broken = copy.deepcopy(_registry())
    claim = next(
        item
        for item in broken["claims"]
        if item["id"] == "longmemeval_token_savings_2026_09_15"
    )
    measurement = next(item for item in claim["measurements"] if item["label"] == label)
    measurement["value"] = value
    if measurement["unit"] == "percent":
        measurement["display"] = f"{value}%"
        claim["approved_wording"] = claim["approved_wording"].replace(
            "75.4%", f"{value}%"
        )

    with pytest.raises(ValueError, match=expected):
        _validate(broken)


def test_active_token_savings_requires_all_inputs() -> None:
    broken = copy.deepcopy(_registry())
    claim = next(
        item
        for item in broken["claims"]
        if item["id"] == "longmemeval_token_savings_2026_09_15"
    )
    claim["measurements"] = [
        item
        for item in claim["measurements"]
        if item["label"] != "Median full haystack"
    ]

    with pytest.raises(ValueError, match="missing Median full haystack"):
        _validate(broken)


def test_public_surface_inventory_covers_package_docs() -> None:
    surfaces = set(SCRIPT_API["_public_docs"]())
    assert ROOT / "CHANGELOG.md" in surfaces
    assert ROOT / "plugin" / "CHANGELOG.md" in surfaces
    assert ROOT / "clients" / "typescript" / "README.md" in surfaces
    assert ROOT / "plugin" / "package.json" in surfaces
    assert ROOT / "EVIDENCE.md" not in surfaces


def test_active_methodology_url_must_pin_code_commit() -> None:
    broken = copy.deepcopy(_registry())
    claim = next(
        item
        for item in broken["claims"]
        if item["id"] == "locomo_caura_retrieval_accuracy_2026_09"
    )
    claim["methodology_url"] = "https://github.com/caura-ai/yanki-locomo/README.md"

    with pytest.raises(ValueError, match="methodology_url must pin code_commit"):
        _validate(broken)


def test_registry_updated_at_covers_all_claim_metadata() -> None:
    broken = copy.deepcopy(_registry())
    broken["updated_at"] = "2026-10-05"

    with pytest.raises(ValueError, match="predates claim metadata dated 2026-10-06"):
        _validate(broken)


@pytest.mark.parametrize(
    "date_field",
    [
        "registry",
        "claim_measurement",
        "last_verified",
        "measurement",
        "withdrawal",
        "withholding",
    ],
)
def test_future_dates_are_rejected(date_field: str) -> None:
    broken = copy.deepcopy(_registry())
    claims = {claim["id"]: claim for claim in broken["claims"]}
    headline = claims["locomo_caura_retrieval_accuracy_2026_09"]

    if date_field == "registry":
        broken["updated_at"] = "2999-01-01"
    elif date_field == "claim_measurement":
        headline["measurement_at"] = "2999-01-01"
    elif date_field == "last_verified":
        headline["last_verified_at"] = "2999-01-01"
    elif date_field == "measurement":
        headline["measurements"][0]["measurement_at"] = "2999-01-01"
    elif date_field == "withdrawal":
        claims["locomo_token_savings_2026_04"]["withdrawal"]["at"] = "2999-01-01"
    else:
        claims["search_latency_warm_single_tenant_2026_04_19"]["withholding"]["at"] = (
            "2999-01-01"
        )

    with pytest.raises(ValueError, match="cannot be in the future"):
        _validate(broken)


def test_generated_blocks_contain_only_active_approved_wording() -> None:
    registry = _registry()
    expected = SCRIPT_API["expected_files"](registry)
    inactive_values = ("87.27%", "96.6%", "23 ms", "27 ms", "300+", "26,500+", "1,372")

    for path in (
        ROOT / "README.md",
        ROOT / "BENCHMARKS.md",
        ROOT / "docs" / "performance.md",
        ROOT / "AGENT-INSTALL.md",
    ):
        content = expected[path]
        block = content.split(SCRIPT_API["BEGIN"], 1)[1].split(SCRIPT_API["END"], 1)[0]
        assert all(value not in block for value in inactive_values)

    block = SCRIPT_API["render_public_block"](registry)
    claims = {claim["id"]: claim for claim in registry["claims"]}
    approved_ids = (
        "locomo_caura_retrieval_accuracy_2026_09",
        "longmemeval_reference_accuracy_2026_09_15",
        "longmemeval_secondary_accuracy_2026_09_15",
        "longmemeval_token_savings_2026_09_15",
    )
    for claim_id in approved_ids:
        assert claims[claim_id]["status"] == "active"
        assert claims[claim_id]["approved_wording"] in block


def test_full_context_baseline_stays_withheld_until_owner_approval() -> None:
    claim = next(
        item
        for item in _registry()["claims"]
        if item["id"] == "locomo_full_context_control_2026_09"
    )

    assert claim["status"] == "withheld"
    assert claim["approved_wording"] is None
    assert (
        claim["code_commit"]["repository"] == "https://github.com/caura-ai/caura-locomo"
    )
    assert claim["raw_result_url"].endswith("outputs/uniform/results_judge_gpt-4o.json")
    assert claim["reproducible_harness_url"].endswith(
        "c55d3a3fe8df01533690c7f2e6874e6a0e67c9be"
    )
    assert "owner confirmation" in claim["evidence_gap"].lower()
    assert "87.27%" not in SCRIPT_API["render_public_block"](_registry())


def test_article_only_latency_claim_stays_withheld() -> None:
    claim = next(
        item
        for item in _registry()["claims"]
        if item["id"] == "search_latency_warm_single_tenant_2026_04_19"
    )

    assert claim["status"] == "withheld"
    assert claim["approved_wording"] is None
    assert claim["raw_result_url"] is None
    assert claim["reproducible_harness_url"] is None
    assert claim["evidence_gap"]


@pytest.mark.parametrize(
    "denied_copy",
    [
        "Caura scored 87.5% on LoCoMo.",
        "The full-context control scored 87.27%.",
        "Caura scored 77.6% on LoCoMo.",
        "Caura saved 96.6% of tokens.",
        "Search returns in 23 ms p50.",
        "Search returns in 27 ms p95.",
        "Used by 300+ AI agents.",
        "Stores 26,500+ memories.",
        "Shares 1,372 skills.",
    ],
)
def test_current_surface_denylist_rejects_stale_or_withheld_copy(
    denied_copy: str,
) -> None:
    with pytest.raises(ValueError, match="public evidence denylist failed"):
        SCRIPT_API["validate_public_surfaces"]({ROOT / "README.md": denied_copy})


def test_active_values_may_not_drift_outside_generated_blocks() -> None:
    with pytest.raises(ValueError, match="outside the generated evidence block"):
        SCRIPT_API["validate_public_surfaces"](
            {ROOT / "README.md": "Caura scored 92.2% in a manually copied paragraph."}
        )


def test_withdrawn_value_is_retained_only_in_evidence_history() -> None:
    evidence = (ROOT / "EVIDENCE.md").read_text(encoding="utf-8")
    public_files = [
        ROOT / "README.md",
        ROOT / "AGENT-INSTALL.md",
        ROOT / "BENCHMARKS.md",
        *sorted((ROOT / "docs").rglob("*.md")),
    ]

    assert "96.6%" in evidence
    assert all("96.6%" not in path.read_text(encoding="utf-8") for path in public_files)


def test_unverified_adoption_counts_are_not_promotional_copy() -> None:
    promotional = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "README.md", ROOT / "AGENT-INSTALL.md")
    )

    assert "300+ agents" not in promotional
    assert "300+ AI agents" not in promotional
    assert "26,500+ memories" not in promotional
    assert "1,372 shared skills" not in promotional
