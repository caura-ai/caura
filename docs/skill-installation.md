# Approving skill delivery

An active skill is eligible for approval; active status alone does not deliver
it to a node. An organization or system administrator approves the rendered
artifact hash and a snapshot of registered node UUIDs. Each recipient includes
the credential-owner binding read from the primary. Approval records retain
the approver, server time, source version and rendered SHA-256. A subsequent
approval publishes a new immutable record and replaces the current pointer;
its recipient list replaces the prior list for future delivery.

Use `POST /api/v1/skills/{doc_id}/delivery` with `expected_artifact_hash` and
`node_ids` (1–256 UUIDs). Omit `tenant_id` to use the credential's tenant;
system administrators must name it. The hash is `sha256:` followed by the
lowercase hex SHA-256 of the final UTF-8 SKILL.md. Existing frontmatter passes
through unchanged; otherwise render JSON-quoted `name` and `description` as
YAML frontmatter, followed by the content. Both server and plugin trim only
ASCII space, tab, CR and LF from metadata edges; other Unicode characters are
preserved. This explicit set keeps their rendered bytes and hashes identical.
Metadata changes also invalidate the source binding, even if the file contains
its own frontmatter. A stale hash, inactive skill, unbound/foreign receiver, or
unattributable administrator
cannot create an approval.

The plugin first sends an authenticated heartbeat. Only the node UUID in that
successful response authorizes its next delivery request. A local node name,
caller-supplied fleet, or agent label cannot supply this identity. The request
is `POST /api/v1/skills/installable` with `node_id` and `delivery_version: 1`.
The response contains `delivery_version`, `node_id`, `complete: true`, `prune_allowed`,
`documents` with grant/hash/byte metadata, and content-free `held` entries.
The receiver credential must match the stored node owner. The tenant-wide
key matches a tenant-owned node; it does not bypass a different owner binding.
New nodes and rotated owners need approval again. Mutable fleet membership
does not extend an approved snapshot.

Current approval reads request only the valid active catalog's pointer IDs,
up to 64 per internal batch with at most eight concurrent primary reads. Each
lookup transfers only the requesting node's matching recipient entry. Deleted,
inactive and renamed skills' stale pointers do not enter the scan or payload;
immutable approval history remains stored.

An unsafe active catalog ID is skipped and logged with its tenant and ID. It
appears in neither `documents` nor `held`. It sets `prune_allowed: false`:
approved skills still install or update, but existing copies in owned
and additive directories are not pruned while that identity is uncertain.
Foreign-tenant rows and failed approval batches fail the whole request. The
plugin requires a boolean pruning permission; missing or malformed permission
preserves disk state.

Legacy active skills without a matching current approval are held. Existing
copies stay unchanged and absent copies are not created. Holds do not grant
approval, change lifecycle status, or count as approved installations. Inactive
or removed, unheld skills still follow the existing owned/additive pruning
rules. Directory ownership, collisions and bundled-skill protections apply.

Each final file is limited to 65,536 UTF-8 bytes, including generated
frontmatter. Every streamed manifest response, including an error body, is
limited to 4,194,304 bytes before JSON parsing; Content-Length alone is not
trusted. All artifacts must pass validation before any writes or pruning.
Writes use a same-directory temporary file whose bytes are verified before
atomic replacement. Invalid, incomplete, oversized or failed responses leave
the installed set unchanged. A saturated 5,000-row active-catalog storage page is refused
rather than returned as a partial desired set; manifests over 4 MiB are also
refused in full. Generic REST/MCP document operations cannot read, write or
delete approval records. They live in `_skill_delivery_grants`, a reserved
namespace that generic storage writes already refused before this API existed;
old caller-written documents cannot become approvals. A narrow internal writer
publishes only to that namespace.

Deploy the storage lookup and core API before releasing the matching plugin.
Old plugins' unversioned requests fail explicitly; they retain their existing
copies. New plugins have no generic-catalog fallback. The heartbeat carries
the prior completed reconciliation summary; it remains a node-reported snapshot,
separate from the durable results below. Existing files are not remotely
attested by this API.

## Installation results

