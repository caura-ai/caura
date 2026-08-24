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

Neither gate looks at **values**. A setting correctly renamed to `CAURA_*` that still
*holds* an old-brand value scores as fully migrated forever. Renaming is not migrating.

To exempt a line the ratchet would otherwise fail, annotate it in that file's own
comment syntax with `legacy-name-ok:` followed by the reason. Exemptions are reported
on every run, so a PR that adds them is visible as such to a reviewer. Use them for
contract, not for convenience.

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

---

## Where the detail lives

This file holds the rules, which are stable. The phase-by-phase board — what has
landed, what is in flight, and which of it is running in parallel — is maintained
outside the repo and changes daily; ask the maintainers for the current handover
document rather than trusting a copy committed here.

Keep this file short. It earns its place by being correct, not by being complete.
