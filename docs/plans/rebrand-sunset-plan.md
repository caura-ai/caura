# Rebrand sunset plan — the in-repo reference

**Status:** active · **Last verified against the repos and live project:** 2026-09-09
**Enforcement:** `scripts/legacy_name_ratchet.py` and `scripts/do_not_touch_sentinel.py`, both required checks

The product was renamed to Caura. This file is the in-repo answer to "is this old-brand
string a bug, and may I change it?" — because for a large and load-bearing subset of
them the answer is **no**, and nothing in the code says so at the point of use.

If you grepped this repo for `memclaw` and landed here, that grep worked as intended. <!-- legacy-name-ok: this doc must be findable by the exact grep it exists to answer -->
Read [The floor](#the-floor) before you change anything you found.

---

## The seven rules

These are the durable part of the plan. Every gate in CI traces to one of them.

| # | Rule | What it means in practice |
| --- | --- | --- |
| 1 | **Provision before flip** | A publish to a topic that does not exist is *silent loss*, not an error. And `apply` is manual: a merged Terraform PR is not a provisioned resource. |
| 2 | **Never edit immutable history** | Migrations, cut releases, hash-chained audit rows, dated posts. Point at them; do not rewrite them. |
| 3 | **Old names stay readable forever** | Alias, dual-read or redirect — never replacement. An old name that stops resolving is a broken customer, not a completed rename. |
| 4 | **The do-not-touch list becomes CI** | Both halves now exist: the ratchet stops names being *minted*, the sentinel stops floor strings being *deleted*. |
| 5 | **Smoke gates before prose** | Keep monitor labels and dual-pushed image names working through every transition, or a healthy deploy starts failing on a stale assertion. |
| 6 | **One coupled cluster at a time** | `{core-api, core-worker, platform-admin-api}` move together; vendored files move together; all DSN consumers move together. |
| 7 | **Mint nothing under the old name** | A new repo, topic, package or service under the legacy brand kills a redirect that already works. This is the one the ratchet enforces automatically. |

### The writer side of rule 3

Rule 3 is stated read-side, and the corollary is not: **the moment you make a
consumer dual-read, every writer that pins the old spelling into something that
consumer later reads becomes a hazard** — a child process environment, a
generated config, an installed service unit. The consumer now *prefers* the new
name, so an ambient new-name value from anywhere else outranks the old-name
value the writer deliberately set. This has already shipped once, as a daemon
and the CLI that started it silently on different state directories.

- **Pin both spellings.** Find every writer that feeds a dual-reading consumer
  and have it set both, in the same change that makes the consumer dual-read.
  Whether that works is a language question: Go's `os/exec` keeps the **last**
  duplicate key, so appending overrides. Verify your language's rule instead of
  assuming it.
- **Precedence is first NON-EMPTY, never first-defined.** Every consumer in
  this fleet reads `""` as "use the default", so first-defined would relocate a
  state directory or drop a cloud origin with nothing red anywhere.
- **The smell:** clearing the *new*-name variable in a test for hermeticity
  during an alias wave. Production has no `t.Setenv`. If a test needs that
  line, check whether the writer it stands in for is still pinning one
  spelling — that clear will mask the live gap for as long as it is there.

---

## What is *not* frozen

An earlier and now-withdrawn version of this plan said Pub/Sub topics, the database and
the Cloud Run services were permanently frozen. **That is false and acting on it will
put you in conflict with work that is in flight right now.** All three are being
migrated, each by a sequence that is deliberate rather than optional:

- **Pub/Sub** — expand → migrate → contract. Topics cannot be renamed, so the new
  family is created alongside, publishers move one family at a time, subscriptions
  drain to zero, and only then are the old topics deleted. Never dual-publish. See
  [Current state and what remains](#current-state-and-what-remains).
- **The database** — a new role is created `IN ROLE` the old one so grants and
  ownership are inherited, DSNs flip one service revision at a time, and the rename
  itself is a single short window. The old role is kept.
- **Cloud Run** — the services are decoupled from build-time configuration so that a
  rename is a deploy rather than a rebuild. The current deployment state is recorded
  [below](#service-prefix-status).
- **The broker's default cloud host** — the compile-time default in the daemon and
  the release mirror derived from it move together, and only once the gateway serves
  the new host. The mirror is the auto-updater's only source for a broker that never
  registered, so flipping before the new host serves `/…/latest.txt` and `/…/<tag>/`
  breaks update for exactly the installs with no other way to recover. Carried in the
  daemon as the repository's only `TODO(rebrand cutover)`.

If you are about to change one of these, the sequence matters more than the change.

---

## Current state and what remains

This is an operative plan, so it records a dated decision surface rather than carrying
an expired inventory forward as though it were live. Re-measure before acting after the
verification date at the top changes.

### Pub/Sub programme status

| family | publisher flip | enum contraction | current state |
| --- | --- | --- | --- |
| `fleet` | 2026-08-25 | 2026-08-31 | complete; no legacy-prefixed topic remains |
| `security` | 2026-08-26 | 2026-08-31 | complete; no legacy-prefixed topic or durable subscription remains |
| `lifecycle` | 2026-08-28 | 2026-09-01 | complete; the former 36-subscription inventory no longer exists |
| `memory` | 2026-09-01 | 2026-09-05 | complete; no legacy-prefixed topic or durable subscription remains |
| `org` | 2026-09-08 | 2026-09-08 | complete; three production topics and two durable subscriptions await the authorized manual Terraform apply |
| `audit` | pending | pending | **last, unconditionally**; live legacy-prefixed work and DLQ resources remain in both environments |
| `pipeline` | not applicable | 2026-09-09 | never provisioned; its enum moved directly to current names rather than performing a fictitious flip |

Read-only inventory on 2026-09-09 returned current-prefixed topics and subscriptions as
the positive control. The legacy-prefixed remainder is finite: seven topics (four
`audit`, three production `org`) and six durable subscriptions (four `audit`, two
production `org`). No legacy-prefixed Pub/Sub resource remains for `fleet`, `security`,
`lifecycle`, or `memory`.

Contraction and resource retirement are deliberately separate. A completed contraction
stops consumers binding the legacy name; it does not authorize deletion. Directly
gathered evidence has already classified the retained production `org` resources ready
and Terraform declares them for deletion; the authorized manual apply remains. `audit`
still requires its own last-family flip and contraction before any retirement plan can
exist.

The previous 31 August per-subscription measurement remains in repository history as
evidence of the pre-contraction state. Its 36 named lifecycle subscriptions have since
been removed, so that snapshot is history rather than an operative runbook.

### Service-prefix status

The enterprise deployment-control variables have selected the current service prefix in
staging since 2026-09-02 and in production since 2026-09-08. The `caura-ops` and
`caura-test-automation` consumer variables selected the staging prefix on the same date
and the production prefix on 2026-09-09. The live service inventory contains the current
stack in both environments and no legacy staging services. Fourteen legacy production
services remain as a separately governed retirement surface; their presence does not
make the prefix flip incomplete.

For the two estate tracks remeasured here, the remaining work is explicit:

1. Flip and contract `audit` last, with its hash-chain ordering guarantees intact.
2. Apply the already-evidenced production `org` retirement through the authorized
   manual Terraform gate.
3. Retire the retained production service stack through its own runbook and gates.
4. Remove flip scaffolding only after no legacy topic or subscription remains.

The database and broker-host tracks retain their own sequences above. Their day-to-day
state remains in the maintainers' handover and was not remeasured for this snapshot.

---

## The floor

A large number of old-brand strings are **contract, not debt**, and will still be here
when the rename is otherwise complete. They fall into a few kinds:

- **User-side identifiers** — plugin ids, config keys, on-disk paths and skill slugs
  that installed clients already wrote to customer machines.
- **Compat shims** — aliases, dual-read tables, import shims and mounted legacy routes
  that exist precisely to honour rule 3.
- **Immutable history** — migration filenames, released package names and tags.
- **Machine surfaces** — hostnames, redirect maps and release mirrors that external
  systems resolve by literal string.
- **Cross-repo dependants** — log prose that a production monitor matches on, and
  wire-contract strings another service parses.

**This file is deliberately not the authoritative list.** The authoritative list is
`scripts/do_not_touch_sentinel.py`, because a list in prose rots and a list in CI does
not. Run it to see what is protected and why:

```
python3 scripts/do_not_touch_sentinel.py --list
```

### Expensive to move is not floor

Environment Terraform is the standing counter-example. Old-brand lines in the two
deployment environment files may name live or deliberately retained resources, but
that does not make them floor. Neither file carries a floor marker today, and that is
correct rather than an oversight.

The [current status](#pubsub-programme-status) separates the final family still to move
from retirement work after a completed move. Both kinds of Terraform entry remain
reachable by this programme.

Marking such a line stops it being counted and starts it being **trusted**, which is
the more expensive mistake: a floor marker claims that nobody will ever need to look
again.

So the test is not what a rename would cost. **It is whether a rename will ever happen.**
Infrastructure is deferred because recreating a live resource is expensive; *deferred*
and *never* are different words, and only the second one is floor.

---

## The two gates, and what each one misses

Both are required checks. Neither is sufficient alone, and the gap between them is
where the real risk lives.

### Historical ratchet audit and canonical engine

The 31 August 2026 audit found `scripts/legacy_name_ratchet.py` copied across
eight repositories in four variants. That finding is the historical input to
Package AB, not the intended steady state.

Package AB supersedes the copied-engine design: every repository receives the
same engine bytes and selects its approved local behavior through an exact
five-field JSON configuration. The rollout starts in `caura` and proceeds one
repository at a time, with a manual merge gate after each repository. The
[canonical-engine plan](legacy-name-ratchet-engine.md) records the pinned
census, the four audited variants, the aggregation contract, and the rollout
order.

Only the gated headline may be summed across repositories. The present-tree
diagnostic includes generated or vendored mirrors and is intentionally
non-additive.

**`legacy_name_ratchet.py`** fails a PR when a file's old-brand count goes **up**. It
stops the rename going backwards. It is *directional*, so a change that **deletes** an
old-brand string always passes — including when that string was load-bearing.

**`do_not_touch_sentinel.py`** asserts that specific strings still **exist**. It is the
only thing standing between a well-meaning sweep and a silently broken dependant.

### A vendored file can be counted in neither repository's total

`caura-enterprise` vendors part of `common/` from this repository, and
`scripts/vendored_files_manifest.json` gives every vendored path a policy:

- **`identical`** — must match the source byte for byte; the drift check fails on any diff.
- **`manual`** — carries intentional enterprise modifications; a diff is reported as
  **`INFO`** and **nothing fails**.

`common/events/topics.py` is `manual`. Three consequences, each reasonable alone and a
blind spot together:

1. The ratchet **excludes** vendored paths from the enterprise count, and discloses it:
   *"Excluded from the count above: 36 line(s) in 2 mirror(s), gated where authored."*
2. The drift check **will not fail** when the two copies disagree.
3. **Nothing syncs them** — the drift check's own docstring says so: *"the copies do not
   auto-sync."*

So a retired name can sit in the enterprise copy indefinitely: not counted there, not
enforced against the source, not propagated from it. **"Gated where authored" holds only
while somebody authors the same change in both places.**

Not hypothetical. `common/structlog_config.py` fell behind this repository's
`_add_logrecord_extras` processor and the platform services silently dropped structured
log fields — found 2026-06-11, months late. That file carries `identical` policy today
*because* of the incident.

**When you change a vendored file here, open the enterprise pull request in the same
sitting.** A `manual` policy makes the mirror your responsibility, not the gate's.

### The release-please pull-request exemption remains repo-specific

The canonical engine contains the release-please handling; the
`release_please_changelogs` configuration field decides whether it is active in
each repository. The 2026-08-30 workflow, configuration, and branch-history
audit found live release automation only in `caura` and `caura-enterprise`.
`openclaw-fleet-tester` nevertheless retains `true` through the mechanical port
as an explicitly documented known-dead flag; removing it is a separate behavior
change. Recheck those three signals before changing any repository's value.

A matching branch name is selection, not authentication: a contributor chooses
the source branch of a pull request. The exemption additionally requires the
GitHub event payload to identify the immutable Caura deploy-bot author id and a
head repository identical to the base repository. Missing or malformed event
context fails closed, so local runs and other trigger types retain full
coverage.

### The lifecycle probes share a contract, not an implementation

Do not extract the two `lifecycle_smoke.py` files into one installable package. Verified
2026-08-31 against `caura-enterprise/dev` at `efcb8b81` and `caura-test-automation/main`
at `a73c13f`: the enterprise deploy and rollback gate is 1,839 lines, while the nightly
fail-closed canary is 788. With test automation as the old input and enterprise as the
new input, `diff -u` contains 2,207 changed content lines — 578 removed and 1,629 added,
excluding the two headers.

The spec's 2,210 does not reproduce, for two reasons, neither of them algorithmic. The
larger is that it measured different files: it predates the service-target repair, which
changed both inputs. The smaller is that `diff -u | grep -c '^[+-]'` over-counts by
exactly two, because `---` and `+++` are themselves lines beginning with `-` and `+`;
that command reports 2,209 here against the 2,207 real changed lines.

Treat the total as evidence of scale rather than a stable architecture metric — and note
that it is unstable in the ordinary course of work, not merely in principle. Both files
are under active development, and re-running this measurement hours apart across a single
merge into `dev` moved it. Pin the commits, as above, or the number will not mean what it
says by the time it is read. The architectural figures are the durable ones: the files
have 41 and 19 top-level function definitions respectively, with only three names in
common — `_point_value`, `_resolve_core_api_url`, and `main` — and that shape has held
across every measurement of it.

That divergence is functional. Enterprise warms and probes a deployment, covers seven
lifecycle actions, integrates with promote and rollback break-glass handling, and owns
the broader rolling health gate. Test automation runs one audit-ID-correlated nightly
canary and turns its evidence into the repository's findings and gate verdict. The
three-repository repair (`caura` publisher/API, enterprise deploy gate, and test-
automation canary) was a coordinated change to a shared product contract; it was not a
mechanical edit to two interchangeable copies. A common package would add version and
rollout coupling without removing that coordination.

The counter-evidence is real: both files retained the same hardcoded core-api service
map through that repair, so one shared defect needed two fixes. Keep that narrow seam
shared as configuration and contract tests instead. Each probe now composes
`<environment service prefix>-core-api` from its repository's established input shape:
enterprise workflows provide `SP` or `PP`, while the test-automation probe accepts
`STAGING_SERVICE_PREFIX` and `PROD_SERVICE_PREFIX` and currently falls back when its
nightly caller omits them. The fallbacks are now compatibility safeguards, not the
rollout state recorded in [Service-prefix status](#service-prefix-status). Reconsider
code extraction only if a larger piece of behaviour, rather than another small
contract, starts changing in lockstep.

Neither gate looks at **values**. A setting correctly renamed to `CAURA_*` that still
*holds* an old-brand value scores as fully migrated forever. Renaming is not migrating.

To exempt a line the ratchet would otherwise fail, annotate it in that file's own
comment syntax with `legacy-name-ok:` followed by the reason. Exemptions are reported
on every run, so a PR that adds them is visible as such to a reviewer. Use them for
contract, not for convenience.

### A mention gets reworded, a contract gets the marker

That last sentence needs a test, because every line the ratchet stops looks like a
contract to whoever is writing it.

Ask what breaks if the old spelling is not on that line. If the answer is nothing — it
is prose that happens to name the thing — **reword it off the literal and take no
exemption.** If a reader has to type the string, or find it on disk, or a machine has to
match it, that is a contract: **mark it, and say which.**

"Which" is a marker, not a convention in the reason text. `legacy-name-ok` is a contract
that **bears** the old name — the alias, the redirect, the pinned wire format rule 3
recognises. `legacy-name-floor` is one that only **names** something the rename will
never reach. Both exempt the line identically; they are counted apart so that a sweep
adding ten mentions cannot bury the one alias among them.

Marking a mention is not a harmless extra. Every exemption widens the surface the gate
no longer watches, and it spends the one annotation whose whole value is that its reasons
are true.

Which way a given sweep falls is not predictable, so measure rather than assume. Prose
about code is mostly mention. Operator-facing documentation is mostly contract, because
its literals are the things a reader types or goes looking for — in one docs sweep, ten
of eleven exemptions survived this test.

Two practical consequences, both cheaper to know early: re-wrapping a line that carries
the old spelling mints new text even when the file's count *falls*, and a marker cannot
be taken off a line without taking the literal off with it. Reword first; annotate only
what is left.

---

## Before you change an old-brand string

1. **Is it on the sentinel list?** If yes, stop — the dependant has to move first, in
   its own repo, and the list changes in the same PR as the code, never after it.
2. **Is it a name or a value?** Values are invisible to both gates; check what actually
   reads it before assuming a rename is safe.
3. **Does anything outside this repo resolve it as a literal?** Monitors, redirects,
   installed clients and air-gapped image tarballs all do.
4. **Would deleting it lower a count while breaking a behaviour?** That combination
   passes CI green. It is the failure mode this plan is built around.
5. **Are you making a consumer read both spellings?** Then find that consumer's
   writers in the same PR and pin both — see
   [The writer side of rule 3](#the-writer-side-of-rule-3). Neither gate can see
   this one: the writer already carries the old name, so nothing is minted and
   nothing is deleted.

---

## Where the detail lives

This file holds stable rules and the dated, remeasured snapshot at the top. Refresh that
snapshot whenever one of its listed states advances. The phase-by-phase board — what is
running in parallel and who owns it — is maintained outside the repo and changes daily;
ask the maintainers for the current handover document rather than trusting a copy
committed here.

Keep this file short. It earns its place by being correct, not by being complete.
