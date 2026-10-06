# core-storage-api operator scripts

Standalone CLIs that ship in the source tree but are **not** imported by
the running service. Each is invokable with `python -m
core_storage_api.scripts.<name>` from inside the
`core-storage-api` container (or any environment with the package on
`PYTHONPATH`).

| Script | What it does | Read/write | When to use |
|---|---|---|---|
| `preflight_012.py` | Reports rows that migration `012_vector_dim_1024` would NULL, estimates UPDATE wall-clock, prints opt-in command. | Read-only | Before triggering migration 012 on staging or prod. |
| `backfill_embeddings.py` | Re-embeds rows whose embedding is NULL after migration 012. | Read + write | After migration 012 completes, on OSS docker-compose deployments. (Enterprise cutovers should prefer the event-driven backfill task in `core-worker`.) |
| `backfill_057_entity_search_vector.py` | Rebuilds `entities.search_vector` so entities written before migration 057 can be found by their aliases. `--dry-run` counts, `--from-id` resumes, `--revert` restores name-only vectors after a downgrade. | Read + write | Once, after the storage deploy that runs migration 057 is healthy. Safe to run while serving and to re-run. |
| `backfill_058_orphaned_derived_rows.py` | Soft-deletes live rows whose `metadata.parent_memory_id` names a parent that is gone (deleted before the delete cascade of caura PR #1843, or never written), as every delete now does. `--dry-run` counts the first level, `--tenant-id` scopes a run to one tenant. | Read + write | Once, after the storage deploy that runs migration 058 is healthy. Safe to run while serving and to re-run. |
| `backfill_060_entity_created_at.py` | Dates each entity from its earliest linked memory (H-05): migration 060 gave every existing entity the migration's time, and the nightly duplicate merge keeps the first seen of two compatible names (caura PR #1876). Only ever moves a time earlier, and counts a memory only in the entity's own tenant. `--dry-run` counts, `--tenant-id` scopes a run to one tenant, `--after-id` resumes. | Read + write | Once, after the storage deploy that runs migration 060 is healthy. Safe to run while serving and to re-run. |

See `docs/local-embedder.md` for the full upgrade walkthrough.
