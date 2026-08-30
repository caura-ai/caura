# Rebrand sunset plan — the in-repo reference

**Status:** active · **Last verified against the repos:** 2026-08-24
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
  drain to zero, and only then are the old topics deleted. Never dual-publish.
- **The database** — a new role is created `IN ROLE` the old one so grants and
  ownership are inherited, DSNs flip one service revision at a time, and the rename
  itself is a single short window. The old role is kept.
- **Cloud Run** — the services are being decoupled from build-time configuration
  first, so that a rename is a deploy rather than a rebuild.
- **The broker's default cloud host** — the compile-time default in the daemon and
  the release mirror derived from it move together, and only once the gateway serves
  the new host. The mirror is the auto-updater's only source for a broker that never
  registered, so flipping before the new host serves `/…/latest.txt` and `/…/<tag>/`
  breaks update for exactly the installs with no other way to recover. Carried in the
  daemon as the repository's only `TODO(rebrand cutover)`.

If you are about to change one of these, the sequence matters more than the change.

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

---

## The two gates, and what each one misses

Both are required checks. Neither is sufficient alone, and the gap between them is
where the real risk lives.

**`legacy_name_ratchet.py`** fails a PR when a file's old-brand count goes **up**. It
stops the rename going backwards. It is *directional*, so a change that **deletes** an
old-brand string always passes — including when that string was load-bearing.

**`do_not_touch_sentinel.py`** asserts that specific strings still **exist**. It is the
only thing standing between a well-meaning sweep and a silently broken dependant.

### The release-please branch exemption is repo-specific

`_release_please_branch()` belongs only in ratchets that can run on a release-please
branch. Verified 2026-08-30 across the nine-repository estate — eight live repositories
plus archived `loadtest` — on all three signals: workflow, configuration, and current or
historical `release-please--*` PR branches. Only `caura` and `caura-enterprise` have any
of them. The other seven have none, so porting the helper there would be dead code. If
release automation is added to another repository, recheck all three signals before
porting the exception.

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
nightly caller omits them. Those fallbacks preserve the current names until the later
variable flip. Reconsider code extraction only if a larger piece of behaviour, rather
than another small contract, starts changing in lockstep.

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

This file holds the rules, which are stable. The phase-by-phase board — what has
landed, what is in flight, and which of it is running in parallel — is maintained
outside the repo and changes daily; ask the maintainers for the current handover
document rather than trusting a copy committed here.

Keep this file short. It earns its place by being correct, not by being complete.
