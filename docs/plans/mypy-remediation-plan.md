# Mypy remediation — full, real fixes across core-api + core-storage-api

## Context

The commit gate (`.claude/hooks/pre-commit-check.sh`) blocks every `git commit`
until `ruff check`, `ruff format --check`, and `mypy` pass for **both**
`core-api` and `core-storage-api`. Ruff/format are already green (uncommitted,
waiting on this work). `mypy` currently reports 455 errors (414 core-api + 41
core-storage-api; some in shared `common/` files are double-counted across
both totals). This blocks committing anything, including unrelated finished
work (RE-01 bookkeeping, sprint WorkItems).

Leon's explicit instruction, in answer to a scoping question about risk vs.
speed: **"Fix all of it properly, in Plan Mode."** No `type: ignore`
suppressions, no loosened gates (CLAUDE.md Failure Mode #11 forbids that
without sign-off, which wasn't given) — real type/code fixes only, committed
per CLAUDE.md convention 11 as a dedicated pre-existing-repair effort, separate
from the sprint's feature commits.

All root causes below have been verified by reading the actual source (not
just mypy's error text), via two research passes this session.

## Fix categories

### A. `mcp_server.py` (245 errors — the bulk of core-api's total)

**A1 — mechanical (~233 errors, `return-value`).** Functions annotated `-> str`
return pre-baked `CallToolResult` objects from guard helpers (`_check_auth()`,
`_check_write_scope()`, etc.) — intentional pattern:
`if err := _check_auth(): return err`. Fix: widen ~40 function signatures from
`-> str` to `-> str | CallToolResult`. No behavior change.

**A2 — real fixes (~12 errors, `arg-type` + `typeddict-item`).** Sampled at
lines 837, 845, 850, 856, 857, 3767, 3768:
- `MemoryCreate(memory_type=...)` fed a raw `str | None` where `MemoryType | None`
  is expected → validate/convert to the enum before passing, or narrow with an
  explicit `if memory_type is not None: MemoryType(memory_type)`.
- `write_mode` fed `str | None` where a `Literal["fast","strong","auto","stm"] | None`
  is expected → validate against the literal set before use (reject/raise on
  unknown values — this is a real input-validation gap, not just a type gap).
- `len()` called on `list[dict] | None` → guard with `if x is not None` or
  default to `[]` at the point of assignment.
- Two `typeddict-item` cases where a TypedDict field typed `str` receives
  `str | None` → either widen the TypedDict field to `str | None` or ensure the
  value is non-None before construction, whichever matches actual call-site
  semantics (needs a quick look at the TypedDict definition to decide).

**After A1+A2:** run the `/tool-surface` skill checklist (CLAUDE.md Failure
Mode #5) — these are the highest-risk-of-baseline-breakage edits in the whole
remediation, since `mcp_server.py` touches tool descriptions/signatures.
Regenerate `plugin/tools.json` via `scripts/export_tool_specs.py` only if the
checklist says signatures changed in a way that affects the exported spec
(annotation widening alone should not, but verify).

### B. `storage_client.py` (10 errors) + `memory_service.py` (66 errors)

**B1 — `call-overload` (5 identical occurrences, lines 1118, 1410, 1426, 2003,
2352).** Root cause confirmed: `_post()` (line 339) is annotated
`-> dict | list`, but every call site treats the result as a dict. Fix: narrow
`_post()`'s return annotation to `-> dict` (its actual contract) — one change
fixes all 5 sites. If any call site *does* legitimately receive a list from
`_post()`, that site needs `cast(dict, result)` instead; check via a quick
grep of `_post(` call sites before committing to the blanket fix.

**B2 — remaining `arg-type`/`attr-defined`/`union-attr` errors** in
`memory_service.py` and the rest of `storage_client.py`: fix individually,
same discipline as A2 (narrow, validate, or default — no suppressions).
Includes the two `no-redef` errors at `memory_service.py:25,33`.

### C. `no-redef` pattern (4 occurrences, root-caused this session)

- **`entity_extraction_worker.py:341`** — `name_to_id` assigned once in an
  `if` branch (~line 154) and again in the `else` branch (~line 341); mypy
  sees competing definitions rather than branches. Fix: hoist a single
  annotated declaration (`name_to_id: dict[str, UUID]`) above the branch,
  assign values inside each branch only.
- **`memory_service.py:25,33`** and **`crystallizer_service.py:18`** —
  standard `try: from openai import OpenAIError / except ImportError:
  class OpenAIError: ...` fallback pattern; mypy treats the fallback class as
  redefining the import. Fix: define the fallback under a distinct internal
  name (`_OpenAIError`/`_GoogleAPIError`) and alias:
  `OpenAIError = _OpenAIError` — mypy no longer sees two definitions of the
  same name in conflicting branches. Apply the same pattern to both files.

### D. `operator` pattern (4 occurrences, root-caused this session)

- **`organization_settings.py:586`** — after an `isinstance` narrowing at
  line 577 against `expected_types` (which can include `str`/`bool` alongside
  `int`/`float`), the range check `lo <= v <= hi` runs even when `v` isn't
  numeric. Fix: add an explicit `if isinstance(v, (int, float)):` guard
  immediately before the range comparison.
- **`crystallizer_service.py:401-402`** — `result["memories_archived"] += ...`
  style increments on a dict whose values are untyped/`object`. Fix: use
  `result["memories_archived"] = result.get("memories_archived", 0) + len(archived_ids)`
  (and the equivalent for `new_memories`), or give `result` a proper
  `dict[str, int]`/TypedDict annotation at its point of construction so the
  values are typed `int` from the start (prefer this if `result` is
  constructed nearby and reused — check before choosing the local patch vs.
  the annotation fix).

### E. SQLAlchemy Core typing — `postgres_service.py` (26 errors, highest risk)

`ColumnElement[bool]` vs `BinaryExpression[bool]` mismatches and `FromClause`
vs `Table`/`insert()`/`delete()` mismatches. These are query-builder typing
gaps, not simple annotation issues — each needs individual review against the
actual SQLAlchemy construct in use (cast to the precise type SQLAlchemy
expects, e.g. `cast(Table, some_from_clause)`, or restructure the expression
so its inferred type matches). Work through them one at a time; do not
batch-cast to silence without confirming the underlying construct really is
that type. This is the one category where "real fix" and "safe fix" require
the most care — go slow, verify no behavioral change by running
`core-storage-api`'s existing test suite after each cluster of fixes.

### F. Missing third-party stubs (13 errors — `import-not-found`/`import-untyped`)

`google.genai`, `google.api_core`, `google.cloud.aiplatform`/`pubsub_v1`,
`pgvector.sqlalchemy`, `sentence_transformers` have no stubs available. This
is not a code-correctness issue — it's a stub-availability gap. Fix via a
scoped mypy config override (per-module `ignore_missing_imports = true` in
each service's mypy config, targeted at exactly these module names — not a
blanket `ignore_missing_imports` for the whole codebase, which would mask
future real import errors). This is the one place a config change substitutes
for a code change, and it's the correct tool for this specific problem (no
stubs exist upstream to install).

### G. `common/` shared files (fix once, resolves errors counted in both services)

`embedding/_service.py`, `embedding/providers/local.py`, `events/pubsub.py`,
`llm/providers/{vertex,gemini,openai}.py`, `llm/registry.py`,
`enrichment/service.py`, `models/{procedure,memory,entity,document}.py`. Fix
each individually per its sampled error code (mostly `arg-type`/`assignment`,
same narrow-or-validate discipline as B2). Re-run mypy for **both** services
after touching any `common/` file to confirm the fix isn't double-needed.

### H. Remaining smaller files (~25 files, core-api + 3 storage-api migrations)

`entity_service.py`, `crystallizer_service.py` (non-operator errors),
`contradiction_detector.py`, `providers/_registry.py`, `skill_promoter.py`,
`governance_remediation.py`, `pipeline/steps/search/classify_query.py`,
`evolve_service.py`, `routes/memories.py`, `insights_service.py`,
`forge/cron_handler.py`, `pipeline/steps/search/parallel_embed_entity_boost.py`,
`auth.py`, `stm_service.py`, `session_trace.py`, `ingest_service.py`,
`ingest_chunking.py`, `fleet.py`, `sqlite_backend.py`,
`parallel_embed_enrich.py`, `load_and_serialize.py`, `identity_token.py`,
`cache.py`, `app.py`, plus core-storage-api's 3 migration files
(`028_procedures.py`, `003_documents_embedding.py`, `001_initial_schema.py`).
1-2 errors each — fix individually as encountered, same narrow/validate/annotate
discipline, no batching risk here since each is isolated.

## Execution order (lowest-risk first, so gate keeps trending green)

1. **F** (stub config) — zero behavior risk, immediate error-count drop.
2. **A1** (mechanical annotation widening) — zero behavior risk.
3. **C, D** (no-redef, operator) — small, well-understood, isolated fixes.
4. **G** (common/ shared files) — moderate risk, re-verify both services after.
5. **B1** (`_post()` return-type narrowing) — check call sites first, then one
   change fixes 5 errors.
6. **A2, B2, H** — individual real fixes, file by file.
7. **E** (`postgres_service.py` SQLAlchemy typing) — last, highest risk, most
   careful, verify against core-storage-api's test suite after each cluster.

After each numbered step: re-run `uv run --project <svc> mypy <svc>/src/` for
the affected service(s), confirm the expected error-count drop, revert any
`uv.lock` side-effects (`git checkout -- core-api/uv.lock core-storage-api/uv.lock`).

## Verification

- After all steps: `uv run --project core-api mypy core-api/src/` → 0 errors.
  `uv run --project core-storage-api mypy core-storage-api/src/` → 0 errors.
- Re-run `ruff check` + `ruff format --check` for both services (guard against
  regressions introduced by the mypy fixes themselves).
- Run the `/tool-surface` skill checklist after step A (mcp_server.py changes)
  specifically — confirms `test_tool_descriptions_regression.py`,
  `test_mcp_token_budget.py`, `test_tools_export_in_sync.py` still pass and
  `plugin/tools.json` stays in sync.
- Run core-storage-api's test suite after step E specifically (SQLAlchemy
  query-builder changes are the one category with real behavior-change risk).
- Full suite green at the end: `TEST_DATABASE_URL=... python -m pytest tests/ -q`
  (per CLAUDE.md §3), count reported from pytest's own tail, not assumed.

## Commits (per CLAUDE.md convention 11 + commit gate discipline)

1. `fix(mypy): pre-existing ruff/format repair — core-api + core-storage-api`
   — the already-finished ruff/format fixes, committed once mypy is green
   enough to pass the gate (or once category F+A1 land, whichever first
   unblocks the gate — the gate needs *all* mypy errors gone, so realistically
   this commit lands after the full mypy remediation, labeled as pre-existing
   repair, DCO-signed).
2. One or more `fix(mypy): <category> — <short description>` commits per
   category above (F, A, C+D, G, B, E, H), each DCO-signed
   (`git commit -s`), each preceded by re-running the affected service's mypy
   + ruff/format to confirm no regression.
3. `chore(sprint-recall-efficiency): RE-01 run_state bookkeeping` for the
   untracked `run_state.json`, separate from the mypy work.

Then resume sprint WorkItems RE-02/RE-03/RE-05 per the sprint-run skill.
