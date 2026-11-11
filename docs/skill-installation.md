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
the prior completed reconciliation summary; it is a node-reported snapshot,
not a durable installation receipt or attestation. Durable installation events
are a separate change. Existing files are not remotely attested by this API.
