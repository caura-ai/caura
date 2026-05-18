# Changelog

All notable changes to MemClaw are documented in this file. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Subsequent releases are produced by [release-please](https://github.com/googleapis/release-please-action)
from [Conventional Commits](https://www.conventionalcommits.org/).

## [2.7.0](https://github.com/caura-ai/caura-memclaw/compare/backend-v2.6.0...backend-v2.7.0) (2026-05-18)


### Features

* **core-api:** cross-tenant read plumbing + capabilities ([#154](https://github.com/caura-ai/caura-memclaw/issues/154)) ([27b02f6](https://github.com/caura-ai/caura-memclaw/commit/27b02f6eb72e1abe17157c1bd13faadaf12ec448))

## [2.6.0](https://github.com/caura-ai/caura-memclaw/compare/backend-v2.5.0...backend-v2.6.0) (2026-05-17)


### Features

* **core-api:** install-credential auth context + http:// ID-token bypass ([#146](https://github.com/caura-ai/caura-memclaw/issues/146)) ([160e143](https://github.com/caura-ai/caura-memclaw/commit/160e14317b2e3ace4ab8eecb42a4e4330b40cbf0))
* **ingest:** 3 MB body cap + POST /ingest/file + text/csv support ([#130](https://github.com/caura-ai/caura-memclaw/issues/130)) ([eb1e3ce](https://github.com/caura-ai/caura-memclaw/commit/eb1e3ce19099313a4de7d237bc951f4236ef999a))
* **ingest:** parent Document per batch + run_id index + undo migration ([#134](https://github.com/caura-ai/caura-memclaw/issues/134)) ([f2e0204](https://github.com/caura-ai/caura-memclaw/commit/f2e02041c48e4edaf542f91d4295fc97abb7968a))
* **memories:** add run_id query filter to /api/memories ([#145](https://github.com/caura-ai/caura-memclaw/issues/145)) ([1063590](https://github.com/caura-ai/caura-memclaw/commit/1063590e9565f11431d3e8f4cd38940eacaacc80))
* **plugin:** decouple plugin release cadence from backend ([#131](https://github.com/caura-ai/caura-memclaw/issues/131)) ([ac6c0f2](https://github.com/caura-ai/caura-memclaw/commit/ac6c0f2ec05a020b0acb27b6ec7b92a0be338d73))


### Bug Fixes

* **ingest:** preserve uploaded filename in source_uri ([#133](https://github.com/caura-ai/caura-memclaw/issues/133)) ([b25e609](https://github.com/caura-ai/caura-memclaw/commit/b25e60970eb5c1a84c5e920b158933e6c57597a7))
* **search:** trust has_embedding sentinel in post-filter for FTS-only rows ([#150](https://github.com/caura-ai/caura-memclaw/issues/150)) ([cc1d311](https://github.com/caura-ai/caura-memclaw/commit/cc1d311ace07d6305717a0a4e32efa0a6dcf21fa))
* **write:** close fast-branch coverage holes + add always-fire detection logs ([#152](https://github.com/caura-ai/caura-memclaw/issues/152)) ([ffb26fd](https://github.com/caura-ai/caura-memclaw/commit/ffb26fd2ed8e65068c9fc479992872d39734fa2a))
* **write:** make ParallelEmbedEnrich deferral write_mode-aware ([#151](https://github.com/caura-ai/caura-memclaw/issues/151)) ([f2236ad](https://github.com/caura-ai/caura-memclaw/commit/f2236adc057274307c01364fa4da93334cb75ed7))

## [2.5.0](https://github.com/caura-ai/caura-memclaw/compare/v2.4.0...v2.5.0) (2026-05-11)


### Features

* **core-storage:** keystone-rule CRUD on _keystones collection (CAURA-000) ([#109](https://github.com/caura-ai/caura-memclaw/issues/109)) ([bd94a9e](https://github.com/caura-ai/caura-memclaw/commit/bd94a9e13fe9e2e99151766a583a4a0fddfc9e15))
* **fleet-stats:** include agent trust_level in per-agent records ([#116](https://github.com/caura-ai/caura-memclaw/issues/116)) ([1c0e6e7](https://github.com/caura-ai/caura-memclaw/commit/1c0e6e75d8f3cd8e624a0ec1c225cb50c4cc488b))


### Bug Fixes

* **ingest:** harden URL fetch (Content-Type allowlist + size cap + SS… ([#115](https://github.com/caura-ai/caura-memclaw/issues/115)) ([9a36656](https://github.com/caura-ai/caura-memclaw/commit/9a36656ddb335956a78537eecdec2ec668eacee7))
* **ingest:** strong-mode writes + pre-loop dedup + parallel commit (P… ([#117](https://github.com/caura-ai/caura-memclaw/issues/117)) ([8359df1](https://github.com/caura-ai/caura-memclaw/commit/8359df1fd608f891a8a795da6e093c74e705536c))

## [2.4.0](https://github.com/caura-ai/caura-memclaw/compare/v2.3.0...v2.4.0) (2026-05-10)


### Features

* **fleet-stats:** add memory-status totals to fleet_summary ([#107](https://github.com/caura-ai/caura-memclaw/issues/107)) ([866c5ac](https://github.com/caura-ai/caura-memclaw/commit/866c5ac298a48affc4476b9853bb3474b8cc159a))
* **recall:** exclude superseded memories from default search results ([#106](https://github.com/caura-ai/caura-memclaw/issues/106)) ([dc03047](https://github.com/caura-ai/caura-memclaw/commit/dc030472e630ba9149636125e5c883dde75cef54))
* **stats:** add include_deleted option to memclaw_stats / /memories/stats ([#105](https://github.com/caura-ai/caura-memclaw/issues/105)) ([d8121f6](https://github.com/caura-ai/caura-memclaw/commit/d8121f64c67d8293681b397e1caa9c6ba2b10ece))


### Bug Fixes

* **cache:** include VECTOR_DIM in qemb query-embedding cache key (CAURA-644) ([#69](https://github.com/caura-ai/caura-memclaw/issues/69)) ([761161c](https://github.com/caura-ai/caura-memclaw/commit/761161cac54155926f0a73b500435f9702609551))
* **core-api:** align write/query embedding surface (CAURA-222) ([#104](https://github.com/caura-ai/caura-memclaw/issues/104)) ([9ff97cd](https://github.com/caura-ai/caura-memclaw/commit/9ff97cd63ead521c37a435baf32cd6f6ced9905e))
* **release-please:** add signoff so release PRs pass DCO ([#101](https://github.com/caura-ai/caura-memclaw/issues/101)) ([50369b0](https://github.com/caura-ai/caura-memclaw/commit/50369b006c8a55d81f392257d9acdb9742ec41a7))

## [2.3.0](https://github.com/caura-ai/caura-memclaw/compare/v2.2.0...v2.3.0) (2026-05-06)


### Features

* **plugin:** synthesize skill frontmatter + anchor catalog discovery ([#99](https://github.com/caura-ai/caura-memclaw/issues/99)) ([54f2eb4](https://github.com/caura-ai/caura-memclaw/commit/54f2eb43982de6cea6b28f746711169be3903177))

## [2.2.0](https://github.com/caura-ai/caura-memclaw/compare/v2.1.0...v2.2.0) (2026-05-06)


### Features

* skills as documents (Phase A) — plugin reconciler ([#94](https://github.com/caura-ai/caura-memclaw/issues/94)) ([7789091](https://github.com/caura-ai/caura-memclaw/commit/778909185b544d44b88bb390868a25965e1b2e54))


### Bug Fixes

* **core-api:** read version from installed package metadata ([#96](https://github.com/caura-ai/caura-memclaw/issues/96)) ([1add99e](https://github.com/caura-ai/caura-memclaw/commit/1add99ebf525ba9abfcecca7fe83424fdad2e4ae))


### Documentation

* align tool count to 10 in README and integration guide ([#98](https://github.com/caura-ai/caura-memclaw/issues/98)) ([fba2903](https://github.com/caura-ai/caura-memclaw/commit/fba290356f4467ffae75951a6e95b8ac829f70e6))

## [2.1.0](https://github.com/caura-ai/caura-memclaw/compare/v2.0.0...v2.1.0) (2026-05-06)


### Features

* add security.session_idle_timeout_minutes org setting (CAURA-652) ([#89](https://github.com/caura-ai/caura-memclaw/issues/89)) ([d264cfe](https://github.com/caura-ai/caura-memclaw/commit/d264cfec2067befc1e014f8e31ddddd5476df9ab))
* **core-operations:** scaffold OSS service for cron/scheduled jobs (CAURA-653) ([#82](https://github.com/caura-ai/caura-memclaw/issues/82)) ([838a54d](https://github.com/caura-ai/caura-memclaw/commit/838a54d4225d03bb618abded8f5bfb33ded8a4c9))
* crystallize + entity-link Pub/Sub fanout (CAURA-657) ([#87](https://github.com/caura-ai/caura-memclaw/issues/87)) ([966f1a5](https://github.com/caura-ai/caura-memclaw/commit/966f1a54091f15f1267ecf25ee6206db6cd73232))
* lifecycle ops Pub/Sub fanout via core-operations (CAURA-655) ([#84](https://github.com/caura-ai/caura-memclaw/issues/84)) ([193b0a7](https://github.com/caura-ai/caura-memclaw/commit/193b0a7e178ac478d01e1667d7a0d3e31b3ce9f5))
* **mcp:** add memclaw_stats — aggregate memory counts on MCP ([#64](https://github.com/caura-ai/caura-memclaw/issues/64)) ([cb6d29a](https://github.com/caura-ai/caura-memclaw/commit/cb6d29a6149e689097b88af34ff495e6ba4edf3a))
* memory retention purge on top of lifecycle Pub/Sub fanout (CAURA-656) ([#86](https://github.com/caura-ai/caura-memclaw/issues/86)) ([7e05ee4](https://github.com/caura-ai/caura-memclaw/commit/7e05ee426b8c5b8cf0b6f1df6b882f1607c88253))
* skills as documents (Phase B) — drop dedicated tools/routes ([#85](https://github.com/caura-ai/caura-memclaw/issues/85)) ([d2bdf3b](https://github.com/caura-ai/caura-memclaw/commit/d2bdf3b3a3aa206e8975bbade45d965dab88f75d))
* **skills:** agent-to-agent skill sharing across the fleet ([#68](https://github.com/caura-ai/caura-memclaw/issues/68)) ([e95b1d0](https://github.com/caura-ai/caura-memclaw/commit/e95b1d091808b1f221dab747b8893ff8cdc9229d))


### Bug Fixes

* **core-worker:** bundle Vertex SDK + improve retry log (CAURA-648) ([#78](https://github.com/caura-ai/caura-memclaw/issues/78)) ([85e54af](https://github.com/caura-ai/caura-memclaw/commit/85e54af22eb67861f34b12ba1ad3f1f69e9943d8))
* **core-worker:** init platform LLM singleton on lifespan startup (CAURA-647) ([#74](https://github.com/caura-ai/caura-memclaw/issues/74)) ([8cc74dd](https://github.com/caura-ai/caura-memclaw/commit/8cc74dd99924cf8de53233e4fcbc49787241a598))
* **crystallizer:** /latest 404→200 null on empty (CAURA-646) ([#71](https://github.com/caura-ai/caura-memclaw/issues/71)) ([a8a63c9](https://github.com/caura-ai/caura-memclaw/commit/a8a63c9787bd00a49181d0c252e7e55a71066d99))
* **deps:** bump google-cloud-aiplatform&gt;=1.80, drop standalone vertexai (CAURA-650) ([#80](https://github.com/caura-ai/caura-memclaw/issues/80)) ([74c2092](https://github.com/caura-ai/caura-memclaw/commit/74c209240e4a12b8d58eaee2df5fb501685f7e5e))
* **plugin-install:** fetch openclaw.plugin.json instead of baking a HEREDOC ([#81](https://github.com/caura-ai/caura-memclaw/issues/81)) ([aedbd47](https://github.com/caura-ai/caura-memclaw/commit/aedbd474e8fd4795d001be1a79c7ef9104fdd17d))
* **plugin:** auto-fill target_fleet_id on skill share/unshare ([#72](https://github.com/caura-ai/caura-memclaw/issues/72)) ([35d7202](https://github.com/caura-ai/caura-memclaw/commit/35d7202c4026f63ca29a5119a5cd6c7230803d88))
* **plugin:** make install_on_fleet skill flow actually work end-to-end ([#77](https://github.com/caura-ai/caura-memclaw/issues/77)) ([97da9fa](https://github.com/caura-ai/caura-memclaw/commit/97da9fafaa12d389969ea71fbc8de9ae4adce2ce))
* **scripts:** backfill_embeddings — str-cast + add openai to core-storage-api deps ([#65](https://github.com/caura-ai/caura-memclaw/issues/65)) ([e77a256](https://github.com/caura-ai/caura-memclaw/commit/e77a2560bff363481727ca7c635b33a8d3f9e29e))
* **vertex:** raise typed error on non-dict JSON response (CAURA-651) ([#90](https://github.com/caura-ai/caura-memclaw/issues/90)) ([ece0081](https://github.com/caura-ai/caura-memclaw/commit/ece00819d209e62ab7cd49cbba2da21aeb6b418c))


### Reverts

* remove security.session_idle_timeout_minutes from OSS organization_settings (CAURA-660) ([#91](https://github.com/caura-ai/caura-memclaw/issues/91)) ([094d785](https://github.com/caura-ai/caura-memclaw/commit/094d785410857961295e383e54413340bae4cfb0))


### Documentation

* complete local-embedder coverage (P0 fixes, GPU, README integration) ([#70](https://github.com/caura-ai/caura-memclaw/issues/70)) ([80fadf2](https://github.com/caura-ai/caura-memclaw/commit/80fadf20d8ff125e88161542521978a0407c3e7f))


### Code Refactoring

* rename tenant_settings → organization_settings (CAURA-654) ([#83](https://github.com/caura-ai/caura-memclaw/issues/83)) ([9494a53](https://github.com/caura-ai/caura-memclaw/commit/9494a53819eae6fc6b45fec9308facfdd192fb9a))

## [2.0.0](https://github.com/caura-ai/caura-memclaw/compare/v1.0.1...v2.0.0) (2026-05-03)

> ⚠️ **BREAKING CHANGE — local embedder + 1024-dim schema migration.**
> v2.0.0 introduces a self-hosted embedder profile (`BAAI/bge-m3` via HuggingFace
> TEI sidecar — see [`docs/local-embedder.md`](docs/local-embedder.md)) and
> migrates the pgvector schema from 768-dim to 1024-dim (alembic
> `012_vector_dim_1024`). Existing installations must opt in via
> `MEMCLAW_RUN_DESTRUCTIVE_MIGRATIONS=true` and re-embed afterward — see
> [README "Upgrading from v1.x"](README.md#upgrading-from-v1x) for the
> procedure. Fresh installs are unaffected.


### Features

* **agents:** split agent_id (opaque, stable) from display_name + plugin install_id (CAURA-000) ([#55](https://github.com/caura-ai/caura-memclaw/issues/55)) ([edc79e3](https://github.com/caura-ai/caura-memclaw/commit/edc79e3b7eff6a5040259b49661fd25e93ee7c22))
* **enrichment:** consolidate memory-type vocabulary as single source of truth (CAURA-000) ([#60](https://github.com/caura-ai/caura-memclaw/issues/60)) ([223601a](https://github.com/caura-ai/caura-memclaw/commit/223601a49c9d87344b06f3a162d05061d64ef7ed))
* **errors:** standardize error contract across REST and MCP ([#58](https://github.com/caura-ai/caura-memclaw/issues/58)) ([6d33caf](https://github.com/caura-ai/caura-memclaw/commit/6d33caf686a207e7cafdff8ce8f77e7ba6614e26))
* **events:** EVENT_BUS_PUBSUB_MAX_MESSAGES override (CAURA-636) — v2 ([#48](https://github.com/caura-ai/caura-memclaw/issues/48)) ([5fb90a1](https://github.com/caura-ai/caura-memclaw/commit/5fb90a175971d4b1113c13686114cf0503e7205b))
* **events:** EVENT_BUS_PUBSUB_MAX_MESSAGES override (CAURA-636) ([#46](https://github.com/caura-ai/caura-memclaw/issues/46)) ([fb0104a](https://github.com/caura-ai/caura-memclaw/commit/fb0104a71a805471f1cbe2eb4046fe23f602ca1a))
* **infra:** docker-compose pulls published images by default ([#28](https://github.com/caura-ai/caura-memclaw/issues/28)) ([3d30d97](https://github.com/caura-ai/caura-memclaw/commit/3d30d97b3044430377825ba8c3ae7e8727a6ecbd))
* **scripts:** preflight_012 — DB readout before migration 012 ([#59](https://github.com/caura-ai/caura-memclaw/issues/59)) ([049ee0c](https://github.com/caura-ai/caura-memclaw/commit/049ee0cc3916669240b367babaf38b3f27f9a88b))
* standardize on POSTGRES_* env vars + README/CONTRIBUTING pre-launch cleanup ([#26](https://github.com/caura-ai/caura-memclaw/issues/26)) ([8a9d017](https://github.com/caura-ai/caura-memclaw/commit/8a9d017edb6a8873e3c3cb825e31738b68aa21d9))
* **stats:** real public counters + /api/v1/status + trust soft-pass (CAURA-000) ([#54](https://github.com/caura-ai/caura-memclaw/issues/54)) ([075aefc](https://github.com/caura-ai/caura-memclaw/commit/075aefc28219d35a2a231944aae7ebd855eb5c64))


### Bug Fixes

* **api:** include visibility in GET /memories/{memory_id} response ([#40](https://github.com/caura-ai/caura-memclaw/issues/40)) ([9562a79](https://github.com/caura-ai/caura-memclaw/commit/9562a799411264e71d64d5669a196486d46bbc5b))
* **audit:** batch + async ingest via in-memory queue (CAURA-628) ([#36](https://github.com/caura-ai/caura-memclaw/issues/36)) ([404f064](https://github.com/caura-ai/caura-memclaw/commit/404f0640bb3fbd40263e03a2f9bc6bd3ad7ac9bb))
* **audit:** per-tenant slot in batch flusher to stop bulk_write contention (CAURA-631) ([#44](https://github.com/caura-ai/caura-memclaw/issues/44)) ([dde839a](https://github.com/caura-ai/caura-memclaw/commit/dde839a4f4a8cb84e8ca11cec220300498556303))
* **bulk:** per-attempt idempotency + 207 Multi-Status (CAURA-602) ([#23](https://github.com/caura-ai/caura-memclaw/issues/23)) ([d2e7c1c](https://github.com/caura-ai/caura-memclaw/commit/d2e7c1c95e60eb67d228bf5b08c5ede241e7af15))
* **contradiction:** preserve direction invariant on re-fired detection ([#41](https://github.com/caura-ai/caura-memclaw/issues/41)) ([78a993b](https://github.com/caura-ai/caura-memclaw/commit/78a993bf12e415fe14bd8aeccc483ca840cc6c49))
* **contradiction:** scope candidate fetch by writer visibility ([#38](https://github.com/caura-ai/caura-memclaw/issues/38)) ([6e31348](https://github.com/caura-ai/caura-memclaw/commit/6e31348203377af2b6a54418c4149371604c312d))
* **core-storage:** exclude soft-deleted from public /stats counters ([#27](https://github.com/caura-ai/caura-memclaw/issues/27)) ([f1581a8](https://github.com/caura-ai/caura-memclaw/commit/f1581a821bdb310ec735f38398c1e26cf7ef90c2))
* **idempotency:** close concurrent-claim race with pending-row pattern ([#32](https://github.com/caura-ai/caura-memclaw/issues/32)) ([a58a719](https://github.com/caura-ai/caura-memclaw/commit/a58a7195b701b5191dfff405b8e33f9ff0a6f5e2))
* **infra:** point docker-compose.dev at per-service Dockerfiles (CAURA-111) ([#37](https://github.com/caura-ai/caura-memclaw/issues/37)) ([b093536](https://github.com/caura-ai/caura-memclaw/commit/b0935368624a41e126b8bda4d934f141b26a7f8f))
* **llm,embedding:** explicit httpx.Limits on OpenAI-compatible providers (CAURA-627) ([#34](https://github.com/caura-ai/caura-memclaw/issues/34)) ([1e2e799](https://github.com/caura-ai/caura-memclaw/commit/1e2e799fbeb05117e33998f340b1c320c5925779))
* **logging:** route uvicorn / fastmcp / mcp / slowapi to root formatter (CAURA-588) ([#32](https://github.com/caura-ai/caura-memclaw/issues/32)) ([f4cc494](https://github.com/caura-ai/caura-memclaw/commit/f4cc4942676e0cca409c9c1aa6eda6c65a537e2d))
* **memories:** apply visibility scoping to memory count endpoints (CAURA-000) ([#42](https://github.com/caura-ai/caura-memclaw/issues/42)) ([6e26dcb](https://github.com/caura-ai/caura-memclaw/commit/6e26dcb360d5dcce91877f59a6aaf7fe4e4cd9c6))
* **memories:** per-phase storage timeout for bulk-create (CAURA-599) ([#33](https://github.com/caura-ai/caura-memclaw/issues/33)) ([6ac1ca5](https://github.com/caura-ai/caura-memclaw/commit/6ac1ca5f4b868925ebfe7cd1244320e1d0f2f07d))
* **memories:** wire GET /memories/:id/contradictions to detector results (CAURA-604) ([#31](https://github.com/caura-ai/caura-memclaw/issues/31)) ([85b4a54](https://github.com/caura-ai/caura-memclaw/commit/85b4a54794d3afc86bb37c6ac49022d1c0fee08c))
* **memory:** drop unused unscoped indexes ix_memories_status + ix_memories_visibility (CAURA-629) ([#35](https://github.com/caura-ai/caura-memclaw/issues/35)) ([9b2e071](https://github.com/caura-ai/caura-memclaw/commit/9b2e07111d38d7f9b3d6e1a50102658e986a73c1))
* **memory:** restore ix_memories_status + ix_memories_visibility (CAURA-632) ([#45](https://github.com/caura-ai/caura-memclaw/issues/45)) ([4be9017](https://github.com/caura-ai/caura-memclaw/commit/4be90175ebb520b6752d6b23642b3e05b92a2c10))
* **noisy-neighbor:** per-tenant asyncio.Semaphore on /search + writes ([#33](https://github.com/caura-ai/caura-memclaw/issues/33)) ([1ce87b5](https://github.com/caura-ai/caura-memclaw/commit/1ce87b5d1e95d825178c8bd0fc98c5ae72d430b4))
* **noisy-neighbor:** per-tenant storage-roundtrip bulkhead (draft, gated on [#23](https://github.com/caura-ai/caura-memclaw/issues/23) loadtest) ([#24](https://github.com/caura-ai/caura-memclaw/issues/24)) ([f934d73](https://github.com/caura-ai/caura-memclaw/commit/f934d735e0f0281c339f00e2ea9666e58f537b2a))
* **noisy-neighbor:** widen per-tenant Semaphore caps so bench loop fits ([#30](https://github.com/caura-ai/caura-memclaw/issues/30)) ([f52d4b5](https://github.com/caura-ai/caura-memclaw/commit/f52d4b57f99f66a41139d664addf3a58bcfac454))
* **search:** drop UUID/hex fragments before entity FTS roundtrip ([#29](https://github.com/caura-ai/caura-memclaw/issues/29)) ([320d9ac](https://github.com/caura-ai/caura-memclaw/commit/320d9accc7219a0e4eaab2f2c5894033b6e8f1cc))
* **stats:** defensive fallback to storage-api on DB pool exhaustion ([#31](https://github.com/caura-ai/caura-memclaw/issues/31)) ([293bd42](https://github.com/caura-ai/caura-memclaw/commit/293bd422b00e5f2aa9a32cd2a0071e0cb5652db3))
* **storage:** align core-storage-api DB pool defaults with platform-storage (5+5) ([#25](https://github.com/caura-ai/caura-memclaw/issues/25)) ([061155e](https://github.com/caura-ai/caura-memclaw/commit/061155e4773a0359bac8e72a668aa96bff2e1fb3))
* **storage:** unblock migrations that use `autocommit_block` ([#27](https://github.com/caura-ai/caura-memclaw/issues/27)) ([835e1a7](https://github.com/caura-ai/caura-memclaw/commit/835e1a78e950c71c44e8ddd2d11a2eeb003dc60f))
* **worker:** per-tenant storage slot to stop noisy-neighbor PATCH-back contention (CAURA-636) ([#53](https://github.com/caura-ai/caura-memclaw/issues/53)) ([98ae9d9](https://github.com/caura-ai/caura-memclaw/commit/98ae9d9aa27d2c29b379e81840344128180cb0e9))
* **write:** str() subject_entity_id before storage POST ([#39](https://github.com/caura-ai/caura-memclaw/issues/39)) ([7b232f5](https://github.com/caura-ai/caura-memclaw/commit/7b232f5acab1a0cda9307eb23f42c5207f1e4205))


### Documentation

* add Discord community link to README + SUPPORT ([#29](https://github.com/caura-ai/caura-memclaw/issues/29)) ([45e55bf](https://github.com/caura-ai/caura-memclaw/commit/45e55bf9df57dc99237dd408d338c206b1ddf193))

## [1.0.0] - 2026-04-26

Initial public release. The public API surface and stability scope are
declared in [README.md § Public API & Stability](README.md#public-api--stability).
