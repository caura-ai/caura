"""Pre-flight script: pure formatting + decision logic.

The script's DB-touching function (``_gather``) needs a real Postgres,
so it's exercised by the staging cutover runbook (Spec E), not here.
This file covers the report-formatting + exit-code-decision logic in
isolation — fast unit-only coverage.
"""

from __future__ import annotations

import json

import pytest

from core_storage_api.scripts.preflight_012 import (
    PreflightReport,
    TableReport,
    _amain,
    _fmt_seconds,
    _format_human,
)


def _make_report(*, total: int, head: str | None = "010") -> PreflightReport:
    """Single-table report, with totals derived to match.

    ``head="010"`` mirrors the post-CAURA-000 / pre-011 state — the
    most common 'is the migration safe?' question on real clusters.
    """
    tables = [
        TableReport(
            table="memories",
            column="embedding",
            null_count=0,
            non_null_count=total,
            estimated_update_seconds=total / 50_000,
        )
    ]
    return PreflightReport(
        head_revision=head,
        tables=tables,
        total_rows_to_null=total,
        total_estimated_seconds=total / 50_000,
        safety_gate_would_trip=total > 0,
    )


@pytest.mark.unit
def test_safety_gate_flag_set_when_rows_present() -> None:
    """Any non-zero count of non-NULL embedding rows → gate would trip.

    The migration's safety gate sums all three target tables; this
    pre-flight mirrors that by setting ``safety_gate_would_trip`` from
    ``total_rows_to_null > 0``."""
    r = _make_report(total=42_000)
    assert r.safety_gate_would_trip is True


@pytest.mark.unit
def test_safety_gate_flag_unset_on_empty_db() -> None:
    """Zero rows-to-null → migration is a no-op for the safety gate
    and the operator gets the green-light path."""
    r = _make_report(total=0)
    assert r.safety_gate_would_trip is False


@pytest.mark.unit
def test_human_output_includes_opt_in_command_when_destructive() -> None:
    """Destructive case: surface the exact env-var + alembic command
    AND the follow-up backfill so the operator can copy-paste the
    full sequence."""
    out = _format_human(_make_report(total=10_000))
    assert "MEMCLAW_RUN_DESTRUCTIVE_MIGRATIONS=true" in out
    assert "alembic upgrade 012" in out
    assert "backfill_embeddings" in out


@pytest.mark.unit
def test_human_output_calls_safe_when_empty_db() -> None:
    """Empty DB: no opt-in env var should appear (would confuse the
    operator into thinking they need it)."""
    out = _format_human(_make_report(total=0))
    assert "safe to run" in out
    assert "MEMCLAW_RUN_DESTRUCTIVE_MIGRATIONS" not in out
    # The plain ``alembic upgrade 012`` command is still printed.
    assert "alembic upgrade 012" in out


@pytest.mark.unit
def test_human_output_renders_head_revision() -> None:
    r = _make_report(total=0, head="010")
    assert "Alembic head:" in _format_human(r)
    assert "010" in _format_human(r)


@pytest.mark.unit
def test_human_output_renders_fresh_db_head() -> None:
    """When alembic_version doesn't exist (fresh DB), head is None and
    we render a friendly placeholder rather than ``None``."""
    out = _format_human(_make_report(total=0, head=None))
    assert "(none — fresh DB)" in out


@pytest.mark.unit
@pytest.mark.parametrize(
    "secs,expected",
    [
        (0, "0s"),
        (45, "45s"),
        (119, "119s"),
        (120, "2.0m"),
        (600, "10.0m"),
        (3599, "60.0m"),
        (3600, "1.0h"),
        (7200, "2.0h"),
    ],
)
def test_fmt_seconds_thresholds(secs: float, expected: str) -> None:
    """Format crosses s → m at 120, m → h at 3600. The boundary
    behaviour matters because operators often see counts near these
    thresholds and we don't want '120s' next to '2.0m' in the same
    table."""
    assert _fmt_seconds(secs) == expected


# ---------------------------------------------------------------------------
# CLI exit-code coverage
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_amain_returns_0_on_safe_db(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Empty DB → exit 0, no opt-in command printed."""
    from core_storage_api.scripts import preflight_012

    async def _fake_gather(_dsn: str, _rps: int) -> PreflightReport:
        return _make_report(total=0, head="010")

    monkeypatch.setattr(preflight_012, "_gather", _fake_gather)
    code = await _amain(["--dsn", "postgresql+asyncpg://x/y"])
    assert code == 0
    out = capsys.readouterr().out
    assert "safe to run" in out


@pytest.mark.unit
@pytest.mark.asyncio
async def test_amain_returns_1_on_destructive_db(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rows present → exit 1 (distinct from 0=safe and 2=error so
    monitoring can branch on it)."""
    from core_storage_api.scripts import preflight_012

    async def _fake_gather(_dsn: str, _rps: int) -> PreflightReport:
        return _make_report(total=42_000, head="010")

    monkeypatch.setattr(preflight_012, "_gather", _fake_gather)
    code = await _amain(["--dsn", "postgresql+asyncpg://x/y"])
    assert code == 1
    assert "MEMCLAW_RUN_DESTRUCTIVE_MIGRATIONS" in capsys.readouterr().out


@pytest.mark.unit
@pytest.mark.asyncio
async def test_amain_returns_2_on_connection_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Any exception from ``_gather`` (DB unreachable, auth fail, query
    error) maps to exit 2 with a single-line stderr diagnostic. No
    traceback by design — keeps operator output clean."""
    from core_storage_api.scripts import preflight_012

    async def _explode(_dsn: str, _rps: int) -> PreflightReport:
        raise ConnectionRefusedError("could not connect to localhost:5432")

    monkeypatch.setattr(preflight_012, "_gather", _explode)
    code = await _amain(["--dsn", "postgresql+asyncpg://x/y"])
    assert code == 2
    err = capsys.readouterr().err
    assert "preflight error" in err
    assert "5432" in err


@pytest.mark.unit
@pytest.mark.asyncio
async def test_amain_emits_parseable_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--json`` produces a single line that ``json.loads`` accepts
    and contains the dataclass fields (smoke-checked for downstream
    pipe-into-jq use)."""
    from core_storage_api.scripts import preflight_012

    async def _fake_gather(_dsn: str, _rps: int) -> PreflightReport:
        return _make_report(total=1_000, head="010")

    monkeypatch.setattr(preflight_012, "_gather", _fake_gather)
    await _amain(["--dsn", "postgresql+asyncpg://x/y", "--json"])
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["head_revision"] == "010"
    assert payload["total_rows_to_null"] == 1_000
    assert payload["safety_gate_would_trip"] is True
    assert isinstance(payload["tables"], list)
