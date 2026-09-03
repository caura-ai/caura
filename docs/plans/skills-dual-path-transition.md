# Skills dual-path transition

**Status:** in progress · **Scope:** the bundled OpenClaw plugin skill
(`plugin/skills/`) and the standalone Claude Code / Codex skill
(`static/skills/`) · **Decided by:** Eldad, via the programme coordinator,
2026-09-03.

## What this covers

Both skill populations are served under two slugs during this transition:
the historical slug (`memclaw`, already on disk on every existing <!-- legacy-name-ok: dual-path skills transition -->
install) and the new one (`caura`, shipped alongside it starting this
change). Neither directory's content differs except in the handful of
lines that self-reference their own install path — see the PR that
introduced this doc for the exact diff.

This is deliberately **not** a flag-day cutover. The two populations are
governed by unrelated mechanisms in different languages
(`PROTECTED_SKILLS` in `plugin/src/reconcile-skills.ts`, a Python dict
`_SKILL_LABELS` in `core-api/src/core_api/routes/plugin.py`), so landing
both correctly in one window and having each be independently verifiable
is safer than a single synchronized flip.

## Why the historical slug isn't dropped yet

`plugin/skills/memclaw/` is protected from deletion by <!-- legacy-name-ok: dual-path skills transition -->
`reconcile-skills.ts`'s `PROTECTED_SKILLS` set. That reconciler runs every
60 seconds and deletes any on-disk skill slug that is neither in the
server's dynamic catalog nor in `PROTECTED_SKILLS` — so removing the <!-- legacy-name-ok: dual-path skills transition -->
historical slug from that set deletes the directory, and whatever an
agent's already-generated `TOOLS.md`/`AGENTS.md` text still points at,
from every existing install within one heartbeat of the next deploy. That
is real user-facing breakage for any install that has not yet had a
chance to pick up the new slug (auto-upgrade is on by default but not
universal — some tenants opt out, and some installs are version-pinned;
see `core-api/src/core_api/routes/fleet.py`'s
`KNOWN_BROKEN_DEPLOY_VERSIONS` and `_auto_upgrade_enabled_for_tenant`).

The standalone `static/skills/memclaw/` has no equivalent deletion risk — <!-- legacy-name-ok: dual-path skills transition -->
nothing reconciles it — but has no update channel either: an existing
curl-install sits untouched until the operator re-runs the installer, so
there's equally no reason to remove server-side support for it while any
such install might still exist.

## Retirement condition

Drop the historical slug from `PROTECTED_SKILLS`, drop its key from
`_SKILL_LABELS` and `_plugin_root_files`, and delete both of its skill
directories (`plugin/skills/` and `static/skills/`) only after **both**:

1. At least one full plugin release has shipped with the new slug
   available alongside the old one, confirmed present and stable on disk
   (not just immediately after a deploy — the reconciler's 60-second
   delete window means "present now" proves nothing; "still present
   after the next heartbeat" does).
2. Eldad (or whoever owns the rebrand at the time) explicitly approves the
   cutover. This is not a fixed calendar date or release number, because
   the actual constraint is real-world adoption on installs this
   repository cannot measure (see the scoping document referenced below)
   — an arbitrary date risks retiring the compatibility path before
   installs that need it have upgraded.

This is intentionally a **named decision gate**, not an open-ended
"eventually": the condition is checkable (has a release shipped with both
present and stable; has the owner signed off), even though it isn't a
date on a calendar.

## Related

- The scoping analysis this transition was built from: shared with Eldad
  directly (coupling sites, ordering constraints, blast radius, what
  could and couldn't be established from this repo alone).
- `docs/plans/rebrand-alias-retirement-policy.md` — a separate,
  superseded, cross-repo policy for production machine-route aliases.
  Not used here: that document's table is shaped for host/route
  retirement across multiple repositories, not a single repo's
  bundled-skill directory slug, and updating it is out of this change's
  scope.
