# Canonical legacy-name ratchet

The 31 August 2026 audit produced the first fleet-wide census of the rename
ratchet. Counts below are the **gated** footprint at the pinned refs, so their
partitions are disjoint and may be summed. The five rows marked “first census”
had not previously been measured by the programme.

| Repository | Ref | Gated lines | Files | Census status |
|---|---:|---:|---:|---|
| `caura` | `be311955` | 315 | 82 | previously measured |
| `openclaw-fleet-tester` | `5a1aa063` | 260 | 27 | **first census** |
| `caura-ops` | `5c835a8d` | 458 | 100 | previously measured |
| `caura-onprem-installer` | `0913557f` | 396 | 36 | **first census** |
| `caura-daemon` | `42137548` | **2,103** | **328** | **first census** |
| `caura-onprem` | `6f42f7e1` | 478 | 39 | **first census** |
| `caura-test-automation` | `8b490bec` | 616 | 72 | **first census** |
| `caura-enterprise` | `a4742cce` | 1,060 | 173 | previously measured |
| **Fleet** | | **5,686** | **857** | |

The three previously measured repositories total 1,833 gated lines. The five
new measurements account for the remaining 3,853; notably, `caura-daemon`
alone is larger than `caura` and `caura-ops` combined.

“Present” is a different, per-repository diagnostic and must never be added
across repositories. Enterprise, for example, reported 1,060 gated lines and
also disclosed: “Excluded from the count above: 36 line(s) in 2 mirror(s),
gated where authored.” Its 1,096 present lines include copies whose authored
lines already belong to another repository's gated partition.

## Audit result

Eight copies existed as four byte variants:

- A: `caura` and `openclaw-fleet-tester`.
- B: `caura-ops` and `caura-onprem-installer`.
- C: `caura-daemon`, `caura-onprem`, and `caura-test-automation`.
- D: `caura-enterprise`.

B and C were genuinely different files, although their executable ASTs were
identical at the measured refs. A and D both had release-please changelog
handling; D's copy gained it in enterprise PR 1303. D alone had generated and
vendored mirror exclusions and the corresponding inventory disclosure.

All four variants produced the same 315-line, 82-file result when run against
the same `caura` tree. The audited figures were therefore comparable; the cost
was maintenance drift and repo-specific behavior hidden in copied code, not an
already-proven mismatch in the headline metric.

The direction was rechecked against the remote refs immediately before
implementation. The trees still form A, B, C, and D as listed above, and the
recent change-summary hardening in `caura` is absent from B and C. The canonical
engine therefore starts in `caura` and moves outward. Adopting B or C into A
would discard that hardening; adopting D wholesale would impose enterprise-only
mirror policy on every repository.

## Shape

`scripts/legacy_name_ratchet.py` is the canonical engine. Every repository gets
the same engine bytes and a local `scripts/legacy_name_ratchet.json`. The JSON
object is strict: missing or unknown keys are errors, and its complete surface
is exactly these five fields:

| Field | Type | Meaning |
|---|---|---|
| `default_base` | validated ref string | Gate comparison ref when `--base` is omitted. |
| `release_please_changelogs` | boolean | Exempt whole files whose basename starts with `CHANGELOG` (case-insensitive), only on an authenticated release-please pull request. |
| `mirror_paths` | list of root-relative paths | Generated mirrors gated in their authoring repository. |
| `mirror_manifest` | root-relative path or `null` | Existing vendored-file manifest whose keys are mirror paths. |
| `marker_inventory_meta_paths` | list of root-relative paths | Paths omitted only from marker analytics. |

No sixth field is part of this port. Engine-wide rules, including marker-system
code/test exclusions and marker semantics, remain in the engine.
A declared manifest must be a readable JSON object whose keys are valid
repository-relative paths. Missing or malformed manifests fail closed rather
than silently moving their mirrors into the fleet-summable headline.

The `caura` reference configuration uses `origin/main`, enables release-please,
has no mirrors or manifest, and declares
`docs/plans/rebrand-sunset-plan.md` as its marker-inventory meta path.
Enterprise will use `origin/dev`, its generated snapshot and vendored manifest,
and `docs/rebrand-sunset-plan.md`. The sunset documents remain fully ratcheted;
only analytics omit their self-referential markers.

`openclaw-fleet-tester` keeps `release_please_changelogs=true` during the
mechanical port even though the flag is currently dead there. Removing it is a
separate behavior change requiring separate approval.

The exemption authenticates the pull request from GitHub's event payload, not
from its branch name alone. The source branch must use release-please's prefix,
the immutable pull-request author id must be the Caura deploy bot
(`265395343`), and the head and base repository ids must match. The bot id and
repository ids are assigned by GitHub rather than supplied by a contributor, so
a fork author cannot opt into the exemption by naming a branch. Missing,
malformed, or non-`pull_request` event context receives no exemption. All three
repositories with the flag enabled run this gate on `pull_request`, not
`pull_request_target`.

