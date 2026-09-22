"""Post-conditions for migrations that deliberately tolerate DDL failures."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PostconditionSeverity = Literal["error", "warning"]


@dataclass(frozen=True)
class MigrationPostcondition:
    """A database predicate that proves a soft-failing migration took effect."""

    revision: str
    name: str
    predicate: str
    severity: PostconditionSeverity
    message: str


MIGRATION_POSTCONDITIONS: tuple[MigrationPostcondition, ...] = (
    # 044's message deliberately does NOT claim live harm. It once said the planner
    # was "under-pricing <=> by ~100x", which reads as an incident on every boot of
    # every managed-Postgres deployment — precisely the deployments where the app
    # user cannot own the pgvector extension and the migration can therefore never
    # apply. Measured 2026-09-22 against a 2.1M-row production table still at
    # procost 1: the filtered ANN arm was already served by the HNSW index (233,662
    # index scans; sequential access 0.03% of all scans, averaging 33k rows and so
    # never the sort-fed full scan an ANN fallback would require). The mispricing is
    # real and worth repairing where someone owns the extension; it is not, on that
    # evidence, a defect in flight. Severity stays "warning" so the condition stays
    # visible — the wording, not the visibility, was what misrepresented it.
    MigrationPostcondition(
        revision="044",
        name="cosine_distance_procost",
        predicate="SELECT procost <> 1 FROM pg_proc WHERE oid = "
        "to_regprocedure('cosine_distance(vector, vector)')",
        severity="warning",
        message=(
            "cosine_distance(vector, vector) still has the default procost, so migration 044 "
            "did not apply here. Expected wherever the app user does not own the pgvector "
            "extension, which is the normal case on managed Postgres. It is latent rather than "
            "a live defect: it changes plan choice only where the planner would otherwise pick "
            "a sequential scan over the HNSW index, so check idx_scan on that index in "
            "pg_stat_user_indexes before treating it as urgent. Repair is manual and optional: "
            "connect as the pgvector extension owner and run "
            "ALTER FUNCTION cosine_distance(vector, vector) COST 100; "
            "re-running the migration will never fix an ownership failure."
        ),
    ),
)
