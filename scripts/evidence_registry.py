"""Validate the public claims registry and keep its Markdown mirrors in sync."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

try:
    import jsonschema
except ImportError:  # pragma: no cover - exercised by the dependency check in CI
    jsonschema = None

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "evidence" / "claims.json"
SCHEMA_PATH = ROOT / "evidence" / "claims.schema.json"
BEGIN = "<!-- BEGIN GENERATED: evidence-benchmarks -->"
END = "<!-- END GENERATED: evidence-benchmarks -->"

GENERATED_BLOCK_SURFACES = (
    ROOT / "README.md",
    ROOT / "BENCHMARKS.md",
    ROOT / "docs" / "performance.md",
    ROOT / "AGENT-INSTALL.md",
)
PUBLIC_DOC_ROOTS = (
    ROOT / "README.md",
    ROOT / "AGENT-INSTALL.md",
    ROOT / "BENCHMARKS.md",
)
PUBLIC_TEXT_TREES = (
    ROOT / "clients",
    ROOT / "plugin",
    ROOT / "static" / "docs",
)
PUBLIC_TEXT_FILES = (
    ROOT / "plugin" / "package.json",
    ROOT / "clients" / "typescript" / "package.json",
)

# Current promotional surfaces fail closed on known stale, control-only, withheld,
# or unproven values. The canonical registry and EVIDENCE.md are deliberately not
# scanned: they retain explicit withdrawal/withholding history.
DENIED_PUBLIC_PATTERNS = {
    r"87\.5\s*%": "87.5% is not an approved Caura LoCoMo result",
    r"87\.27\s*%": "87.27% is a withheld full-context control",
    r"77\.6\s*%": "77.6% is the stale April LoCoMo result",
    r"96\.6\s*%": "96.6% is a withdrawn LoCoMo token-savings claim",
    r"23\s*(?:ms|milliseconds)\s*p50": "23 ms p50 latency is withheld",
    r"27\s*(?:ms|milliseconds)\s*p95": "27 ms p95 latency is withheld",
    r"26,?500\+?\s+memories": "the eToro memory count is withheld",
    r"1,?372\s+(?:shared\s+)?skills": "the eToro skill count is withheld",
    r"300\+\s+(?:AI\s+)?agents": "the eToro agent count is withheld",
    r"291\s+agent identifiers": "the eToro agent-identifier count is withheld",
    r"100,?000\+?\s+downloads": "the download count lacks registry evidence",
}

# Distinctive active benchmark values may appear on current public surfaces only
# inside generated blocks. This prevents a registry update from leaving a
# manually duplicated, now-stale value elsewhere in README/docs.
GOVERNED_CURRENT_PATTERNS = {
    r"77\.9\s*%": "LoCoMo accuracy",
    r"92\.2\s*%": "LongMemEval reference-judge accuracy",
    r"90\.2\s*%": "LongMemEval secondary-judge accuracy",
    r"79\.2\s*%": "LongMemEval context-only savings",
    r"75\.4\s*%": "LongMemEval all-reader-call savings",
    r"22,?410\s+tokens": "LongMemEval retrieved-context median",
    r"107,?706(?:-token|\s+tokens)": "LongMemEval full-haystack median",
}


class RegistryError(ValueError):
    """The evidence registry violates its checked contract."""


def _parse_date(value: object, field: str) -> date:
    if not isinstance(value, str):
        raise RegistryError(f"{field} must be an ISO date string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise RegistryError(f"{field} must be an ISO date: {value!r}") from exc
    today = datetime.now(UTC).date()
    if parsed > today:
        raise RegistryError(f"{field} cannot be in the future: {value}")
    return parsed


def _validate_schema(instance: dict[str, Any], schema: dict[str, Any]) -> None:
    if jsonschema is None:
        raise RegistryError(
            "jsonschema is required; install the pinned CI dependency "
            "jsonschema==4.25.1"
        )
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(
        schema,
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )
    errors = sorted(validator.iter_errors(instance), key=lambda item: list(item.path))
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise RegistryError(
            f"JSON Schema validation failed at {location}: {error.message}"
        )


def _validate_model(claim_id: str, name: str, model: dict[str, Any]) -> None:
    if model["applicable"]:
        if not model["provider"] or not model["model"]:
            raise RegistryError(f"{claim_id}.{name}: applicable model must be named")
        if (
            model["immutable_snapshot"] is None
            and "snapshot" not in model["notes"].lower()
        ):
            raise RegistryError(
                f"{claim_id}.{name}: missing immutable snapshot needs an explicit note"
            )
    elif any(
        model[field] is not None
        for field in ("provider", "model", "immutable_snapshot")
    ):
        raise RegistryError(
            f"{claim_id}.{name}: non-applicable model fields must be null"
        )


def validate_registry(registry: dict[str, Any], schema: dict[str, Any]) -> None:
    """Validate schema plus cross-field and time-dependent evidence invariants."""
    _validate_schema(registry, schema)
    registry_updated_at = _parse_date(registry["updated_at"], "updated_at")
    latest_claim_date = date.min

    ids: set[str] = set()
    for claim in registry["claims"]:
        claim_id = claim["id"]
        if claim_id in ids:
            raise RegistryError(f"duplicate claim id: {claim_id}")
        ids.add(claim_id)
        status = claim["status"]

        if claim["measurement_at"] is not None:
            latest_claim_date = max(
                latest_claim_date,
                _parse_date(claim["measurement_at"], f"{claim_id}.measurement_at"),
            )
        elif status == "active":
            raise RegistryError(f"{claim_id}: active claims need measurement_at")
        latest_claim_date = max(
            latest_claim_date,
            _parse_date(claim["last_verified_at"], f"{claim_id}.last_verified_at"),
        )
        for index, measurement in enumerate(claim["measurements"]):
            measured = measurement["measurement_at"]
            if measured is not None:
                latest_claim_date = max(
                    latest_claim_date,
                    _parse_date(
                        measured,
                        f"{claim_id}.measurements[{index}].measurement_at",
                    ),
                )
            elif status == "active":
                raise RegistryError(f"{claim_id}: active measurements need a date")

        for disposition_name in ("withdrawal", "withholding"):
            disposition = claim[disposition_name]
            if disposition is not None:
                latest_claim_date = max(
                    latest_claim_date,
                    _parse_date(disposition["at"], f"{claim_id}.{disposition_name}.at"),
                )

        _validate_model(claim_id, "answering_model", claim["answering_model"])
        _validate_model(claim_id, "judge", claim["judge"])

        evaluation = claim["evaluation"]
        counts = (
            evaluation["sample_size"],
            evaluation["numerator"],
            evaluation["denominator"],
        )
        if any(item is None for item in counts) and not evaluation["null_reason"]:
            raise RegistryError(f"{claim_id}: null evaluation fields need null_reason")
        if all(item is not None for item in counts):
            sample_size, numerator, denominator = counts
            if sample_size != denominator or not 0 <= numerator <= denominator:
                raise RegistryError(f"{claim_id}: invalid evaluation counts")
            percent_measurements = [
                item for item in claim["measurements"] if item["unit"] == "percent"
            ]
            if claim["metric"] == "accuracy" and percent_measurements:
                published = percent_measurements[0]["value"]
                decimals = len(str(published).partition(".")[2])
                tolerance = 0.5 * 10 ** (-decimals)
                computed = 100 * numerator / denominator
                if abs(computed - published) > tolerance:
                    raise RegistryError(
                        f"{claim_id}: {published}% disagrees with "
                        f"{numerator}/{denominator} ({computed:.4f}%)"
                    )

        if status == "active":
            if not claim["methodology_url"]:
                raise RegistryError(f"{claim_id}: active claim needs methodology_url")
            if not (claim["raw_result_url"] or claim["reproducible_harness_url"]):
                raise RegistryError(
                    f"{claim_id}: active claim needs raw result or harness"
                )
            commit = claim["code_commit"]["commit"]
            if not commit:
                raise RegistryError(
                    f"{claim_id}: active claim needs an immutable code commit"
                )
            for field in (
                "methodology_url",
                "raw_result_url",
                "reproducible_harness_url",
            ):
                url = claim[field]
                if url and commit not in url:
                    raise RegistryError(f"{claim_id}.{field} must pin code_commit")
            approved_wording = claim["approved_wording"]
            for measurement in claim["measurements"]:
                if (
                    measurement["unit"] == "percent"
                    and measurement["display"] not in approved_wording
                ):
                    raise RegistryError(
                        f"{claim_id}: approved_wording must include "
                        f"{measurement['display']}"
                    )
            if claim["metric"] == "token_savings":
                measurements = {
                    measurement["label"]: measurement
                    for measurement in claim["measurements"]
                }
                required_labels = (
                    "Median retrieved context",
                    "Median all-reader-call total",
                    "Median full haystack",
                    "Context token savings",
                    "All-reader-call token savings",
                )
                missing = [
                    label for label in required_labels if label not in measurements
                ]
                if missing:
                    raise RegistryError(
                        f"{claim_id}: active token_savings claim is missing "
                        + ", ".join(missing)
                    )
                haystack = measurements["Median full haystack"]["value"]
                for token_label, percent_label in (
                    ("Median retrieved context", "Context token savings"),
                    (
                        "Median all-reader-call total",
                        "All-reader-call token savings",
                    ),
                ):
                    tokens = measurements[token_label]["value"]
                    published = measurements[percent_label]["value"]
                    decimals = len(str(published).partition(".")[2])
                    tolerance = 0.5 * 10 ** (-decimals)
                    computed = 100 * (1 - tokens / haystack)
                    if abs(computed - published) > tolerance:
                        raise RegistryError(
                            f"{claim_id}: {published}% {percent_label} disagrees "
                            f"with {tokens}/{haystack} tokens ({computed:.4f}%)"
                        )
        else:
            if not claim["methodology_url"] and claim["metric"] != "adoption":
                raise RegistryError(
                    f"{claim_id}: non-adoption claim needs methodology_url"
                )
            if (
                not (claim["raw_result_url"] or claim["reproducible_harness_url"])
                and not claim["evidence_gap"]
            ):
                raise RegistryError(f"{claim_id}: missing evidence needs evidence_gap")
            code = claim["code_commit"]
            if code["commit"] is None and not code["null_reason"]:
                raise RegistryError(
                    f"{claim_id}: null code commit needs an explicit null_reason"
                )

    if registry_updated_at < latest_claim_date:
        raise RegistryError(
            f"updated_at {registry_updated_at.isoformat()} predates claim metadata "
            f"dated {latest_claim_date.isoformat()}"
        )


def load_registry() -> tuple[dict[str, Any], dict[str, Any]]:
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(registry, dict) or not isinstance(schema, dict):
        raise RegistryError("registry and schema roots must be objects")
    validate_registry(registry, schema)
    return registry, schema


def _claims_by_id(registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {claim["id"]: claim for claim in registry["claims"]}


def _approved(claims: dict[str, dict[str, Any]], claim_id: str) -> str:
    claim = claims[claim_id]
    if claim["status"] != "active" or not claim["approved_wording"]:
        raise RegistryError(f"{claim_id} is not approved for current public copy")
    return claim["approved_wording"]


def render_public_block(registry: dict[str, Any], *, relative_prefix: str = "") -> str:
    claims = _claims_by_id(registry)
    rows = (
        ("LoCoMo accuracy", "locomo_caura_retrieval_accuracy_2026_09"),
        ("LongMemEval reference judge", "longmemeval_reference_accuracy_2026_09_15"),
        ("LongMemEval secondary judge", "longmemeval_secondary_accuracy_2026_09_15"),
        ("LongMemEval token efficiency", "longmemeval_token_savings_2026_09_15"),
    )
    lines = [BEGIN]
    lines.extend(
        f"- **{label}:** {_approved(claims, claim_id)}" for label, claim_id in rows
    )
    lines.extend(
        [
            "",
            "Only active, approved claims appear here. Control, withdrawn, and withheld",
            "records remain in the evidence registry and are excluded from promotional copy.",
            f"See [`{relative_prefix}evidence/claims.json`]({relative_prefix}evidence/claims.json) and",
            f"[`{relative_prefix}EVIDENCE.md`]({relative_prefix}EVIDENCE.md). Do not hand-edit this block.",
            END,
        ]
    )
    return "\n".join(lines)


def render_agent_install_block(registry: dict[str, Any]) -> str:
    return render_public_block(registry)


def render_evidence(registry: dict[str, Any]) -> str:
    lines = [
        "# Caura Evidence Registry",
        "",
        "<!-- Generated by scripts/evidence_registry.py from evidence/claims.json. Do not edit by hand. -->",
        "",
        f"Registry version: **{registry['registry_version']}** · Updated: **{registry['updated_at']}**",
        "",
        "The JSON registry is canonical. Current public copy may use only an active",
        "claim's exact `approved_wording`; controls, withdrawals, and withheld records",
        "are retained here for auditability and excluded from promotional blocks.",
        "",
        "## Approved current wording",
        "",
        render_public_block(registry)
        .split(BEGIN + "\n", 1)[1]
        .rsplit("\n" + END, 1)[0],
        "",
        "## All claims",
    ]
    for claim in registry["claims"]:
        code = claim["code_commit"]
        model = claim["answering_model"]
        judge = claim["judge"]
        lines.extend(
            [
                "",
                f"### {claim['title']}",
                "",
                f"- **ID:** `{claim['id']}`",
                f"- **Status / role:** {claim['status'].upper()} / {claim['role']}",
                f"- **Measurement / verified:** {claim['measurement_at'] or 'unknown'} / {claim['last_verified_at']}",
                f"- **Approved wording:** {claim['approved_wording'] or 'None — not approved for current promotional copy.'}",
                f"- **Scope:** {claim['scope']}",
                f"- **Evaluation:** {claim['evaluation']['population']}; sample={claim['evaluation']['sample_size']}; numerator={claim['evaluation']['numerator']}; denominator={claim['evaluation']['denominator']}; {claim['evaluation']['sampling_notes']}",
                f"- **Answering model:** {model['provider'] or 'not applicable'} / {model['model'] or 'not applicable'}; snapshot={model['immutable_snapshot'] or 'not recorded/not applicable'}. {model['notes']}",
                f"- **Judge:** {judge['provider'] or 'not applicable'} / {judge['model'] or 'not applicable'}; snapshot={judge['immutable_snapshot'] or 'not recorded/not applicable'}. {judge['notes']}",
                f"- **Configuration:** {claim['configuration']['description']} Parameters: `{json.dumps(claim['configuration']['parameters'], sort_keys=True)}`",
                f"- **Code:** {code['repository'] or 'unavailable'} @ `{code['commit'] or 'unavailable'}`. {code['null_reason'] or ''}".rstrip(),
                f"- **Measurements:** {'; '.join(item['display'] + ' ' + item['label'] for item in claim['measurements'])}",
                f"- **Methodology:** {claim['methodology_url'] or 'unavailable'}",
                f"- **Raw result:** {claim['raw_result_url'] or 'unavailable'}",
                f"- **Reproducible harness:** {claim['reproducible_harness_url'] or 'unavailable'}",
                f"- **Evidence gap:** {claim['evidence_gap'] or 'None recorded.'}",
                f"- **Caveats:** {' '.join(claim['caveats'])}",
            ]
        )
        if claim["withdrawal"]:
            lines.append(
                f"- **Withdrawn {claim['withdrawal']['at']}:** {claim['withdrawal']['reason']}"
            )
        if claim["withholding"]:
            lines.append(
                f"- **Withheld {claim['withholding']['at']}:** {claim['withholding']['reason']}"
            )
    lines.extend(
        [
            "",
            "## Updating a claim",
            "",
            "1. Update `evidence/claims.json`; never erase withdrawal or withholding history.",
            "2. Run `python3 scripts/evidence_registry.py --write`.",
            "3. Run `python3 scripts/evidence_registry.py --check` and the focused tests.",
            "4. Review the generated diff and the public-surface denylist result.",
            "",
        ]
    )
    return "\n".join(lines)


def replace_block(current: str, block: str, path: Path) -> str:
    pattern = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.DOTALL)
    matches = list(pattern.finditer(current))
    if len(matches) != 1:
        raise RegistryError(
            f"{path.relative_to(ROOT)} must contain exactly one generated block; "
            f"found {len(matches)}"
        )
    return pattern.sub(lambda _match: block, current, count=1)


def expected_files(registry: dict[str, Any]) -> dict[Path, str]:
    blocks = {
        GENERATED_BLOCK_SURFACES[0]: render_public_block(registry),
        GENERATED_BLOCK_SURFACES[1]: render_public_block(registry),
        GENERATED_BLOCK_SURFACES[2]: render_public_block(
            registry, relative_prefix="../"
        ),
        GENERATED_BLOCK_SURFACES[3]: render_agent_install_block(registry),
    }
    expected = {ROOT / "EVIDENCE.md": render_evidence(registry)}
    for path, block in blocks.items():
        expected[path] = replace_block(path.read_text(encoding="utf-8"), block, path)
    return expected


def _public_docs() -> list[Path]:
    docs = set(PUBLIC_DOC_ROOTS)
    docs.update(ROOT.glob("*.md"))
    docs.discard(ROOT / "EVIDENCE.md")
    docs.update((ROOT / "docs").rglob("*.md"))
    for tree in PUBLIC_TEXT_TREES:
        docs.update(tree.rglob("*.md"))
        docs.update(tree.rglob("*.txt"))
    docs.update(path for path in PUBLIC_TEXT_FILES if path.is_file())
    return sorted(docs)


def validate_public_surfaces(expected: dict[Path, str]) -> None:
    failures: list[str] = []
    generated_pattern = re.compile(
        re.escape(BEGIN) + r".*?" + re.escape(END), re.DOTALL
    )
    for path in _public_docs():
        content = expected.get(path, path.read_text(encoding="utf-8"))
        for pattern, reason in DENIED_PUBLIC_PATTERNS.items():
            match = re.search(pattern, content, flags=re.IGNORECASE)
            if match:
                line = content.count("\n", 0, match.start()) + 1
                failures.append(f"{path.relative_to(ROOT)}:{line}: {reason}")
        if path.name == "CHANGELOG.md":
            # Release history may quote an active result from the release that
            # introduced it. Withheld/withdrawn values remain denied above,
            # but current-value generation is not expected in changelogs.
            continue
        ungoverned_content = generated_pattern.sub("", content)
        for pattern, label in GOVERNED_CURRENT_PATTERNS.items():
            match = re.search(pattern, ungoverned_content, flags=re.IGNORECASE)
            if match:
                failures.append(
                    f"{path.relative_to(ROOT)}: active {label} value appears "
                    "outside the generated evidence block"
                )
    if failures:
        raise RegistryError(
            "public evidence denylist failed:\n  " + "\n  ".join(failures)
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="fail if evidence drifts")
    action.add_argument(
        "--write", action="store_true", help="regenerate Markdown mirrors"
    )
    args = parser.parse_args()

    try:
        registry, _schema = load_registry()
        expected = expected_files(registry)
        validate_public_surfaces(expected)
    except (json.JSONDecodeError, OSError, RegistryError) as exc:
        print(f"evidence registry error: {exc}", file=sys.stderr)
        return 1

    drifted: list[Path] = []
    for path, content in expected.items():
        final_content = content.rstrip() + "\n"
        if args.write:
            path.write_text(final_content, encoding="utf-8")
        elif not path.is_file() or path.read_text(encoding="utf-8") != final_content:
            drifted.append(path)
    if drifted:
        print("Evidence mirrors are stale:", file=sys.stderr)
        for path in drifted:
            print(f"  {path.relative_to(ROOT)}", file=sys.stderr)
        print("Run: python3 scripts/evidence_registry.py --write", file=sys.stderr)
        return 1
    print(
        f"Evidence registry and {len(expected)} mirror(s) are current."
        if args.check
        else f"Updated {len(expected)} evidence mirror(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