Once authenticated, the exemption is file-wide: the engine skips every matched
line in each path whose basename starts with `CHANGELOG` (case-insensitive). It
does not identify individual generated lines. No path outside that basename
predicate is exempt.

## Deferred work is an attribute, not an exemption

`legacy-name-deferred: <reason> (<doc path or issue URL>)` records work that a
decision explicitly postpones. It is deliberately outside the permanent-marker
table and regular expression. The scan stores it as an attribute of a counted
line, so a newly written deferred line fails exactly as the same unmarked line
would. Adding the annotation to an already-counted line is text-neutral for the
ratchet; it cannot provide headroom or turn a red build green.

The reference is mandatory and is checked in the exact tree being scanned.
Working-tree document references must be regular repository files that are
already committed or fully staged; intent-to-add entries, symlinks, and
submodules do not qualify. HTTPS issue references are syntax-checked without
making gate availability depend on a remote service. Malformed syntax, a
missing document, or a local-only document fails the gate.

A line carrying both deferred and a permanent `legacy-name-ok` or
`legacy-name-floor` marker is a hard error. The permanent marker still wins the
classification, because `_kind()` remains the only exemption decision, but the
gate then fails the contradiction instead of silently publishing a deferred
line outside the headline. This differs from the measured alias-plus-floor
population: those are two compatible permanent claims, while permanent and
deferred are mutually exclusive lifecycle states. There is no deferred
population to preserve, so choosing a hard error now has no migration cost.

The only sanctioned count increase is a same-path `legacy-name-floor` to
`legacy-name-deferred` replacement. It must preserve the exact text before the
marker, including comment introducer and internal whitespace, and the exact
syntactic closer after it. As everywhere else in this engine, outer indentation
is normalized before text comparison, so reindentation alone is not treated as
a new branded line. A surviving floor or alias occurrence consumes the base
floor before transition matching, so one old floor cannot authorize both a
permanent successor and a deferred duplicate. Each net-new deferred occurrence
is allocated once: an old counted line in the same path claims it before the
count-increasing floor transition is considered. The exception is derived from
the base line; a line that had no base floor marker cannot use it.

## Report contract

A bare `--report` is current inventory only. It does not resolve a base and
cannot print a misleading all-zero split. `--report --base <ref>` adds the
explicitly requested change split. The gate still uses `default_base` when its
caller omits `--base`.

Every report says that the scope is tracked files only and gives the number of
untracked, non-ignored files omitted. This makes the existing tracked-only
boundary visible without changing what the gate refuses.

Human output leads with gated lines/files and labels them as the fleet-summable
headline. It also prints present lines/files as a per-repository diagnostic,
labels it “never sum,” and discloses any excluded mirror inventory.

`--report --json` makes the boundary structural:

- `headline.name` is `gated`; it contains numeric `lines` and `files` plus
  `aggregation: {"allowed": true, "operation": "sum"}`. Its additive
  `deferred` member partitions out valid deferred lines by file and decision
  reference without removing them from `headline.lines`.
- `diagnostics.present` contains an explicit denied aggregation contract and a
  display string. It deliberately has no numeric `lines` or `files` fields, so
  a generic caller cannot select a `present` number and silently sum it.
- Mirror details remain auditable under `diagnostics.mirrors`, also tagged with
  denied aggregation.
- `marker_inventory.counts` includes the deferred token separately, including a
  recognized marker whose syntax or reference is invalid.
- `deferred_validation` exposes whether every recognized deferred marker is
  valid and lists bounded path/line diagnostics when one is not.
- `scope` and an optional explicit-base change object carry the remaining
  bounded diagnostics. When present, `change.deferred` distinguishes
  count-neutral annotations from narrowly excused floor-to-deferred increases.
  These additions are backward-compatible fields in the existing
  `legacy-name-ratchet-report/v1` object; existing field meanings do not change.

## Gate invariants and tests

The canonicalization does not change what `caura` refuses. The text-based mint
decision, marker semantics, move reconciliation, removal/new-exemption reports,
and release changelog net accounting remain intact. Configuration selects only
the approved repo-specific base and exemptions; `caura`'s mirror set is empty.

The common suite exercises every configuration feature independent of the
local values. It includes A's release-changelog net-accounting regression and
D's seven generated-mirror fixtures: exclusion from the headline, regenerated
mirror pass, disclosure, empty-disclosure silence, gate-output silence, and
root/subdirectory consistency for both the exclusion and disclosure. A
manifest-backed mirror fixture covers the vendored half separately. Additional
fixtures pin strict five-field validation, inventory-only bare reporting,
tracked-only disclosure, JSON aggregation tags, default-base selection, and the
analytics-only sunset-document exclusion.

## Rollout

Land and observe `caura` first. Stop at its manual merge gate. Only after that
merge, port the identical engine and one local config to one repository at a
time, rechecking open and recently merged ratchet PRs at each port. A broken
gate must never be broadcast across all eight lanes in one change.
