# Gateway RPC dual-read alias: historical spellings → `caura.*`

**Status:** in progress · **Scope:** the six OpenClaw gateway commands
registered by the plugin (`plugin/src/index.ts`) · **Decided by:** Eldad,
via the programme coordinator, 2026-09-04.

## What this covers

`caura.status`, `caura.deploy`, `caura.deploy.status`, `caura.educate`,
`caura.allowlist.check`, and `caura.allowlist.fix` become the canonical
gateway command names. The historical `memclaw.*` spellings keep <!-- legacy-name-ok: dual-read alias, tracked for eventual retirement in this doc -->
dispatching to the exact same handlers — a dual-read alias, the same
shape as the `caura_*`/`memclaw_*` MCP tool rename, but registered as two <!-- legacy-name-ok: dual-read alias, tracked for eventual retirement in this doc -->
separate names rather than one shim, because `registerGatewayMethod`
takes an exact string per registration with no prefix-stripping layer to
hook into.

## Why this is `legacy-name-deferred`, not a permanent alias

Unlike the MCP tool-name shim (`core-api/src/core_api/mcp_server.py`,
declared permanent because saved prompts, keystone rules, and published
tutorials are known to quote the old tool names), nobody has established
that the same is true for these six gateway commands. What is established
is the opposite of "safe to drop today": a caller-side measurement (see
the scoping analysis referenced below) found these calls are dispatched by
OpenClaw's local gateway daemon over IPC on the operator's own machine —
they never reach Caura's network, so Caura's own telemetry has **no way**
to observe whether any external script or runbook still calls the old
names. That is unmeasurable risk, not measured zero risk. The alias is
cheap insurance against something this repository cannot rule out.

## Retirement condition

Drop the six `memclaw.*` gateway-method registrations no earlier than <!-- legacy-name-ok: dual-read alias, tracked for eventual retirement in this doc -->
**12 months after this change ships**, and only after Eldad (or whoever
owns the rebrand at the time) explicitly approves the cutover. Both
conditions, not either alone: the date alone doesn't establish that
nothing depends on the old names (nothing can, given the measurement gap
above), and approval without a minimum window risks retiring a
compatibility path within an operator's normal upgrade cadence.

If a way to measure real callers is found before then (e.g. a future
plugin version that self-reports which gateway command names are
actually invoked), that measurement should replace the calendar condition
as the deciding factor — this document does not need to be treated as
final if better evidence arrives.

## Related

- The scoping analysis and the caller measurement this decision was built
  from: shared with Eldad directly.
- `docs/plans/skills-dual-path-transition.md` — the sibling dual-path
  decision for the bundled/standalone skill directories, same programme,
  same 2026-09-04 decision point, different mechanism and different risk
  shape (that one has a real deletion hazard on a 60-second clock; this
  one has no clock at all, since nothing here can be deleted out from
  under a caller the way a skill directory can).
