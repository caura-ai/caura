# Rebrand alias retirement policy

**SUPERSEDED 2026-09-02 by Eldad's direction — legacy names are deleted, not retained;
"permanent by choice" no longer applies to any row below. Each owning lane updates its
own rows as it reaches them; see `codex-packages/PLAN.md`.**

**Status:** proposed 2026-09-01 · **Scope:** policy only · **Owner:** Caura release owner

This document decides when a retired identity may stop resolving. It changes no DNS,
redirect, route, package, repository, publication workflow, or infrastructure resource.

The decision is deliberately asymmetric:

- Cheap discovery aliases, installed-machine identifiers, package forwarders, and
  immutable historical objects are **permanent by choice**.
- A live machine endpoint may retire only after its consumers are measurable, every
  affected organization has had notice, and valid external use reaches zero for a
  complete evidence window.
- An alias with no instrument or owner is not eligible for retirement. It remains active
  because its condition cannot be proved, not because silence is evidence.

The accepted command/package decisions are inputs, not questions. See
[AI's command-rename record](https://github.com/caura-ai/caura-daemon/blob/main/docs/rebrand-naming.md)
and [caura-daemon#159](https://github.com/caura-ai/caura-daemon/pull/159). The current
routing shape is pinned, but its usage is not measured, by
[AL's retired-host probe](https://github.com/caura-ai/caura-test-automation/blob/main/tests/probes/retired_host_contract_probe.py)
and [caura-test-automation#297](https://github.com/caura-ai/caura-test-automation/pull/297).

## The rule

An alias has exactly one of three policies:

1. **Permanent by choice.** Its compatibility value exceeds its ongoing cost, or its
   installed base cannot be measured without disproportionate work. It has no retirement
   clock.
2. **Measured sunset.** Its condition, instrument, notice, decision role, and rollback are
   all named below. The clock starts only after the instrument and the new default are live.
3. **Not an alias.** Historical records that are not resolution targets and active
   infrastructure migrations remain governed by the [sunset plan](rebrand-sunset-plan.md),
   not by an alias clock.

No date by itself is a retirement condition. No absence in a 30-day log is evidence for a
90-day condition. A probe that proves an endpoint still works is availability evidence, not
consumer evidence.

## Inventory

### Already decided permanent

These decisions are accepted inputs from AI or existing permanent-alias contracts in
`caura`. This policy does not reopen them.

| Class | Retired identity kept readable | Policy and reason |
| --- | --- | --- |
| Daemon executable | `memclaw` | Permanent executable alias. Offline scripts and managed services are not fully enumerable. | <!-- legacy-name-ok: names AI's permanent daemon-command alias -->
| Python daemon project | PyPI `memclawd` | Keep the final forwarder published; never yank or delete it. | <!-- legacy-name-ok: names AI's permanent PyPI forwarder -->
| npm daemon project | `@caura-ai/memclawd` | Keep the final forwarder published; never unpublish it. | <!-- legacy-name-ok: names AI's permanent npm forwarder -->
| Homebrew | old `memclaw` token's rename record | Keep the rename record permanently after migration. | <!-- legacy-name-ok: names AI's permanent Homebrew compatibility token -->
| Scoop | old `memclaw` manifest | Keep it updated from the canonical release description; Scoop has no proven installed-app rename. | <!-- legacy-name-ok: names AI's permanent Scoop compatibility manifest -->
| Installer entry point | `https://memclaw.dev/install.sh` | Resolve permanently, serving a self-migrating installer when the new channel exists. | <!-- legacy-name-ok: names AI's permanent installer URL -->
| Python client | ~~The `MemClaw*` class/error names inside `caura_client`~~ | Retired 2026-09 — the same treatment already given to PyPI `memclaw-client` and the `memclaw_client` import package; no transition owed to pre-rename installs. | <!-- legacy-name-ok: records the retirement of the Python client class-alias surface -->
| Python client PyPI keyword | ~~`"memclaw"` in `caura-client`'s `keywords` list~~ | Retired 2026-09 — deliberately NOT in the "cheap discovery alias, permanent by choice" bucket above. A keyword is pure registry search-indexing metadata, re-published fresh on every release; unlike a URL redirect or a Homebrew/Scoop compatibility token, nothing on any customer's disk or in any existing install depends on it, so its removal has no installed-state consequence to protect against. No sentinel entry needed for the same reason. | <!-- legacy-name-ok: records the retirement of the PyPI search keyword -->
| npm client | ~~`@caura/memclaw-client`~~ | Retired 2026-09; the forwarding alias was deleted, not kept. | <!-- legacy-name-ok: records the retirement of a formerly-permanent npm client alias -->
| Interviewer | ~~`memclaw-interviewer` console-script entry point~~ | Retired 2026-09; nothing in Caura's own infrastructure invoked it (checked: no cron, Cloud Scheduler job, or CI workflow across caura-ops, caura-enterprise, caura-onprem, caura-onprem-installer, caura-daemon, caura-test-automation references it). | <!-- legacy-name-ok: records the retirement of the interviewer console-script alias -->
| Interviewer (transitional) | `resolve_cmd()`'s PATH fallback, config path, lock name, and cron marker in `caura_client.interviewer` | Kept for exactly one caura-client release past the console-script removal above, so `caura-interviewer install`/`uninstall` can still find and replace a pre-rename customer's existing crontab line instead of writing a duplicate beside it. Removed in the release after this one — see the `legacy-name-deferred` annotations on each line. | <!-- legacy-name-deferred: one-release upgrade path for pre-rename customer crontabs (docs/plans/rebrand-alias-retirement-policy.md) -->
| MCP | `memclaw_*` tool calls and the `mcpServers.memclaw` server/config key | Permanent dispatch/config aliases. Saved prompts and deployed agent configuration cannot be counted reliably. | <!-- legacy-name-ok: names permanent MCP tool and server aliases -->
| Plugin and REST | plugin id/path/skill slug `memclaw`, `/api/v1/memclaw`, and legacy managed-file markers | Permanent installed-state and route aliases protected by the do-not-touch sentinel. | <!-- legacy-name-ok: names permanent plugin, skill, and REST aliases -->
| Environment/config | `MEMCLAW_*` variables and the stored `memclaw.auto_upgrade_enabled` namespace | Permanent dual-read aliases; new spelling wins only when non-empty. | <!-- legacy-name-ok: names permanent environment and stored-setting aliases -->
| Issued identities | `mc_` API-key prefix, legacy agent ids, state directories, service labels, and support-bundle schema values | Permanent protocol or persisted-data identities. They are read compatibility, not product prose. |

The detailed code-level inventory remains executable in the
[do-not-touch sentinel](../../scripts/do_not_touch_sentinel.py). That list protects exact
strings; this document owns their policy class.

### Permanent by this policy

| Class | Identities | Decision |
| --- | --- | --- |
| Retired site roots | `memclaw.net/` → `caura.ai/`; `memclaw.dev/` → `caura.dev/`; `www.memclaw.net` → apex | Keep the 301s permanently. They are cheap, externally cached, and valuable to bookmarks, citations, and search indexes. | <!-- legacy-name-ok: names the retired root aliases this policy keeps permanently -->
| Old content paths | The gateway's retired blog, use-case, and `/for-agents` paths | Keep their 301s permanently. The source already records that indexed and externally cited paths redirect indefinitely. |
| Production installer entry point | `https://memclaw.net/install.sh` | Give it the same permanent policy as AI's staging installer URL; the published distribution E2E already consumes it. | <!-- legacy-name-ok: names the second live installer URL found outside AI's explicit row -->
| Historical release objects | `memclaw_<version>_...` archive filenames and `/memclaw/<tag>/...` objects | Retain permanently. Stop advancing the old channel only under S2 below; never remove historical objects or checksums. | <!-- legacy-name-ok: distinguishes permanent historical mirror objects from the sunsettable latest channel -->
| On-prem command | `memclawctl` | Resolve AI's permitted permanent outcome in favor of compatibility: keep both console entries on one implementation. Offline and air-gapped automation makes a zero-use claim uneconomic. | <!-- legacy-name-ok: resolves AI's may-be-permanent on-prem command in favor of compatibility -->
| Installed on-prem state | Existing container, volume, network, service, database, local-host, log-path, and license-path identities | Preserve the old read/lookup side permanently once a canonical spelling is added. Installations, air-gap bundles, certificates, and upgrade scripts are not centrally enumerable. |
| Published container references | OSS `ghcr.io/caura-ai/caura-memclaw-*` and on-prem `ghcr.io/caura-ai/memclaw-*` repositories, tags, and digests | Keep the existing references readable and, if canonical repositories are introduced, publish both names from one release description permanently. Anonymous pulls, offline Compose files, and air-gap bundles cannot support an organization-aware zero-use claim. | <!-- legacy-name-ok: names published image repositories that become permanent aliases when successors exist -->
| Reserved repository names | `memclaw-testing`, `memclawdash`, `memclaw-tutorials`, `memclaw-bus`, `memclaw-onprem`, and `memclawd` | Keep the six empty anti-hijack repositories permanently. Their descriptions reserve the retired names for live successors. | <!-- legacy-name-floor: names repository namespaces intentionally retained as permanent reservations -->
| Archived repository names | `caura-memclaw-OLD`, `memclaw-plugin-OLD`, and `memclaw-LME-bench` | Keep archived and addressable. Historical links and namespace reservation cost less than deletion or rename. | <!-- legacy-name-floor: names archived repository identities retained as history -->
| Repository rename redirects | `caura-ai/caura-memclaw` → `caura-ai/caura` and `caura-ai/caura-memclaw-enterprise` → `caura-ai/caura-enterprise` | Preserve the GitHub redirects permanently by never reusing either retired repository name. Existing links and image provenance labels resolve through them today. | <!-- legacy-name-ok: names two live GitHub repository redirects that namespace reuse would destroy -->

Permanent does not mean unowned. The platform reliability owner keeps the root and installer
availability probes green; release engineering keeps forwarders, image references, and historical
objects resolvable; repository administration keeps reserved, archived, and redirected names from
being deleted or reused.

### Measured sunsets

Here, **valid external use** means a request whose credential, session, or bootstrap token is
successfully associated with a non-Caura organization, regardless of the later application status.
Scans, invalid credentials, and AL's availability probe do not count as consumers.

| ID | Alias class | Retirement condition | Existing instrument and gap | Notice | Rollback | Decision role |
| --- | --- | --- | --- | --- | --- | --- |
| S1 | Production machine routes on `memclaw.net`: `/mcp*`, all `/api*` routes, `/join/*`, and authenticated application/session/OAuth compatibility | After all Caura-owned callers use the canonical host, **zero successful authenticated requests from an external organization for 90 consecutive days**. Any valid external request resets the clock. Static assets retire with their authenticated application surface and do not create a separate bot-traffic gate. | Cloud Run request logs preserve host, path, status, and user agent, but only for 30 days and without organization/credential identity. AL proves shape only. **Nothing currently proves this condition.** Build M1 below. | Email every organization observed after M1 starts, show an admin-dashboard banner, and publish release notes. Retirement is no earlier than 180 days after the latest first notice to an affected organization. | Serve a descriptive 410 for 30 days while retaining the mapping and previous gateway revision. Restore direct routing and reset the clock on a verified missed consumer. Never 301 an authenticated route. | Caura release owner, after written attestations from platform reliability and customer support. | <!-- legacy-name-ok: names the production retired host whose machine surface has a measured sunset -->
| S2 | Active publication through `/memclaw/latest.txt` and new tags under the old mirror prefix | The old latest pointer resolves to a bridge that can migrate itself; the old-channel E2E reaches the current successor through **two consecutive successor releases**; every historical object remains; and release engineering records the freeze version. | `dist-install-e2e.yml` exercises the old channel today, but no released bridge or two-successor proof exists yet. The condition is currently untestable because the migration releases do not exist. | Deprecation text in the bridge, release notes for two successor releases, and at least 90 days before freezing the pointer. | Resume advancing the old pointer from the same canonical release data. Never delete old objects or reuse a tag. | Release engineering owner; registry publication remains with the L/L9 publisher lane. | <!-- legacy-name-ok: names the old release-channel pointer that may freeze after a self-migrating bridge -->
| S3 | Staging machine routes on `memclaw.dev`: `/api*`, `/mcp*`, `/join/*`, and authenticated application/session/OAuth compatibility | For an external-capable staging route, use S1's 90-day zero-valid-external-use condition. For a route proven internal-only, require all callers moved, all legacy-origin bootstrap credentials expired/consumed/revoked, and zero valid use for 30 consecutive days. Static assets retire with their authenticated application surface. The permanent installer and historical mirror objects, and S2's active pointer, are excluded. | Staging Cloud Run logs have the same 30-day/no-org gap. The join URL embeds a credential in its path and the issuing row does not establish the URL origin, so raw URL logging must not be used as the cohort ledger. **Nothing currently proves the full condition.** | External organizations: S1's rolling 180-day direct-notice rule. Proven internal-only callers: one change window plus 30 days. | Keep the old mapping and gateway revision through a 30-day 410 period; restore direct routing if a missed pilot or automation appears. | Caura release owner, with platform reliability and the staging service owner. | <!-- legacy-name-ok: names the retired staging host and its direct machine surfaces -->
| S4 | Legacy sandbox mappings `sandbox-1.memclaw.dev` through `sandbox-10.memclaw.dev` | The slot is absent from the active sandbox registry, its replacement (if any) is healthy on the current domain, no workflow or callback allowlist names the old host, and the dedicated instrument records zero valid use for 30 consecutive days. | Cloud Run currently lists ten old-domain mappings, while the service inventory does not show their matching gateway services. Mapping existence is not reachability, ownership, or zero-use evidence. M1 must add a sandbox host class; no dedicated usage report exists. | Internal sandbox owners receive one change window plus 30 days; notify any external pilot directly if one is discovered. | Recreate the mapping to the recorded slot target; do not guess a target from the hostname. | Sandbox service owner with platform reliability attestation. | <!-- legacy-name-ok: names the unclassified legacy sandbox-domain range found in live mappings -->

### Found but never assigned a separate policy

The following were hidden by the phrase "the old domain":

- The gateway excludes authenticated application routes, static assets, `/_next`, OAuth
  compatibility, and `/join/*` from page redirects. They join S1/S3; a root-redirect probe
  says nothing about them.
- `www.memclaw.net` is a live mapping and a redirect hop; it is now permanent with the root. <!-- legacy-name-ok: records the separately mapped www alias -->
- `www.memclaw.dev` appears in the gateway map but has no live domain mapping. It is not an alias to maintain and rule 7 forbids provisioning it merely for symmetry. <!-- legacy-name-floor: records a dormant legacy hostname that must not be minted -->
- The production installer URL was not named in AI's explicit installer row even though the
  distribution E2E consumes it. It now shares the permanent installer policy.
- A join URL contains its bootstrap credential in the path. Any new measurement must normalize
  it to the `join` class before persistence and must never log the raw path, authorization
  header, API key, or response body.
- The six empty anti-hijack repositories and three archived old-name repositories are
  namespace aliases even though no source-code ratchet can see a repository name.
- GitHub still redirects two pre-rename repository URLs. Reusing either old namespace would
  destroy that compatibility path, so repository administration must treat the redirects as
  owned aliases rather than incidental GitHub behavior.
- Public and on-prem container image repositories still publish under old-name-bearing
  identities. No canonical counterpart or registry-side consumer ledger exists today; this
  policy makes the old references permanent if a counterpart is introduced.

### Active identities outside the alias clock

Some old-name-bearing resources are not compatibility aliases and must not be assigned an alias
sunset merely because their spelling is old:

- The internal Artifact Registry repositories `staging-memclaw` and `prod-memclaw` are active <!-- legacy-name-ok: names live registry ids until their separate rollback-safe retirement -->
  deployment resources. Their separate copy/cutover runbook permits deletion only after no
  rollback-reachable revision references the old repository and after at least one full release
  cycle beyond the last pre-cut production revision. That irreversible, storage-cost decision is
  not S1-S4.
- Caura-managed Pub/Sub topics and subscriptions, database roles/names, Cloud Run service ids,
  Secret Manager resource ids, VPC/network names, and internal monitor labels are active
  infrastructure or wire contracts. Their existing expand/drain/contract, revision, or
  dependent-first runbooks own their retirement evidence.
- Shared workflow inputs, secret keys, action inputs, release tags, and generated service-file
  names are also active wire or publication contracts until a versioned dual-read migration
  creates an alias. They follow the same dependent-first rule rather than a domain clock.
- A current identity is not made permanent merely by appearing here. If a migration creates an
  alias, the change must assign it to permanent-by-choice or add a complete measured-sunset row
  before the alias ships.

## Measurement M1: required before a machine-host clock can start

### What exists on 1 September 2026

Read-only inspection established:

- Live Cloud Run mappings route both retired apex hosts to their environment gateways; the
  production `www` alias is mapped separately.
- Cloud Run request entries preserve the original request host and path. Queries for both old
  apex hosts returned non-zero API/MCP traffic; therefore no current zero-use claim is possible.
- Request entries expose method, URL, status, user agent, remote IP, trace/span ids, and revision
  labels. They expose no organization, tenant, credential kind, or internal-caller marker.
- The default log bucket retains 30 days. There is no dedicated legacy-host log-based metric or
  export sink. The only configured sinks are the standard required/default sinks.
- AL sets a recognizable user agent and can be excluded exactly, but the estate contains other
  CI and internal callers that do not share a reliable marker.
- The gateway is plain nginx with no Datadog tracer. Its upstream proxy rewrites `Host` to the
  backend service and does not forward the original host on the general API/MCP routes. The
  org-aware backend therefore cannot answer the legacy-host question from its existing traces.

The conclusion is narrow and important: **an operator can see that a request arrived at an old
host today, but nobody can produce the organization-aware 90-day ledger that S1 requires.**

### Instrument to build

Platform reliability owns one sanitized gateway event for S1/S3 and any reachable S4 mapping:

- Emit only `legacy_host_class`, normalized `path_class`, response class, authenticated
  organization/tenant identifier, credential kind, environment, and an explicit internal-caller
  classification.
- Resolve authenticated identity from the existing gateway auth result or through the platform
  API. Never query the database directly and never persist a raw credential.
- Normalize `/join/<credential>` before logging. Do not persist full URLs, query strings,
  authorization headers, cookies, API keys, remote IPs, or response bodies in this ledger.
- Route the event to a dedicated store retained for at least 180 days and publish a weekly report
  of external organizations last seen on each alias class. Alert when an external organization
  reappears during a zero-use clock.
- Tag every Caura-owned caller by authenticated organization or an explicit workload identity.
  User agent alone is not an internal/external boundary.
- Record the instrument deployment time and the time each internal default moved. The clock starts
  at the later timestamp; earlier logs cannot be backfilled into proof.

Customer support owns the organization-contact ledger and notice timestamps. Platform reliability
signs the traffic evidence. The Caura release owner is the only role allowed to declare the gate
met, and only from those two written records.

## Retirement procedure for S1 and S3

1. Deploy M1, move every known Caura-owned caller to the canonical host, and record both times.
2. Run a fixed 30-day baseline beginning at the later time. Directly notify every external
   organization observed in it; this starts that organization's 180-day minimum.
3. Keep measuring and notify any external organization first observed later. The earliest 410
   date moves to 180 days after the latest first-notice timestamp; an unknown or uncontactable
   organization blocks retirement.
4. Start or continue the 90-day zero-valid-external-use clock. It may overlap the notice period,
   but must end no earlier than the last applicable 180-day minimum. Any valid external request
   resets the zero-use clock. If it never reaches 90 days, keep the alias deliberately.
5. Assemble the evidence packet: instrument version and retention, internal-caller inventory,
   per-org last-seen report, notice ledger, AL availability history, rollback owner, and previous
   gateway revision.
6. After the release owner signs, return **410 Gone** on the retired machine paths with a body that
   names the canonical endpoint. Do not redirect credentials across hosts.
7. Hold DNS/domain mapping, TLS, telemetry, and the previous route for 30 days. Restore direct
   routing and reset the clock if a missed consumer is verified.
8. Only a later, separately approved infrastructure change may remove the retired machine
   upstreams. The domain, DNS, TLS, and enough routing to serve permanent roots, installers, and
   historical objects remain. They may move to a cheaper edge, but the permanent URLs may not
   disappear. This document performs none of those actions.

## Recommendation and priced alternatives

**Recommendation:** keep low-cost and unmeasurable installed, package, image, discovery, and
history aliases permanently; build M1 only for live network machine surfaces; then use notice,
zero-valid-use evidence, and a reversible 410 stage to retire those surfaces. Freeze the old
release channel only after its self-migrating bridge passes two successor releases.

The alternatives cost more than they first appear:

- **Keep every network surface forever:** avoids M1 and outreach, but every gateway/auth change
  carries two host contracts indefinitely; certificates, callbacks, monitoring, attack surface,
  and incident diagnosis retain the old brand forever.
- **Choose a calendar cutoff:** avoids instrumentation, but makes breakage the measuring tool. It
  cannot identify who needs notice and gives unattended agents a silent outage.
- **Use raw Cloud Logging counts:** requires no build, but 30-day retention cannot prove 90 days,
  scanners and CI prevent a meaningful zero, and no organization exists to contact. This is not a
  retirement instrument.
- **Measure every permanent installed alias:** would require telemetry in offline scripts,
  package managers, cron, air-gapped images, container pulls, and customer files. That collection
  costs more and creates more privacy risk than keeping the aliases.

## Draft replacement for rule 3

The [sunset plan](rebrand-sunset-plan.md) can replace rule 3 and add the following paragraph in one
pass:

> **Old names stay readable under an explicit policy.** Package and image forwarders,
> installed-machine identifiers, redirects, immutable historical objects, and other aliases
> classified as permanent remain readable indefinitely. A live machine alias may retire only
> when its policy names an owner, a privacy-safe instrument, a notice period, an objective
> zero-use condition, and a tested rollback; its evidence clock starts only after that instrument
> and the canonical default are live. An unmeasured alias does not retire. Authenticated endpoints
> return a descriptive 410 during a reversible observation period and are never redirected across
> hosts.

And immediately below it:

> Permanent is a decision, not the absence of a cleanup ticket. Time-bounded aliases carry their
> condition beside the compatibility mechanism; aliases without one are policy defects and stay
> active until the defect is resolved.

## Evidence boundary and commands

Verified against these repository heads: `caura` `67110830`, `caura-daemon` `32b22294`,
`caura-test-automation` `a0ee9f33`, and `caura-enterprise` `75ff46b8`.

Read-only operations used:

- `git rev-parse --is-shallow-repository`, `git fetch`, `git show`, `git grep`, `rg`, and the
  do-not-touch sentinel's `--list` mode.
- `gh api user`, authenticated repository reads (including redirect resolution), and an
  organization repository inventory.
- `gcloud auth list`; Cloud Run service/domain-mapping lists; Cloud Logging request-schema,
  retention, metric, and sink reads. Log queries emitted only field names or aggregate host/path
  classes; no secret value, credential path, response body, or raw request was printed.

Could not verify registry consoles, anonymous package-manager installed bases, offline scripts,
or a live organization-aware old-host report, because no such report exists. No traffic was sent
to either retired host during this policy investigation.

Nothing was retired, deleted, renamed, redirected, published, deployed, or changed outside this
document. No DNS, infrastructure, workflow, probe, package channel, repository setting, or live
service was modified.
