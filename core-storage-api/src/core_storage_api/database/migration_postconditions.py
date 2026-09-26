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
    # SQL returning true where THIS deployment could never have applied the
    # migration, so an unmet post-condition is the documented outcome rather
    # than a defect.
    #
    # A soft-failing migration promises "apply this if you are allowed to", and
    # until now the post-condition tested the stronger "this was applied". On
    # every deployment that is not allowed — the normal case on managed
    # Postgres — the two disagree forever, and the gap was carried in prose
    # inside ``message`` where nothing could act on it. Stating it in SQL lets
    # the probe tell the two apart: expected here, or genuinely unapplied.
    #
    # Left None for a post-condition whose migration has no skip branch; those
    # are unconditional and any failure is real.
    expected_when: str | None = None


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
            "did not apply here — and the role this service connects as is the pgvector "
            "extension owner, or a member of it, so this was NOT the ownership skip 044 "
            "tolerates. It is latent rather than a live defect: it changes plan choice only "
            "where the planner would otherwise pick a sequential scan over the HNSW index, so "
            "check idx_scan on that index in pg_stat_user_indexes before treating it as "
            "urgent. Repair: ALTER FUNCTION cosine_distance(vector, vector) COST 100; "
            "re-running the migration will never fix an ownership failure."
        ),
        # 044 tolerates TWO failures, and this has to cover both or it claims
        # more than it knows. ``insufficient_privilege``: altering a function
        # requires owning it or membership in the owning role, which
        # ``pg_has_role`` answers, and which is true for a superuser, who could
        # also apply it. ``undefined_function``: no such function, so there was
        # nothing to alter — ``to_regprocedure`` returns NULL rather than
        # raising, the row is simply absent, and COALESCE turns that absence
        # into the same "could not have applied it" answer.
        #
        # Without the COALESCE the missing-function case returns no row, which
        # reads as "not expected" and fires a warning whose message asserts this
        # role owns the pgvector extension — a claim nothing established. That
        # is the exact fault this post-condition is being fixed for, one level
        # down, so it is worth the extra clause.
        expected_when=(
            "SELECT NOT COALESCE("
            "(SELECT pg_has_role(current_user, proowner, 'USAGE') FROM pg_proc "
            "WHERE oid = to_regprocedure('cosine_distance(vector, vector)'))"
            ", false)"
        ),
    ),
)
