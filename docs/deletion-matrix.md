# Deletion matrix

Where a memory's text, text derived from it, or other personal data can be
stored, and which deletion reaches each copy.

Three deletions are covered:

- **Memory delete**: `DELETE /api/v1/memories/{id}`, the bulk and filter
  deletes, `/ingest/undo` and a governance policy drop. The memory is
  soft-deleted: it stays in `memories`, flagged deleted, until the retention
  purge. Rows derived from it are soft-deleted with it. A policy drop also
  clears its text, title, embedding and triple at once.
- **Retention purge**: the daily lifecycle purge, per organization. It
  hard-deletes memories that have been deleted for longer than the org's
  `lifecycle.memory_retention_days` (1 to 30, default 30), at most 500 per org
  per run. It also deletes the graph only those memories supported, recall-log
  events older than the same window, and expired idempotency responses.
- **Organization purge**: `POST /api/v1/admin/org/purge-data`, which deletes
  every row of each tenant except those listed as retained.

`tests/test_deletion_matrix.py` holds this table to the code. Every
tenant-scoped table has to appear in it. A memory is then written with a unique
token, deleted, and purged, and after each step every table is searched for
the token. It may remain only where this table says **Kept** for that step.

## In the database

| Store | Holds | Memory delete | Retention purge | Organization purge |
|---|---|---|---|---|
| `memories` | The memory: content, title, triple, source, metadata and embedding | **Kept**, flagged deleted, and readable only with `include_deleted` by a tenant credential, a session or an agent at trust level 3. A policy drop clears the text at once | **Cleared**: the row is hard-deleted | **Deleted** |
| `memory_entity_links` | Which entities a memory mentions (ids) | **No text** | **Cleared** with the memory | **Deleted** |
| `entities` | Names and attributes extracted from memories | **Kept** | **Cleared** for entities only purged memories mentioned. An entity another memory mentions stays | **Deleted** |
| `relations` | Relations between entities, with the memory that evidences each | **No text** | **Cleared** when their evidence is purged | **Deleted** |
| `relation_evidence` | Which memories evidence a relation (ids) | **No text** | **Cleared** with the memory | **Deleted** |
| `memory_conflicts` | A judge's diagnosis of two contradicting memories, which may quote them | **Kept** | **Cleared** with either memory | **Deleted** |
| `memory_derivations` | Which memory was derived from which (ids) | **No text** | **Cleared** with the memory | **Deleted** |
| `memory_origins` | The session a memory was written in (ids) | **No text** | **Cleared** with the memory | **Deleted** |
| `memory_reuse` | Which session read which memory (ids) | **No text** | **Cleared** with the memory | **Deleted** |
| `memory_exposure` | Which agents read which memory, how often and when (ids and counts) | **No text** | **Cleared** with the memory | **Deleted** |
| `memory_correction` | Corrections of a memory: who made each and when, and for a content edit or a conflict review the text before and after, up to 280 characters each. A conflict review's record also quotes the other memory of the pair | **Kept**. An agent that read the memory is still shown a pending notice of it on recall, for up to 7 days after the correction. The delete adds a record of its own, with only the old and new status | **Cleared** with the corrected memory. A conflict review's quote of the other memory stays until the corrected memory is purged | **Deleted** |
| `memory_correction_recipient` | Which agents a correction is for, and whether each was told (ids and delivery states) | **No text** | **Cleared** with its correction | **Deleted** |
| `agent_sessions` | Agent sessions (ids) | **No text** | **No text** | **Deleted** |
| `audit_log` | Who did what to which resource. Memory rows record no title and no edited text, only which text fields changed. Rows written before that change keep a title or 200-character excerpts until the organization purge: the log is hash-chained, so a row can't be edited | **No text** | **No text** | **Deleted** |
| `audit_chain_head` | The audit chain's latest hash | **No text** | **No text** | **Deleted** |
| `documents`, `ingest-sources` collection | Each ingest batch's parent record, with a summary of up to 500 characters of its facts and an embedding of it | **Cleared**: deleting any fact of the batch clears the summary and its embedding. The rest of the record stays | **Cleared** | **Deleted** |
| `documents`, `skills` and `skills_rollback` collections | Skills the Skill Factory writes from clusters of memories | **Kept** | **Kept** | **Deleted** |
| `documents`, `interview_jobs` collection | The masked session events the interviewer turns into memories (off by default) | **No text** | **No text**. Finished jobs are deleted 14 days after they finish | **Deleted** |
| `documents`, any other collection | Documents agents write themselves | **No text** | **No text** | **Deleted** |
| `keystone_versions` | Every version of the tenant's keystone documents | **No text** | **No text** | **Deleted** |
| `analysis_reports` | Crystallizer reports: counts, issue codes and memory ids | **No text** | **No text** | **Deleted** |
| `dedup_reviews` | Pairs of statements held for a duplicate review, verbatim. Nothing in this repository writes the table today | **No text** | **No text** | **Deleted** |
| `idempotency_responses` | The replayable response to a write sent with an `Idempotency-Key`, which can include the memory's content | **Kept** until it expires, 24 hours after the write by default | **Cleared** once expired | **Deleted** |
| `recall_event` | Logged recall queries, with personal data masked (opt-in per tenant) | **No text** | **No text**. Events older than the retention window are deleted | **Deleted** |
| `recall_candidate` | The memories a logged recall considered (ids) | **No text** | **No text**. Deleted with their event | **Deleted** |
| `session_traces` | Agent sessions' goal phrases and the memory ids they used | **No text** | **No text** | **Deleted** |
| `agent_activity_digests` | Per-agent summaries of recent memories, written by a model (opt-in) | **Kept** | **Kept**. Each digest run deletes digests older than its `retention_days` (default 90) | **Deleted** |
| `background_task_log` | Background task names, statuses and errors | **No text** | **No text** | **Deleted** |
| `forge_rejected_fingerprints` | Skill clusters a reviewer rejected, with the reason | **No text** | **No text** | **Deleted** |
| `agents` | Registered agents and their profiles | **No text** | **No text** | **Deleted** |
| `fleet_nodes` | Plugin nodes and what they report about themselves | **No text** | **No text** | **Deleted** |
| `fleet_commands` | Commands sent to plugin nodes, and their results | **No text** | **No text** | **Deleted** |
| `capability_usage` | Counts of which capabilities were used | **No text** | **No text** | **Deleted** |
| `tenant_usage_counters` | Usage counts | **No text** | **No text** | **Deleted** |
| `llm_usage_minutes` | Model token counts per minute | **No text** | **No text** | **Deleted** |
| `value_usage_minutes` | Value metering counts per minute | **No text** | **No text** | **Deleted** |
| `organization_settings` | The organization's settings | **No text** | **No text** | **Deleted** |
| `organization_settings_audit` | Changes to those settings | **No text** | **No text** | **Deleted** |
| `tenant_suppression` | Whether the tenant's organization is deleted and waiting out its grace period | **No text** | **No text** | **Retained**: it records the deletion |
| `lifecycle_audit` | Lifecycle job runs, with counts | **No text** | **No text** | **Retained**: it records the purge itself |

