# Upgrading from v1.x

> **⚠️ v2.0.0 ships a destructive schema migration.** If your installation is on
> v1.x and has any memories already stored, follow this procedure carefully — the
> migration NULLs every existing embedding to widen the pgvector column from
> 768 → 1024 dim. The application is designed to refuse the migration
> automatically; you must opt in.

## What changes

- The supported vector width changes from 768 to 1024 dimensions. Hosted
  OpenAI `text-embedding-3-small` remains the default; `BAAI/bge-m3` is an
  optional self-hosted provider through the new `tei` Compose profile. See
  [`local-embedder.md`](local-embedder.md).
- pgvector schema dim: `vector(1024)` (was: `vector(768)`).
- Existing embeddings on `memories.embedding`, `entities.name_embedding`, and
  `documents.embedding` are NULLed by alembic revision `012_vector_dim_1024`.
  Re-embedding is required for full semantic recall. Until then, NULL-vector
  rows can still surface through full-text keyword matching, but they do not
  contribute a vector-similarity score.

## Procedure (OSS, docker-compose)

1. **Stop the stack** so no writes happen during migration:

   ```bash
   docker compose down
   ```

2. **Snapshot the database.** A `pg_dump` is the safest fallback. Replace
   `<container>` with the running PostgreSQL container name (typically
   `caura-db-1`):

   ```bash
   docker compose up -d db    # bring just the DB back
   docker exec <container> pg_dump -U memclaw memclaw > backup-pre-v2.sql
   docker compose down
   ```

3. **Select a v2 image, pull it, and run the migration explicitly.** If `.env`
   pins `CAURA_VERSION` to a v1 tag, update it to the v2 release you are
   installing first; otherwise `docker compose pull` will fetch v1 again. The
   stock Compose service does not forward arbitrary shell variables into the
   container, so pass the opt-in on the one-off migration command itself:

   ```bash
   docker compose pull
   docker compose run --rm \
     -e CAURA_RUN_DESTRUCTIVE_MIGRATIONS=true \
     core-storage-api \
     python -c 'import asyncio; from core_storage_api.database.init import init_database; asyncio.run(init_database())'
   docker compose up -d --wait
   ```

   `init_database()` runs `alembic upgrade head` through the same advisory-lock
   path used at normal service startup. The migration runs in
   seconds-to-minutes for typical OSS workloads.

4. **Verify the restarted service is at the current schema head.** The
   `--wait` command above checks readiness; the storage log should also show
   that no migration remains:

   ```bash
   docker compose logs core-storage-api | \
     grep -i "schema already at head\|database initialization complete"
   ```

5. **Re-embed your data.** Three paths are available:

   - **Keyword-only while waiting:** no automatic read-time re-embedding occurs.
     NULL-vector rows may still appear through full-text matching, but semantic
     paraphrases will miss them. Run one of the eager paths below to restore
     full semantic recall.
   - **Eager (recommended for the stock OSS stack):** run the bundled backfill CLI. It walks every
     memory and entity with a NULL embedding and re-embeds via the configured
     provider. Idempotent — safe to re-run. First do a dry-run to estimate
     scope. The `--env-from-file` option requires Docker Compose 2.34+; if
     `docker compose run --help` does not list it, upgrade Compose first:

     ```bash
     docker compose run --rm --env-from-file .env core-storage-api \
       python -m core_storage_api.scripts.backfill_embeddings --dry-run
     ```

     Then the real run:

     ```bash
     docker compose run --rm --env-from-file .env core-storage-api \
       python -m core_storage_api.scripts.backfill_embeddings
     ```

     `--env-from-file .env` is required: unlike `core-api`, the stock
     `core-storage-api` service does not load provider settings from `.env`.
     Without it, the CLI falls back to fake embeddings.

     Optional knobs: `--tenant-id <id>` (per-tenant cutover), `--batch-size N`,
     `--max-inflight N`, `--only-table memories|entities`. Documents are NOT
     covered (their embed-source field is per-row JSON, not a fixed column);
     re-write any embedded documents to refresh them.
   - **Eager (event-driven, recommended for multi-tenant production):** if you
     run the `core-worker` service, drive the existing `EMBED_REQUESTED`
     consumer instead. The CLI scans `WHERE embedding IS NULL` and publishes
     one event per row, inheriting per-tenant concurrency + retry + DLQ:

     Run these commands inside an already configured `core-worker` runtime;
     the stock Compose stack does **not** define a `core-worker` service:

     ```bash
     python -m core_worker.cli backfill-embeddings \
       --tenant-id <tenant-id> --dry-run
     python -m core_worker.cli backfill-embeddings \
       --tenant-id <tenant-id>
     ```

     `--tenant-id` is required; repeat the command once per tenant for a full
     deployment cutover. The other knobs are `--batch-size`, `--max-inflight`,
     and `--dry-run`. This path currently covers `memories` only. Self-hosters
     on the stock stack should use the `core-storage-api` variant above.

## What if I skip the opt-in?

The one-off migration command exits non-zero with a clear error reporting how
many rows would be NULLed. No data is touched. Add the explicit opt-in and
retry.

## Rolling back

Restore the `pg_dump` snapshot from step 2. Migration 012 has a symmetric
`downgrade()`, but it NULLs every 1024-dim embedding written since the upgrade
before restoring `vector(768)`, so restoring the pre-upgrade snapshot is the
safer and simpler recovery path.

## v1.x → v2.x compatibility for client code

No public API changes. Code that reads memory embeddings via the search/recall
endpoints is unaffected. External clients should not assume a vector width;
custom integrations that directly handle the internal storage schema must
migrate from 768 to 1024 dimensions with the database.
