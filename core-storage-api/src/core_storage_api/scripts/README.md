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

See `docs/local-embedder.md` for the full upgrade walkthrough.