`POST /api/v1/skills/installations` accepts `reporting_version: 1`, `node_id`
and 1–64 events. Each event carries a stable UUID `event_id`, `skill_doc_id`,
immutable approval `grant_id`, `artifact_hash`, `outcome` (`installed`,
`updated` or `removed`), an opaque UUID `target_id`, and the node's local
`occurred_at` time. The server derives tenant/credential/node attribution and
adds its own receipt time. Raw skill content, absolute paths, caller-supplied
actor/fleet claims and unknown fields are refused. The credential must still
match the node's current owner, and the exact historical approval must match
that node/owner, skill and artifact. Reporting does not grant new delivery or
activate a skill. A superseded current approval does not invalidate an offline
result for the earlier artifact.

The API acknowledges `accepted_event_ids` only after synchronous durable
storage accepts the whole batch. Stable IDs are namespaced by tenant, node
and owner in the existing event store. A lost acknowledgment can be retried
with the same IDs without appending another result. Invalid or failed storage
responses acknowledge nothing. An ID denotes one report; replaying it does not
replace the first stored report. This is at-least-once submission with storage
deduplication, not an exactly-once filesystem transaction or remote attestation.
A compromised node can report a false outcome.

The plugin keeps `skill-installations.json` in its private installation
directory. It retains approval bindings for files it actually materializes,
pending outcomes and write-ahead intent, with a total 1 MiB ceiling and at
most 256 pending events plus 256 quarantined rejections. POSIX files are mode
`0600`; Windows uses inherited profile-directory access controls. Updates use a
verified same-directory temporary file and atomic rename. File data is synchronized before publication;
directory entries are also synchronized on POSIX. Direct reconciliation calls
are serialized within the gateway; run one gateway writer per plugin directory.

Intent is persisted before a skill mutation. Success becomes reportable only
after the installed bytes verify or a tracked directory removal completes.
After restart, a reserved write whose exact bytes are present can finish its
report; an unperformed intent is not reported as success. Unreadable or damaged
state is held for operator review rather than reset. Changed tenant identity
always holds the original state. A changed node ID within the same tenant
loads the journal in drain-only mode. It recovers old intent and submits ready
results with the journal's original node ID, while both write and removal
methods refuse mutations. Once active results are accepted or explicitly
quarantined, it persists fresh node/target bindings and discards the prior
node's installed bindings;
unchanged existing files gain no new installation history. Quarantined reports
retain their original tenant/node identity across that rebind.
Retry IDs and local target IDs survive restart. Events never include private
target paths, though recovery necessarily retains them in the private file.

Unchanged copies, rejected artifacts, failed writes and foreign additive
collisions produce no successful-installation event. Legacy copies without a
recorded materialization keep existing pruning rules without invented approval
history. A tracked file changed outside reconciliation is held before removal
reporting. Corrupt, oversized, unwritable or full state pauses skill changes;
heartbeat and command handling stay available. Failed delivery, malformed
acknowledgments and offline result storage retain pending events. Reports use
bounded batches and short request deadlines, so an offline result endpoint
cannot hold heartbeats behind the usual long request timeout.

A batch rejected with 409 or 422 is retried individually to isolate invalid
reports. Each individually rejected report moves atomically to the bounded
private `rejected` list, retaining its original event ID, node/tenant, status
and rejection time, with an explicit log entry. It is not acknowledged as
accepted or silently deleted, and no longer blocks active delivery. Other
failures, including network/timeouts, 5xx, 408 and 429, retain the original
pending events for recovery. For the current receiver, 401/403 and a missing
route also stay retryable. When heartbeat supplies a replacement node ID,
an actual old-node submission refused with 401/403/404 instead moves that
batch to the bounded quarantine with `reason: superseded_receiver` and the
replacement UUID. This is an explicit local disposition of obsolete-receiver
reports, not acceptance or proof of permanent server rejection; original IDs,
artifacts, node/tenant, status and time remain available for investigation.
It lets an inaccessible old receiver finish draining without a manual file
edit. A transient failure still holds drain-only mode until recovery.
Isolation shares a ceiling
of eight reporting requests per reconciliation, each with a two-second
deadline. If quarantine or the total file budget fills, further rejections
stay pending rather than evicting old evidence; continued exhaustion can pause
mutations until an operator resolves the retained reports. New manifests and
write intents require UUID grant IDs. Older non-UUID pending reports can still
receive an explicit rejection without locking the whole journal.

Deploy the reporting API before releasing this plugin version. A missing or
incompatible endpoint never counts as acceptance; results remain local and
eventual outbox saturation pauses further mutations. These results describe
node-reported operations after this version starts recording them. They do not
backfill prior installation history or prove what files currently exist.
