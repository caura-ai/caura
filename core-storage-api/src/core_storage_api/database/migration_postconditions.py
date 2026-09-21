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
    MigrationPostcondition(
        revision="044",
        name="cosine_distance_procost",
        predicate="SELECT procost <> 1 FROM pg_proc WHERE oid = "
        "to_regprocedure('cosine_distance(vector, vector)')",
        severity="warning",
        message=(
            "cosine_distance(vector, vector) still has the default procost, so migration 044 "
            "did not apply and the planner is under-pricing <=> by ~100x. Repair is manual: "
            "connect as the pgvector extension owner and run "
            "ALTER FUNCTION cosine_distance(vector, vector) COST 100; "
            "re-running the migration will never fix an ownership failure."
        ),
    ),
)