**No text** means the store holds no text taken from a memory. A cell can
still say what else happens to the store's rows.

## Outside the database

| Store | Holds | What removes it |
|---|---|---|
| Short-term memory (Redis or in-process, off by default) | Agents' notes and fleets' bulletins: not memories, but free text | Deleting an agent clears its notes. An organization purge clears every note and bulletin of its tenants. Otherwise they expire after 24 hours (notes) or 48 hours (bulletins) |
| Query embedding cache (Redis) | Embeddings of search queries: vectors, no text | Expires after 3 days |
| Event bus messages | Enrichment and embedding requests carry the memory's content. Correction events carry the text before and after, up to 280 characters each | A handled message is gone. With Pub/Sub, a message that keeps failing moves to a dead-letter subscription and stays for that subscription's retention, which the deployment's infrastructure sets. The in-process bus keeps nothing |
| Correction notices on the Caura bus (only with `COLLABORATION_API_URL` set) | A correction's text before and after, up to 280 characters each, sent to the agents that read the memory | The collaboration service's own retention. No deletion here reaches it |
| Logs | Whatever a deployment's log level records | The deployment's log retention |

## Known gaps

These keep text derived from a deleted memory until the organization purge or
their own retention:

- skills written from memories (`documents`, `skills`);
- agent activity digests (`agent_activity_digests`), until the next digest run
  past their retention;
- a judge's diagnosis of a contradiction (`memory_conflicts`), until the
  memory is purged;
- a correction's text before and after (`memory_correction`), until the
  corrected memory is purged, and pending notices of it for up to 7 days. A
  conflict review's record also keeps the other memory's text after that
  memory is purged;
- correction notices already sent over the Caura bus;
- audit rows written before memory rows stopped recording text.
