# Changelog

All notable changes to MemClaw are documented in this file. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Subsequent releases are produced by [release-please](https://github.com/googleapis/release-please-action)
from [Conventional Commits](https://www.conventionalcommits.org/).

## [2.17.0](https://github.com/caura-ai/caura-memclaw/compare/backend-v2.16.0...backend-v2.17.0) (2026-06-24)


### Features

* **audit:** tamper-evident per-tenant hash chain for audit_log (eToro governance) ([#397](https://github.com/caura-ai/caura-memclaw/issues/397)) ([baaa493](https://github.com/caura-ai/caura-memclaw/commit/baaa493cdc8a2bf71d031b0f457844d389c8f6c4))
* **core-api:** cross-worker settings-cache invalidation (CAURA-571) ([#418](https://github.com/caura-ai/caura-memclaw/issues/418)) ([86bbe9e](https://github.com/caura-ai/caura-memclaw/commit/86bbe9ec0aa061741df0422bdc5b8dec286fa654))
* **core-api:** fast business/personal pre-gate before enrichment (opt-in) ([#403](https://github.com/caura-ai/caura-memclaw/issues/403)) ([095c360](https://github.com/caura-ai/caura-memclaw/commit/095c36043ebeeb1c13e7cde79895bfda6aa324f6))
* **core-api:** install-skill ?skill= selector + company-brain import path ([#483](https://github.com/caura-ai/caura-memclaw/issues/483)) ([a2a764e](https://github.com/caura-ai/caura-memclaw/commit/a2a764e83f6f42d5bf8122adebecb86227170081))
* **core-api:** route documents + fleet residuals through core-storage-api (Fix 2 Phase 3) ([#431](https://github.com/caura-ai/caura-memclaw/issues/431)) ([ad2a2d9](https://github.com/caura-ai/caura-memclaw/commit/ad2a2d9f91d6802a8a66d8935befef39cafa9346))
* **core-api:** route mcp_server's 9 ready tools through core-storage-api (Fix 2 Phase 4) ([#432](https://github.com/caura-ai/caura-memclaw/issues/432)) ([dc0b674](https://github.com/caura-ai/caura-memclaw/commit/dc0b674287b7fd523a598b1277e9a4bbf2d40ed5))
* **core-api:** route memory REST surface through core-storage-api (Fix 2 Phase 2) ([#429](https://github.com/caura-ai/caura-memclaw/issues/429)) ([1e39c59](https://github.com/caura-ai/caura-memclaw/commit/1e39c59a0d2366023e4065c46926deb94b13c247))
* **core-api:** route organization_settings through core-storage-api (Fix 2 Phase 0) ([#427](https://github.com/caura-ai/caura-memclaw/issues/427)) ([4450ec0](https://github.com/caura-ai/caura-memclaw/commit/4450ec0cbcdd3e297bdc350d5ec74f05b9cc1583))
* **core-api:** route tenant-discovery through core-storage-api (Fix 2 Phase 1) ([#428](https://github.com/caura-ai/caura-memclaw/issues/428)) ([f05f498](https://github.com/caura-ai/caura-memclaw/commit/f05f4982d70449db5de6e21c1239ff57c53e1562))
* **core-storage-api:** crystallizer entity-coverage + audit-usage reads (Fix 2 final-cleanup PR3a) ([#486](https://github.com/caura-ai/caura-memclaw/issues/486)) ([0737b6a](https://github.com/caura-ai/caura-memclaw/commit/0737b6a3d134dd03f3cf3ea316011d9eee4dc2cf))
* **core-storage-api:** recall/capability/ingest endpoints for Fix-2 final cleanup (PR1) ([#484](https://github.com/caura-ai/caura-memclaw/issues/484)) ([d8b18d0](https://github.com/caura-ai/caura-memclaw/commit/d8b18d0893ba2e3400630d9ded328b87589c2410))
* **core-storage-api:** route entity-linking pipeline through storage (Fix 2 Ph6) ([#477](https://github.com/caura-ai/caura-memclaw/issues/477)) ([74a18d7](https://github.com/caura-ai/caura-memclaw/commit/74a18d7c7ecbfdf643929e78c08beeb54d461d39))
* **core-storage-api:** route evolve through storage + delete _mcp_session (Fix 2 Ph5b, PR2) ([#472](https://github.com/caura-ai/caura-memclaw/issues/472)) ([44743e1](https://github.com/caura-ai/caura-memclaw/commit/44743e19b1972a4439b9f43c8f7919df282363e0))
* **core-storage-api:** route insights_service through storage (Fix 2 Ph5b, PR1 — insights) ([#471](https://github.com/caura-ai/caura-memclaw/issues/471)) ([2655b84](https://github.com/caura-ai/caura-memclaw/commit/2655b84a91624b0acf38be564181b3c8dd06e866))
* **core-storage-api:** route the skill-factory pipeline through storage (Fix 2 Ph5a) ([#470](https://github.com/caura-ai/caura-memclaw/issues/470)) ([81e79be](https://github.com/caura-ai/caura-memclaw/commit/81e79be5bd64e047c229b955d8717e8e7de04af8))
* **governance:** ingestion-boundary PII + business/personal gate (eToro) ([#398](https://github.com/caura-ai/caura-memclaw/issues/398)) ([12860af](https://github.com/caura-ai/caura-memclaw/commit/12860af951539ea2b097f2f2dd9ee7615b1c9464))
* **plugin:** additive-mode skill reconcile with ownership marker (PR2) ([#417](https://github.com/caura-ai/caura-memclaw/issues/417)) ([3d970ef](https://github.com/caura-ai/caura-memclaw/commit/3d970ef2cbd26c4ab3830101bb4ae313409545bd))
* **plugin:** auto-register additive skill dirs on OpenClaw's load path (PR3b) ([#425](https://github.com/caura-ai/caura-memclaw/issues/425)) ([819a77f](https://github.com/caura-ai/caura-memclaw/commit/819a77f9110352ad5008c7d1160a60a43b5451fa))
* **plugin:** per-target reconcile observability (PR3a) ([#424](https://github.com/caura-ai/caura-memclaw/issues/424)) ([04120f2](https://github.com/caura-ai/caura-memclaw/commit/04120f241f6083615dcab2de49fca661d692c334))
* **recall:** opt-in logging of agent-chosen recalls ([#480](https://github.com/caura-ai/caura-memclaw/issues/480)) ([953ec33](https://github.com/caura-ai/caura-memclaw/commit/953ec336c052f88a9f630394c6dfac0e68d0d389))
* **skills:** refresh canonical + plugin memclaw skills, add company-brain ([#482](https://github.com/caura-ai/caura-memclaw/issues/482)) ([aa03c03](https://github.com/caura-ai/caura-memclaw/commit/aa03c03c47aaeef4da3390d2b810ae3b46045bfd))


### Bug Fixes

* **audit:** idempotent bulk flush via client_event_id — stop dropping audit rows ([#407](https://github.com/caura-ai/caura-memclaw/issues/407)) ([bf87e64](https://github.com/caura-ai/caura-memclaw/commit/bf87e64146303865c75cdbb65c08a7c95cbd4afb))
* **core-api:** cut prod connect-timeout + rate-limit-Redis log noise ([#426](https://github.com/caura-ai/caura-memclaw/issues/426)) ([a082f3f](https://github.com/caura-ai/caura-memclaw/commit/a082f3fa470eb7503f7f7c39961af657a7610180))
* **core-api:** harden business/personal pre-gate (timeout, audit truth, fail-closed, audit durability) ([#416](https://github.com/caura-ai/caura-memclaw/issues/416)) ([aa4be8e](https://github.com/caura-ai/caura-memclaw/commit/aa4be8e913da5f8d643ca1f98d6d9aaa0f821931))
* **core-api:** keep-alive pool + bounded entity-context fan-out (VPC-connector ConnectTimeout residual) ([#434](https://github.com/caura-ai/caura-memclaw/issues/434)) ([0ba51b4](https://github.com/caura-ai/caura-memclaw/commit/0ba51b43d7bbaa8b52b97eee7ec6dcf7f05c3331))
* **core-api:** per-tenant embed concurrency cap (noisy-neighbor-search) ([#464](https://github.com/caura-ai/caura-memclaw/issues/464)) ([043c02e](https://github.com/caura-ai/caura-memclaw/commit/043c02e5df4a109881dabf902c1969371cf3b08e))
* **core-api:** re-route third-party loggers from the ASGI lifespan startup ([#473](https://github.com/caura-ai/caura-memclaw/issues/473)) ([8f8f39e](https://github.com/caura-ai/caura-memclaw/commit/8f8f39ef85360e37c1aca510feab770de9b926fe))
* **core-api:** resolve home fleet on omitted fleet_id for MCP writes ([#465](https://github.com/caura-ai/caura-memclaw/issues/465)) ([56db1f5](https://github.com/caura-ai/caura-memclaw/commit/56db1f5fadb7e3abc24cf2ef6aec7df3d35abd70))
* **core-api:** route recall/capability writes through storage (Fix-2 cleanup PR2) ([#485](https://github.com/caura-ai/caura-memclaw/issues/485)) ([592b231](https://github.com/caura-ai/caura-memclaw/commit/592b231d395ff8787c9ad82c91a4b5262432b95d))
* **core-api:** self-heal storage pool + cancellation-safe requests (CAURA-000) ([#415](https://github.com/caura-ai/caura-memclaw/issues/415)) ([5889e3f](https://github.com/caura-ai/caura-memclaw/commit/5889e3facbba3f66b517644ff3d011ea226960f9))
* **core-api:** stamp version into image so /api/v1/version stops serving "dev" (CAURA-000) ([#414](https://github.com/caura-ai/caura-memclaw/issues/414)) ([4d1c486](https://github.com/caura-ai/caura-memclaw/commit/4d1c486bf7c28fddcc0cad2d93cd53589460831a))
* **core-api:** surface write-pipeline step failures instead of masking as KeyError ([#474](https://github.com/caura-ai/caura-memclaw/issues/474)) ([5d359b2](https://github.com/caura-ai/caura-memclaw/commit/5d359b291a7340383bac95e8dd81e908c94bf476))
* **core-storage-api:** 404 (not silent 200) on wrong-tenant embedding write ([#468](https://github.com/caura-ai/caura-memclaw/issues/468)) ([bec8229](https://github.com/caura-ai/caura-memclaw/commit/bec8229785741b95424718a5fd38a387bbf8c373))
* **core-storage-api:** 422 on missing/non-list embedding in /entities/set-embeddings ([#478](https://github.com/caura-ai/caura-memclaw/issues/478)) ([6268725](https://github.com/caura-ai/caura-memclaw/commit/62687253000a1f0fbb37206c9f538aa7f3bdc270))
* **core-storage-api:** followers skip the migration lock once schema is at head ([#487](https://github.com/caura-ai/caura-memclaw/issues/487)) ([8a7ac37](https://github.com/caura-ai/caura-memclaw/commit/8a7ac3758347e04960e942dc81e907a425df7780))
* **core-storage-api:** tenant-guard by-id memory UPDATEs ([#467](https://github.com/caura-ai/caura-memclaw/issues/467)) ([4248b4d](https://github.com/caura-ai/caura-memclaw/commit/4248b4db51f5be46d47260fb5b6cc34da075fd2e))
* **extraction:** re-fold split-off subject discriminators (A33) ([#412](https://github.com/caura-ai/caura-memclaw/issues/412)) ([ab0d2d6](https://github.com/caura-ai/caura-memclaw/commit/ab0d2d6b126841f82af397d5b64d84d9f98c960c))
* **http-retry:** widen connection-phase retry to ride out storage cold starts ([#406](https://github.com/caura-ai/caura-memclaw/issues/406)) ([5bbede7](https://github.com/caura-ai/caura-memclaw/commit/5bbede7481032397ec50400f166f12f132b5fc4a))
* **logging:** demote handler-less reroute 'no-op' to DEBUG (records aren't lost) ([#488](https://github.com/caura-ai/caura-memclaw/issues/488)) ([9bdbe44](https://github.com/caura-ai/caura-memclaw/commit/9bdbe44f05aa3b2e90e309b66cdf8baddad5b791))
* **prompts:** stop brace-escaping content before str.format ([#404](https://github.com/caura-ai/caura-memclaw/issues/404)) ([ba94a54](https://github.com/caura-ai/caura-memclaw/commit/ba94a54b2ade8641fa58cbf447db2759cc550604))
* **rate-limit:** fail open on Redis outage instead of 500 (view_rate_limit AttributeError) ([#410](https://github.com/caura-ai/caura-memclaw/issues/410)) ([53b467d](https://github.com/caura-ai/caura-memclaw/commit/53b467d2c622da7d339d7c390624f37aacf9c427))
* **recall:** tighten recall-brief grounding to stop invented details ([#489](https://github.com/caura-ai/caura-memclaw/issues/489)) ([6794f3e](https://github.com/caura-ai/caura-memclaw/commit/6794f3e60d177cd7d60058eba7b7641326bf4b79))
* **search:** dampen recall_boost so it can't hijack rankings (A26) ([#411](https://github.com/caura-ai/caura-memclaw/issues/411)) ([1de219c](https://github.com/caura-ai/caura-memclaw/commit/1de219c8a4078f971102dcb6c82fc10028e025f0))
* **search:** rank entity-lookup fan-out cap by query-match count (A30) ([#409](https://github.com/caura-ai/caura-memclaw/issues/409)) ([8f809c0](https://github.com/caura-ai/caura-memclaw/commit/8f809c0914e83271a108c5cfeba53d794a664c6b))
* **storage:** harden migration advisory-lock so a slow migration can't crash booting writers ([#408](https://github.com/caura-ai/caura-memclaw/issues/408)) ([54cbe1a](https://github.com/caura-ai/caura-memclaw/commit/54cbe1acd1e48431b3974c828a47d0a86ce597d1))


### Performance

* **core-api:** make document-ingest (kreuzberg) an opt-in extra to cut cold start ([#469](https://github.com/caura-ai/caura-memclaw/issues/469)) ([1f082d8](https://github.com/caura-ai/caura-memclaw/commit/1f082d8783a0f4de03cd629b79d66e9782026172))


### Documentation

* **core-api:** clarify memclaw_recall top_k is soft-capped, not rejected ([#481](https://github.com/caura-ai/caura-memclaw/issues/481)) ([6938f08](https://github.com/caura-ai/caura-memclaw/commit/6938f08fd52dd9a9c9efdb3fba42f7b20c4b65d0))
* **core-api:** document LLM free-form PII recall limit (recommend a capable enrichment model) ([#419](https://github.com/caura-ai/caura-memclaw/issues/419)) ([36b1b30](https://github.com/caura-ai/caura-memclaw/commit/36b1b30ad92deb987aac0427b068c0250d4f66f7))
* point all memclaw.dev links to memclaw.net ([#475](https://github.com/caura-ai/caura-memclaw/issues/475)) ([a6f9ad1](https://github.com/caura-ai/caura-memclaw/commit/a6f9ad1581bd30c60a125de1159ffe3918bf3222))


### Code Refactoring

* **core-storage-api:** consolidate router validation guards into _validation ([#479](https://github.com/caura-ai/caura-memclaw/issues/479)) ([e9dd9ca](https://github.com/caura-ai/caura-memclaw/commit/e9dd9caa18c291630f7f2d39af9a6cddc546088f))
* **plugin:** configurable skill-reconcile targets (PR1 — refactor + config plumbing) ([#413](https://github.com/caura-ai/caura-memclaw/issues/413)) ([57e5e96](https://github.com/caura-ai/caura-memclaw/commit/57e5e962cbc5a0f95555e0bfe762e5a3ae309b49))

## [2.16.0](https://github.com/caura-ai/caura-memclaw/compare/backend-v2.15.0...backend-v2.16.0) (2026-06-15)


### Features

* **core-api:** capability-usage adoption counters (MCP + REST) ([#353](https://github.com/caura-ai/caura-memclaw/issues/353)) ([0421ceb](https://github.com/caura-ai/caura-memclaw/commit/0421ceb0addadd71833f2ddce42cf973edd9fd75))


### Bug Fixes

* **compose:** stop shadowing .env provider keys with empty "${VAR:-}" passthroughs ([#346](https://github.com/caura-ai/caura-memclaw/issues/346)) ([8ab95d8](https://github.com/caura-ai/caura-memclaw/commit/8ab95d8e416862153410942bdf1bfcf51fcad2c1))
* **core-api:** make the CAURA-602 startup guard FastAPI-0.137-proof ([#364](https://github.com/caura-ai/caura-memclaw/issues/364)) ([65de110](https://github.com/caura-ai/caura-memclaw/commit/65de110ac996fe7bd79c5823b92e3852934cc112))
* **deps:** cap fastapi &lt;0.137 to unbreak CI ([#359](https://github.com/caura-ai/caura-memclaw/issues/359)) ([561d5c9](https://github.com/caura-ai/caura-memclaw/commit/561d5c96fee6be698cc0d2077a7e80f0aadbbb0c))
* **fleet:** cap auto-upgrade re-queue with a per-(node,target) attempt budget (CAURA-000) ([#351](https://github.com/caura-ai/caura-memclaw/issues/351)) ([22a3ac0](https://github.com/caura-ai/caura-memclaw/commit/22a3ac064ecafc912180767890399a2ce93ac531))
* **keystones:** let the standalone operator author keystones out-of-the-box (F-7) ([#354](https://github.com/caura-ai/caura-memclaw/issues/354)) ([d62c36a](https://github.com/caura-ai/caura-memclaw/commit/d62c36a9fa900426d70276f0950de318d1bdffb1))
* **plugin:** send X-Bulk-Attempt-Id header on bulk memory writes ([#355](https://github.com/caura-ai/caura-memclaw/issues/355)) ([a4515ed](https://github.com/caura-ai/caura-memclaw/commit/a4515ed1e5f5f7679daea36c99e58d1d331b3934))
* **search:** return raw cosine in similarity, not the ranking composite (F-14) ([#392](https://github.com/caura-ai/caura-memclaw/issues/392)) ([3865600](https://github.com/caura-ai/caura-memclaw/commit/3865600ce73a9273e4fd5db4f0b218f454b99422))
* **search:** surface conflicted memories that exactly match the query ([#357](https://github.com/caura-ai/caura-memclaw/issues/357)) ([c00a3f7](https://github.com/caura-ai/caura-memclaw/commit/c00a3f76f6f390024567f6d23acef062f304dca9))


### Documentation

* add top-level BENCHMARKS.md as a citable benchmark asset ([#349](https://github.com/caura-ai/caura-memclaw/issues/349)) ([fc6d563](https://github.com/caura-ai/caura-memclaw/commit/fc6d563b171264fc2bc1578a708fc9ad3104cc4a))
* comparison table — "PII detection & flagging", not "quarantine" ([#345](https://github.com/caura-ai/caura-memclaw/issues/345)) ([3bb7b1c](https://github.com/caura-ai/caura-memclaw/commit/3bb7b1c8903e90b77630324222956eb9d83fc4b9))
* **core-api:** document the redis&lt;9 cap rationale (review follow-up) ([#395](https://github.com/caura-ai/caura-memclaw/issues/395)) ([cd5420c](https://github.com/caura-ai/caura-memclaw/commit/cd5420c255594d3f0e80798b65847d11d51db17d))
* lead Quick Start with a no-key, no-signup local quickstart ([#348](https://github.com/caura-ai/caura-memclaw/issues/348)) ([91ee613](https://github.com/caura-ai/caura-memclaw/commit/91ee613c46cdf9e0f81c7249ab2b3c75601c110a))
* README write-response field names match payload ([#361](https://github.com/caura-ai/caura-memclaw/issues/361)) ([2ecc098](https://github.com/caura-ai/caura-memclaw/commit/2ecc098f099c333655d922b08ea7bc03ca699c4f))


### Code Refactoring

* **api:** unify evolve/insights caller-identity resolution; close F-7 tail (§2) ([#400](https://github.com/caura-ai/caura-memclaw/issues/400)) ([f4af240](https://github.com/caura-ai/caura-memclaw/commit/f4af240917061bc568c8177ea9fcb8db004036a4))

## [2.15.0](https://github.com/caura-ai/caura-memclaw/compare/backend-v2.14.0...backend-v2.15.0) (2026-06-13)


### Features

* **skills:** gate the OpenClaw install path to active-only skills ([#317](https://github.com/caura-ai/caura-memclaw/issues/317)) ([50488fc](https://github.com/caura-ai/caura-memclaw/commit/50488fc6eb07d244297ad1d3e065887c8cb3e47e))
* **skills:** per-node skill-reconcile observability on the heartbeat ([#325](https://github.com/caura-ai/caura-memclaw/issues/325)) ([d806114](https://github.com/caura-ai/caura-memclaw/commit/d806114bc943a1d97480584192091e0583d41545))


### Bug Fixes

* **api:** close cross-tenant and caller-asserted-identity authz gaps ([#320](https://github.com/caura-ai/caura-memclaw/issues/320)) ([fcf96c7](https://github.com/caura-ai/caura-memclaw/commit/fcf96c74c28022415d1f691d3507c96ba8c99b22))
* **api:** default agent_id to "mcp-agent" on standalone POST /memories ([#339](https://github.com/caura-ai/caura-memclaw/issues/339)) ([4d2d6ef](https://github.com/caura-ai/caura-memclaw/commit/4d2d6ef008de5c743b848f566b51f461d117e06a))
* **core-worker:** retry connection-phase failures on storage calls — shared policy in common/http_retry ([#334](https://github.com/caura-ai/caura-memclaw/issues/334)) ([43b3d81](https://github.com/caura-ai/caura-memclaw/commit/43b3d8191b3567beba9cc6025f2df8e9aabb02de))
* **cross-links:** single multi-VALUES insert — executemany cannot RETURN rows ([#337](https://github.com/caura-ai/caura-memclaw/issues/337)) ([8bbdeed](https://github.com/caura-ai/caura-memclaw/commit/8bbdeedb84c5a85655a88485583dcabd7b21d9a6))
* cross-tenant search widening, non-default LLM structured-output kwargs, event deploy safety ([#322](https://github.com/caura-ai/caura-memclaw/issues/322)) ([f161607](https://github.com/caura-ai/caura-memclaw/commit/f1616070b72efbfba712de4095f6f556669368cc))
* **embedding:** fall back to default model when OPENAI_EMBEDDING_MODEL is empty ([#336](https://github.com/caura-ai/caura-memclaw/issues/336)) ([d1f1f5a](https://github.com/caura-ai/caura-memclaw/commit/d1f1f5a6f8ac4a631b4bb8dfa89801384e4aea44))
* **entities:** scope-filter relations in get_entity and drop the per-memory agent lookup ([#321](https://github.com/caura-ai/caura-memclaw/issues/321)) ([e355f87](https://github.com/caura-ai/caura-memclaw/commit/e355f87fba54ca45b634bf2affd4b9893a0e3528))
* **entity-linking:** bind reinforce weight directly — LEAST over untyped params coerced to text ([#341](https://github.com/caura-ai/caura-memclaw/issues/341)) ([b4a5712](https://github.com/caura-ai/caura-memclaw/commit/b4a57120107c2319013e8ac64aa6aaecc87d0751))
* **entity-linking:** synchronize_session=False on the entity-embedding backfill bulk update ([#342](https://github.com/caura-ai/caura-memclaw/issues/342)) ([c310ecf](https://github.com/caura-ai/caura-memclaw/commit/c310ecfd61ee14b0545234b7bb4773fbc0e1685e))
* **events:** publisher candidate cleanups also called the nonexistent close() ([#331](https://github.com/caura-ai/caura-memclaw/issues/331)) ([610fe8e](https://github.com/caura-ai/caura-memclaw/commit/610fe8e385c38237cf22276a02ef35e9345772d2))
* **events:** pubsub shutdown never flushed the publisher — final batches silently lost ([#330](https://github.com/caura-ai/caura-memclaw/issues/330)) ([1c2aba5](https://github.com/caura-ai/caura-memclaw/commit/1c2aba577a1a7cd4347faff3118d7179c39a7368))
* **llm:** explicit httpx per-phase timeouts — 5s connect too tight behind VPC connector ([#332](https://github.com/caura-ai/caura-memclaw/issues/332)) ([ea89a61](https://github.com/caura-ai/caura-memclaw/commit/ea89a61cb3b6b4331b92183dfe09f3bbfc24e879))
* **logging:** reroute idempotency guard mis-keys on captured handlers ([#328](https://github.com/caura-ai/caura-memclaw/issues/328)) ([352689d](https://github.com/caura-ai/caura-memclaw/commit/352689d3f18c2d00b4444cca62d80d1b4b340109))
* **mcp:** enforce gateway perimeter on the /mcp header-trust path ([#319](https://github.com/caura-ai/caura-memclaw/issues/319)) ([109d837](https://github.com/caura-ai/caura-memclaw/commit/109d837950550a577f4fcb8e585e76105b961d9b))
* **mcp:** memclaw_tune crashed with 'dict' object has no attribute 'id' ([#338](https://github.com/caura-ai/caura-memclaw/issues/338)) ([30e3cff](https://github.com/caura-ai/caura-memclaw/commit/30e3cff097c73d0af6faec739f638c5fecf46aef))
* **plugin:** point agents at the memclaw skill by name, not by filesystem path (CAURA-000) ([#323](https://github.com/caura-ai/caura-memclaw/issues/323)) ([ec3fcaf](https://github.com/caura-ai/caura-memclaw/commit/ec3fcaf45a7545568e9f713a6f1db631dfa1c2f3))
* **release:** keep plugin/src/version.ts in sync on release-please PRs ([#335](https://github.com/caura-ai/caura-memclaw/issues/335)) ([e26a3c5](https://github.com/caura-ai/caura-memclaw/commit/e26a3c561b542fbf58b4fd556ec2919e87286dfd))
* **search:** consume the inflight embedding future's exception when no joiner awaits it ([#340](https://github.com/caura-ai/caura-memclaw/issues/340)) ([a06ec53](https://github.com/caura-ai/caura-memclaw/commit/a06ec539449d89fc3d3f58e45c645ed3ca456ae1))
* **search:** skip entity hop-boost when classifier declined the entity match as over-broad ([#327](https://github.com/caura-ai/caura-memclaw/issues/327)) ([4744732](https://github.com/caura-ai/caura-memclaw/commit/4744732196c05fb1fd207f53f5bd03a849472f7d))
* **storage-client:** retry POSTs on connection-phase failures — never-sent requests cannot double-insert ([#333](https://github.com/caura-ai/caura-memclaw/issues/333)) ([2d91e33](https://github.com/caura-ai/caura-memclaw/commit/2d91e33baa968459443005fd5a86d127c6c0c4d1))


### Documentation

* add eToro proof, comparison table, FAQ, and star ask to README ([#326](https://github.com/caura-ai/caura-memclaw/issues/326)) ([e6395b5](https://github.com/caura-ai/caura-memclaw/commit/e6395b535f58fd92906138c83b45a5c40a67be89))
* bump eToro memory count 21,500 → 26,500 + social card ([#329](https://github.com/caura-ai/caura-memclaw/issues/329)) ([ea0e52b](https://github.com/caura-ai/caura-memclaw/commit/ea0e52b3fe43e72a279f365f38cd81301f1a6fce))
* correct Claude Code MCP setup — claude mcp add -s user, not settings.json ([#343](https://github.com/caura-ai/caura-memclaw/issues/343)) ([fc6324b](https://github.com/caura-ai/caura-memclaw/commit/fc6324b6c60206efe814c3e9cdfc42d0f85266f4))
* fix README/AGENT-INSTALL inaccuracies (PII, health shape, write response) ([#344](https://github.com/caura-ai/caura-memclaw/issues/344)) ([2dea567](https://github.com/caura-ai/caura-memclaw/commit/2dea567b802e9effaad3455284a7aab8e1cf124d))

## [2.14.0](https://github.com/caura-ai/caura-memclaw/compare/backend-v2.13.0...backend-v2.14.0) (2026-06-10)


### Features

* **skills:** auto-approve clean Forge candidates (skip HITL inbox) ([#313](https://github.com/caura-ai/caura-memclaw/issues/313)) ([6f3452d](https://github.com/caura-ai/caura-memclaw/commit/6f3452d8197083a25cc34489a1c39c3adafebc81))
* **skills:** MCP-direct delivery — agents discover only active skills ([#315](https://github.com/caura-ai/caura-memclaw/issues/315)) ([1ac4f1f](https://github.com/caura-ai/caura-memclaw/commit/1ac4f1f14a0af7384ebed9a6df696641b8d60627))
* **skills:** wire Forge into the lifecycle cron fanout ([#311](https://github.com/caura-ai/caura-memclaw/issues/311)) ([0523ed0](https://github.com/caura-ai/caura-memclaw/commit/0523ed0ab172942d7e25f9d54a29175ec2394ea1))


### Documentation

* replace concept SVG with rendered PNG diagram in README ([#316](https://github.com/caura-ai/caura-memclaw/issues/316)) ([f32ba1b](https://github.com/caura-ai/caura-memclaw/commit/f32ba1bd675d1465d089fcd3b7e4862e9ae0f981))

## [2.13.0](https://github.com/caura-ai/caura-memclaw/compare/backend-v2.12.1...backend-v2.13.0) (2026-06-08)


### Features

* **embedding:** let the platform singleton target a self-hosted endpoint ([#291](https://github.com/caura-ai/caura-memclaw/issues/291)) ([0991cef](https://github.com/caura-ai/caura-memclaw/commit/0991cef1c9aa001af6557d38ee7098e3b9f20b5a))
* **graph-build:** drop literal/attr entity nodes, block suffix-distinct merges, rerank entity_lookup by query overlap ([#304](https://github.com/caura-ai/caura-memclaw/issues/304)) ([f3a0f7c](https://github.com/caura-ai/caura-memclaw/commit/f3a0f7c7084d2eef4fbe8d0484d53b2997f527c3))
* **skills:** Skill Factory · Phases 0, 1, and 2 (partial — backend only) ([#293](https://github.com/caura-ai/caura-memclaw/issues/293)) ([f654ddb](https://github.com/caura-ai/caura-memclaw/commit/f654ddb9b2c8eb1956e8f9e73b75572ddf65f012))


### Bug Fixes

* **events:** drop cross-environment Pub/Sub fan-out messages ([#297](https://github.com/caura-ai/caura-memclaw/issues/297)) ([4f03679](https://github.com/caura-ai/caura-memclaw/commit/4f03679ef389c7c31e1ef6c3d31792881eccb600))
* **events:** env-scoped topic prefix for PubSubEventBus (no-op default) ([#295](https://github.com/caura-ai/caura-memclaw/issues/295)) ([0864274](https://github.com/caura-ai/caura-memclaw/commit/086427407b08df8475a29927882b01c5c76cb026))
* **evolve:** pass weight_adjustment_skipped_reason from MCP evolve path ([#299](https://github.com/caura-ai/caura-memclaw/issues/299)) ([b7d0f2f](https://github.com/caura-ai/caura-memclaw/commit/b7d0f2f864eee3e16f3df40c10b71c7f2e3c0dc9))
* **fleet:** bound deploy-queue runaway when a previous deploy is acked (CAURA-000) ([#305](https://github.com/caura-ai/caura-memclaw/issues/305)) ([a94d0ab](https://github.com/caura-ai/caura-memclaw/commit/a94d0ab0664c4a1f36c28713b929e8489a5103cc))
* **plugin:** bound resolveTenantId fetch with AbortSignal timeout (CAURA-000) ([#292](https://github.com/caura-ai/caura-memclaw/issues/292)) ([a3532a4](https://github.com/caura-ai/caura-memclaw/commit/a3532a433ab4ef5032304a2c125251c7c4bfdea5))
* **plugin:** don't create plugins.allow from nothing on autoFix (CAURA-000) ([#307](https://github.com/caura-ai/caura-memclaw/issues/307)) ([2aab828](https://github.com/caura-ai/caura-memclaw/commit/2aab828324ca054024ba075a2bbc682f749c6f8a))
* **plugin:** memoize bootstrap at process level (CAURA-000) ([#303](https://github.com/caura-ai/caura-memclaw/issues/303)) ([3bbb559](https://github.com/caura-ai/caura-memclaw/commit/3bbb559ea8cf397a0a49f3b00db37d5978e9b3df))
* **plugin:** schedule restart AFTER result POST resolves (CAURA-000) ([#306](https://github.com/caura-ai/caura-memclaw/issues/306)) ([fe8ad26](https://github.com/caura-ai/caura-memclaw/commit/fe8ad26df789c60f65d38ea64b2fff5a3f82f210))
* **plugin:** suppress bootstrap agent-id warn + swallow afterTurn 409 (CAURA-000) ([#300](https://github.com/caura-ai/caura-memclaw/issues/300)) ([6705315](https://github.com/caura-ai/caura-memclaw/commit/67053152428d55d2e93f9a552ddd0724d1a73393))
* return real columns from cross-link insert instead of nonexistent id ([#298](https://github.com/caura-ai/caura-memclaw/issues/298)) ([7ddb78b](https://github.com/caura-ai/caura-memclaw/commit/7ddb78b20de6a3f111e935f888a186ff1fa9b880))
* **search:** bound graph BFS frontier to prevent unbounded IN-clause (CAURA-000 F4) ([#301](https://github.com/caura-ai/caura-memclaw/issues/301)) ([b7d73d4](https://github.com/caura-ai/caura-memclaw/commit/b7d73d4465ee01ca9360728c44b68925c58648e6))

## [2.12.1](https://github.com/caura-ai/caura-memclaw/compare/backend-v2.12.0...backend-v2.12.1) (2026-06-07)


### Documentation

* **contributing:** warn about commit subjects that break release-please ([#285](https://github.com/caura-ai/caura-memclaw/issues/285)) ([ea829db](https://github.com/caura-ai/caura-memclaw/commit/ea829db0bd8d21b359c418d14b43e3ea0e29d5c3))


### Code Refactoring

* rename cryptic sp to search_params in ClassifyQuery ([#287](https://github.com/caura-ai/caura-memclaw/issues/287)) ([bad3109](https://github.com/caura-ai/caura-memclaw/commit/bad3109da265f13507532484f2c9dd892f8dd8af))

## [2.12.0](https://github.com/caura-ai/caura-memclaw/compare/backend-v2.11.0...backend-v2.12.0) (2026-06-03)


### Features

* **api:** reject server-reserved memory_types at write boundary (C3, C8) ([#261](https://github.com/caura-ai/caura-memclaw/issues/261)) ([0abd727](https://github.com/caura-ai/caura-memclaw/commit/0abd727f31b53430519054cc339c238f319489ba))
* **api:** surface near_duplicate_of in default-mode write metadata (A21) ([#266](https://github.com/caura-ai/caura-memclaw/issues/266)) ([e7f9bc8](https://github.com/caura-ai/caura-memclaw/commit/e7f9bc8de184a99f3b97d0d17d54cd3094a28574))
* **core-api:** add fleet-scoped hard-purge endpoint ([#275](https://github.com/caura-ai/caura-memclaw/issues/275)) ([cd9e2a6](https://github.com/caura-ai/caura-memclaw/commit/cd9e2a6a5a63e03f404797944d056025f8e53df0))
* **core:** deletion-preview counts (CAURA-696 PR A — OSS half) ([#246](https://github.com/caura-ai/caura-memclaw/issues/246)) ([454a233](https://github.com/caura-ai/caura-memclaw/commit/454a233d6fdbaaad67053bf40e3e7b584c956f52))
* **core:** OSS suppression mirror + boundary guard (CAURA-694) ([#244](https://github.com/caura-ai/caura-memclaw/issues/244)) ([64cf3e4](https://github.com/caura-ai/caura-memclaw/commit/64cf3e446cf26b634e3dee39197f2fcc76affe88))
* **evolve:** surface weight-adjustment skip reason (A15) ([#257](https://github.com/caura-ai/caura-memclaw/issues/257)) ([5f314d9](https://github.com/caura-ai/caura-memclaw/commit/5f314d90c8e847b874a7d5fcd2c9e5218a4c0e1c))
* **lifecycle:** align all lifecycle crons to a fixed UTC hour, not boot-relative intervals ([#278](https://github.com/caura-ai/caura-memclaw/issues/278)) ([6b73bba](https://github.com/caura-ai/caura-memclaw/commit/6b73bba5e95769247101a02a788f3e8de6ee7819))
* **lifecycle:** run insights cron at a fixed UTC hour, not a boot-relative interval ([#277](https://github.com/caura-ai/caura-memclaw/issues/277)) ([bca3ddd](https://github.com/caura-ai/caura-memclaw/commit/bca3dddadf5f439ce00585c2df23a992b39f550e))
* **plugin:** bump MEMCLAW_KEYSTONES_TOKEN_CAP default 500 → 1500 (CAURA-000) ([#249](https://github.com/caura-ai/caura-memclaw/issues/249)) ([6c3b021](https://github.com/caura-ai/caura-memclaw/commit/6c3b021e4e011e1d1ada5e811b4e3ca68065e615))
* **storage:** purge_org_data primitive for org hard-delete (CAURA-689) ([#241](https://github.com/caura-ai/caura-memclaw/issues/241)) ([f3d896b](https://github.com/caura-ai/caura-memclaw/commit/f3d896b1890e5e2e46e166afdf225d475046ebf0))


### Bug Fixes

* **api:** accept metadata_mode as a query param on PATCH /memories/{id} (C7) ([#265](https://github.com/caura-ai/caura-memclaw/issues/265)) ([808f4c5](https://github.com/caura-ai/caura-memclaw/commit/808f4c564e413ee9822fad08acf1cc2b72c02727))
* **mcp:** extend mcp-agent default refusal to all read tools (A29) ([#263](https://github.com/caura-ai/caura-memclaw/issues/263)) ([1cccbe3](https://github.com/caura-ai/caura-memclaw/commit/1cccbe37a1c312978085fecf59a1753b5eb06b4b))
* **mcp:** extend mcp-agent default refusal to all write tools (A14) ([#256](https://github.com/caura-ai/caura-memclaw/issues/256)) ([887c702](https://github.com/caura-ai/caura-memclaw/commit/887c7021d6e00bab844a893005f723cda55f96c0))
* **plugin:** context-engine auto-recall, smoke cleanup & post-upgrade allowlist drift ([#274](https://github.com/caura-ai/caura-memclaw/issues/274)) ([573857c](https://github.com/caura-ai/caura-memclaw/commit/573857c70867541069d4f8e8157864b19372f5d1))
* **plugin:** tolerate undefined config in ContextEngine constructor (CAURA-000) ([#247](https://github.com/caura-ai/caura-memclaw/issues/247)) ([78e10fb](https://github.com/caura-ai/caura-memclaw/commit/78e10fb3cc21021883798d43d8e09f55bb33890d))
* **recall:** add items alias key to /recall response (C4) ([#262](https://github.com/caura-ai/caura-memclaw/issues/262)) ([88ca5a8](https://github.com/caura-ai/caura-memclaw/commit/88ca5a8f33008370bfb720695582c190c39c1207))
* **search:** dedup per-tenant storage_search slot on entity-lookup fall-through (C10) ([#264](https://github.com/caura-ai/caura-memclaw/issues/264)) ([905db5c](https://github.com/caura-ai/caura-memclaw/commit/905db5c6874a5dbcaa1a387d3730caca87da0656))
* **search:** include EMBEDDING_QUERY_INSTRUCTION in embedding cache key (C9) ([#259](https://github.com/caura-ai/caura-memclaw/issues/259)) ([595300a](https://github.com/caura-ai/caura-memclaw/commit/595300aae3e51d14d183a1f52a6461e3244e98ec))
* **search:** per-task timeout for parallel_embed_entity_boost (D7) ([#260](https://github.com/caura-ai/caura-memclaw/issues/260)) ([68f1977](https://github.com/caura-ai/caura-memclaw/commit/68f19773ecd0d840048b046071e42a813eb68d50))
* **search:** share entity tokenizer between FTS-weight + entity-FTS gates (A27) ([#258](https://github.com/caura-ai/caura-memclaw/issues/258)) ([71fd841](https://github.com/caura-ai/caura-memclaw/commit/71fd8412086f426519a401038f9954242c41e120))
* **security:** block agent credentials from agent-management & settings (trust self-escalation) ([#252](https://github.com/caura-ai/caura-memclaw/issues/252)) ([8475901](https://github.com/caura-ai/caura-memclaw/commit/8475901603d52438638bb890c562017d3ea42fa1))
* **security:** enforce fleet/agent scope on by-id memory access ([#250](https://github.com/caura-ai/caura-memclaw/issues/250)) ([fde5ceb](https://github.com/caura-ai/caura-memclaw/commit/fde5cebe569e1fdb5c7cab101d60a4bc857719a8))
* **security:** enforce fleet/agent scope on entity reads + document deletes ([#253](https://github.com/caura-ai/caura-memclaw/issues/253)) ([dab6b71](https://github.com/caura-ai/caura-memclaw/commit/dab6b711921a5bba568a083eb6d8c09b2545870a))
* **security:** gateway-shared-secret gate on the X-Tenant-ID auth path (CRITICAL-1, app side) ([#254](https://github.com/caura-ai/caura-memclaw/issues/254)) ([045039f](https://github.com/caura-ai/caura-memclaw/commit/045039f341ba54fe080d0e83f04e84fdc961750b))
* **security:** require admin-trust for bulk/whole-tenant memory deletes ([#251](https://github.com/caura-ai/caura-memclaw/issues/251)) ([9fcac01](https://github.com/caura-ai/caura-memclaw/commit/9fcac01685dee45072b809cb45d9366e63898e13))
* **version-compat:** bump MIN_RECOMMENDED_PLUGIN_VERSION to 2.7.0 ([#279](https://github.com/caura-ai/caura-memclaw/issues/279)) ([7238180](https://github.com/caura-ai/caura-memclaw/commit/7238180a4f451838e3e4e7c71c85609d08aa58b3))


### Performance

* **entity-extraction:** collapse worker N+1 storage HTTPs (P1) ([#245](https://github.com/caura-ai/caura-memclaw/issues/245)) ([e2d805e](https://github.com/caura-ai/caura-memclaw/commit/e2d805e8b80aa24ba22312e4c9ee35b6381c56ce))


### Documentation

* fix stale API paths, tool counts, and version references ([#255](https://github.com/caura-ai/caura-memclaw/issues/255)) ([496717e](https://github.com/caura-ai/caura-memclaw/commit/496717eeeb28cabdf07d8b690c07e2d03ac7aa2f))

## [2.11.0](https://github.com/caura-ai/caura-memclaw/compare/backend-v2.10.0...backend-v2.11.0) (2026-05-28)


### Features

* **events:** subscription manifest to guard infra provisioning ([#240](https://github.com/caura-ai/caura-memclaw/issues/240)) ([6332c25](https://github.com/caura-ai/caura-memclaw/commit/6332c25a675b14eb4bd9ba17041b534e4c1c1750))
* **lifecycle:** add insights daily cron ([#238](https://github.com/caura-ai/caura-memclaw/issues/238)) ([b17482d](https://github.com/caura-ai/caura-memclaw/commit/b17482d06fdfdea0bf3ea1f497f46b17f5d9767e))
* **plugin-manifest:** include min_auto_deploy_plugin_version ([#225](https://github.com/caura-ai/caura-memclaw/issues/225)) ([09bac0a](https://github.com/caura-ai/caura-memclaw/commit/09bac0acda3e62d904f6b97dcab8cc70ae21744f))
* **storage-api:** add /_debug/pg_locks endpoint for live lock investigation (CAURA-686) ([#219](https://github.com/caura-ai/caura-memclaw/issues/219)) ([80b2eed](https://github.com/caura-ai/caura-memclaw/commit/80b2eed9b4256c8562d8e49c3992ea6fb9f1d983))


### Bug Fixes

* **core-api:** bump storage_client httpx pool limits 100/50 -&gt; 200/150 (CAURA-682) ([#224](https://github.com/caura-ai/caura-memclaw/issues/224)) ([240e0b3](https://github.com/caura-ai/caura-memclaw/commit/240e0b31ec7230af30c059b8f7ad1c021dae22e6))
* **logging:** propagate stdlib logger extras into structlog JSON ([#209](https://github.com/caura-ai/caura-memclaw/issues/209)) ([43240ca](https://github.com/caura-ai/caura-memclaw/commit/43240caf4aea9815b6bf2101ebc606978a74f41a))
* **mcp:** wire read-only scope gate and reset ContextVars per request ([#210](https://github.com/caura-ai/caura-memclaw/issues/210)) ([7f21f14](https://github.com/caura-ai/caura-memclaw/commit/7f21f14f902cc415982cd8cb2a81c1f5e042021f))
* **plugin:** delegate compaction to OpenClaw runtime SDK to unwedge over-budget groups (CAURA-000) ([#234](https://github.com/caura-ai/caura-memclaw/issues/234)) ([6e70d3f](https://github.com/caura-ai/caura-memclaw/commit/6e70d3f677cfec4029edbada88fe4f1e88cd4732))
* **plugin:** wire keystones into WhatsApp system prompts end-to-end (CAURA-000) ([#212](https://github.com/caura-ai/caura-memclaw/issues/212)) ([cb54bda](https://github.com/caura-ai/caura-memclaw/commit/cb54bda929e9f45744c6baea485e00aede9c682e))
* **storage-api:** bulk-insert memory_entity_links with ON CONFLICT DO NOTHING (CAURA-686) ([#220](https://github.com/caura-ai/caura-memclaw/issues/220)) ([7ab127a](https://github.com/caura-ai/caura-memclaw/commit/7ab127aeac2b61c3f923b6570c2de8e1b42b70e1))
* **storage:** drop name_embedding/search_vector from entity payloads ([#206](https://github.com/caura-ai/caura-memclaw/issues/206)) ([731ec6c](https://github.com/caura-ai/caura-memclaw/commit/731ec6cd1d65279de31e2441236f463b8ad84583))
* **version-compat:** bump MIN_RECOMMENDED_PLUGIN_VERSION to 2.6.3 ([#226](https://github.com/caura-ai/caura-memclaw/issues/226)) ([bf9021c](https://github.com/caura-ai/caura-memclaw/commit/bf9021ccb6910753c02c6fc754d30f3015a05405))
* **version-compat:** bump MIN_RECOMMENDED_PLUGIN_VERSION to 2.6.4 ([#242](https://github.com/caura-ai/caura-memclaw/issues/242)) ([f5668f6](https://github.com/caura-ai/caura-memclaw/commit/f5668f6d0e99ee4f3d4612c8dbdafcaff2a8bb13))
* **write:** skip embedding-cache lookup when embedding is deferred (CAURA-682) ([#216](https://github.com/caura-ai/caura-memclaw/issues/216)) ([195176d](https://github.com/caura-ai/caura-memclaw/commit/195176da129d02d2e409b2f6754b67e78d46544e))


### Performance

* **contradiction:** collapse N+1 status updates into batch_update_status per path (P2) ([#236](https://github.com/caura-ai/caura-memclaw/issues/236)) ([2c8942c](https://github.com/caura-ai/caura-memclaw/commit/2c8942c0b88039aa6cc7b8990420ea39a3de26c1))
* **crystallizer:** collapse archive-sweep N+1 storage HTTPs (P5) ([#239](https://github.com/caura-ai/caura-memclaw/issues/239)) ([751108c](https://github.com/caura-ai/caura-memclaw/commit/751108c7bf320841105d85a06cf117341e86b913))
* **ingest:** route ingest_commit through create_memories_bulk ([#227](https://github.com/caura-ai/caura-memclaw/issues/227)) ([245b947](https://github.com/caura-ai/caura-memclaw/commit/245b947e9ccf22bd6bd55a64c125c2a3511bb940))
* **mcp:** close DB session before evolve LLM round-trip ([#232](https://github.com/caura-ai/caura-memclaw/issues/232)) ([d652777](https://github.com/caura-ai/caura-memclaw/commit/d6527775485fe7cfe135479860037eac85a6a437))
* **mcp:** close DB session before insights LLM round-trip ([#231](https://github.com/caura-ai/caura-memclaw/issues/231)) ([a8af687](https://github.com/caura-ai/caura-memclaw/commit/a8af687ec0626f1fdc460b26736aaa548844bc7c))
* **mcp:** close DB session before recall brief LLM round-trip ([#228](https://github.com/caura-ai/caura-memclaw/issues/228)) ([13f09b6](https://github.com/caura-ai/caura-memclaw/commit/13f09b6bb6d70641f0b86d3d843076771b063cfd))
* **recall:** add stampede guard to query-embedding cache ([#213](https://github.com/caura-ai/caura-memclaw/issues/213)) ([677032f](https://github.com/caura-ai/caura-memclaw/commit/677032ff2d4f7d60bccf53f3c8298c72b1aba46a))
* **recall:** close DB session before LLM brief on REST /recall ([#233](https://github.com/caura-ai/caura-memclaw/issues/233)) ([21f8434](https://github.com/caura-ai/caura-memclaw/commit/21f84341cfdfa2f9228fc7859d81930be8cbe9b2))
* **stats:** collapse compute_memory_stats into one GROUPING SETS query ([#214](https://github.com/caura-ai/caura-memclaw/issues/214)) ([63310a1](https://github.com/caura-ai/caura-memclaw/commit/63310a18fcfa77628c3eeb0a2301824a319ec8ab))


### Documentation

* **memories:** document PATCH concurrency contract ([#208](https://github.com/caura-ai/caura-memclaw/issues/208)) ([d2c452c](https://github.com/caura-ai/caura-memclaw/commit/d2c452ccdd284ff2d71ddb597bde7ba79bc1a84a))

## [2.10.0](https://github.com/caura-ai/caura-memclaw/compare/backend-v2.9.0...backend-v2.10.0) (2026-05-25)


### Features

* **core-api:** per-phase write-latency instrumentation (CAURA-682 Phase 1) ([#201](https://github.com/caura-ai/caura-memclaw/issues/201)) ([d28055e](https://github.com/caura-ai/caura-memclaw/commit/d28055e95cfdc833a86e4643f144801688848dfa))


### Bug Fixes

* **version-compat:** bump MIN_RECOMMENDED_PLUGIN_VERSION to 2.6.2 ([#204](https://github.com/caura-ai/caura-memclaw/issues/204)) ([3ae24a3](https://github.com/caura-ai/caura-memclaw/commit/3ae24a303e83fa5f11b79d562cfb536974ae1e77))

## [2.9.0](https://github.com/caura-ai/caura-memclaw/compare/backend-v2.8.0...backend-v2.9.0) (2026-05-24)


### Features

* **contradiction:** back-channel idempotency on detection entry (A4 [#14](https://github.com/caura-ai/caura-memclaw/issues/14)) ([#188](https://github.com/caura-ai/caura-memclaw/issues/188)) ([f370fad](https://github.com/caura-ai/caura-memclaw/commit/f370fad23af42d3c662fb19a2c214590d160e7f1))
* **contradiction:** Path C re-judges and retracts wrong Path A verdicts (A4 [#13](https://github.com/caura-ai/caura-memclaw/issues/13)) ([#187](https://github.com/caura-ai/caura-memclaw/issues/187)) ([1669690](https://github.com/caura-ai/caura-memclaw/commit/1669690c6e37c740e1315e2c867b69fd62d3823b))
* **contradiction:** widen _llm_contradiction_check to (verdict, confidence) (A4 [#12](https://github.com/caura-ai/caura-memclaw/issues/12)) ([#186](https://github.com/caura-ai/caura-memclaw/issues/186)) ([0c332a3](https://github.com/caura-ai/caura-memclaw/commit/0c332a3e8923acc78fcc15db70513e9a9f31f6e9))
* **dedup:** add two-tier dedup threshold constants (A1 [#15](https://github.com/caura-ai/caura-memclaw/issues/15)) ([#190](https://github.com/caura-ai/caura-memclaw/issues/190)) ([dcc41ae](https://github.com/caura-ai/caura-memclaw/commit/dcc41aea2e2589c2ca8163415e4560ce8b78eaf8))
* **dedup:** backend review queue for ambiguous decisions (A1 [#18](https://github.com/caura-ai/caura-memclaw/issues/18)) ([#194](https://github.com/caura-ai/caura-memclaw/issues/194)) ([bf6dce7](https://github.com/caura-ai/caura-memclaw/commit/bf6dce73d697b08c7a0ba3a35e341502e93016ea))
* **dedup:** identifier pre-filter in CheckSemanticDuplicate (A1) ([#195](https://github.com/caura-ai/caura-memclaw/issues/195)) ([a578a35](https://github.com/caura-ai/caura-memclaw/commit/a578a35e36be38f9fef35b42215e66ec640913f0))
* **evolve:** surface rule-synthesis skip reason on every path (A10) ([#199](https://github.com/caura-ai/caura-memclaw/issues/199)) ([b16de2b](https://github.com/caura-ai/caura-memclaw/commit/b16de2bfbbfdc74d79ce1d4b9e318069cec5a05c))
* **storage:** find_entity_overlap_candidates surfaces conflicted supersedes_of target (A4 [#11](https://github.com/caura-ai/caura-memclaw/issues/11)) ([#185](https://github.com/caura-ai/caura-memclaw/issues/185)) ([f120f55](https://github.com/caura-ai/caura-memclaw/commit/f120f558d13ad1b4f37a5069a1c187a918fec930))


### Bug Fixes

* **enrichment:** action/episode few-shot disambiguation (A9) ([#198](https://github.com/caura-ai/caura-memclaw/issues/198)) ([81ec08f](https://github.com/caura-ai/caura-memclaw/commit/81ec08f59411b4d20fd1732ff4c6abe788c82f9d))
* **enrichment:** tighten tag generation prompt + normalising validator (A8) ([#197](https://github.com/caura-ai/caura-memclaw/issues/197)) ([6bea431](https://github.com/caura-ai/caura-memclaw/commit/6bea4318de2f6324a621a5573ae9b79ded85b54c))
* **plugin:** conform to OpenClaw MemoryFlushPlan contract (CAURA-000) ([#191](https://github.com/caura-ai/caura-memclaw/issues/191)) ([ea750c7](https://github.com/caura-ai/caura-memclaw/commit/ea750c7abea37cbce21fef85ffe21db830bb5f07))
* **search:** query classifier recall on entity-token queries (A7) ([#196](https://github.com/caura-ai/caura-memclaw/issues/196)) ([670549b](https://github.com/caura-ai/caura-memclaw/commit/670549b4596395e91e8932030605248e75066321))
* **storage:** invert A4 [#11](https://github.com/caura-ai/caura-memclaw/issues/11) include_supersedes filter direction ([#189](https://github.com/caura-ai/caura-memclaw/issues/189)) ([831897d](https://github.com/caura-ai/caura-memclaw/commit/831897d17f89b436530e47452938f62fa9b0fea0))


### Code Refactoring

* **config:** delete legacy embed/enrich_on_hot_path flags + asymmetric branch (F3 Phase 3) ([#183](https://github.com/caura-ai/caura-memclaw/issues/183)) ([9116e80](https://github.com/caura-ai/caura-memclaw/commit/9116e80237a4186493c8923b1ac1e40a3da4faf7))

## [2.8.0](https://github.com/caura-ai/caura-memclaw/compare/backend-v2.7.0...backend-v2.8.0) (2026-05-20)


### Features

* **config:** add deployment_mode setting alongside legacy flags (F3 Phase 1) ([#173](https://github.com/caura-ai/caura-memclaw/issues/173)) ([1cc30b0](https://github.com/caura-ai/caura-memclaw/commit/1cc30b0525da92c8dc73e7a6960978d8fc249010))
* **entity-extraction:** plumb tenant_config through worker → extractor (A5c) ([#170](https://github.com/caura-ai/caura-memclaw/issues/170)) ([62bdee9](https://github.com/caura-ai/caura-memclaw/commit/62bdee99d9484f47b4a43a099fe72d6bd8ce11f8))
* **entity-extraction:** prompt + schema overhaul (A5b) ([#169](https://github.com/caura-ai/caura-memclaw/issues/169)) ([7c0d862](https://github.com/caura-ai/caura-memclaw/commit/7c0d86206812f61267403f7d9030965c552b969b))
* **storage:** support retracting supersedes_id verdicts (A4 [#10](https://github.com/caura-ai/caura-memclaw/issues/10)) ([#171](https://github.com/caura-ai/caura-memclaw/issues/171)) ([ccebcb5](https://github.com/caura-ai/caura-memclaw/commit/ccebcb514ed7a91dc49efd43feb9c8affaaa8143))
* widen cross-tenant credential reads across all surfaces + audit emission + /whoami scope ([#164](https://github.com/caura-ai/caura-memclaw/issues/164)) ([0afacdb](https://github.com/caura-ai/caura-memclaw/commit/0afacdbcc7cf67b0e1f368fe96f4687bf6b90b63))


### Bug Fixes

* **entity-extraction:** deterministic seed + first-seen canonical preservation (A5a) ([#167](https://github.com/caura-ai/caura-memclaw/issues/167)) ([0a27e66](https://github.com/caura-ai/caura-memclaw/commit/0a27e66c41cec2871df31e797fa441bc966fd18f))
* **mcp:** disable auto-generated output schema on tool registrations ([#180](https://github.com/caura-ai/caura-memclaw/issues/180)) ([61d19a4](https://github.com/caura-ai/caura-memclaw/commit/61d19a4d84430cc7080b32f3822f5a00d98afff7))
* **mcp:** MISSING_AGENT_ID error no longer points at deprecated mca_ keys ([#161](https://github.com/caura-ai/caura-memclaw/issues/161)) ([c7525bf](https://github.com/caura-ai/caura-memclaw/commit/c7525bf303f6dfb4631e82e749c36131463de278))
* **plugin:** close install-script alsoAllow drift + manifest version drift (CAURA-444) ([#181](https://github.com/caura-ai/caura-memclaw/issues/181)) ([28f22aa](https://github.com/caura-ai/caura-memclaw/commit/28f22aa72b0ca306b5f1a7e25a6005ead484db02))
* **release-please:** correct openclaw.plugin.json path under the plugin package ([#182](https://github.com/caura-ai/caura-memclaw/issues/182)) ([630653c](https://github.com/caura-ai/caura-memclaw/commit/630653caa7ca4b0d9ebca0729bc101173b3c3c4d))
* **tests:** make rate-limit burst tests reliable + defensively unset MEMCLAW_API_KEY ([#177](https://github.com/caura-ai/caura-memclaw/issues/177)) ([bc5e144](https://github.com/caura-ai/caura-memclaw/commit/bc5e1446734db05e012653ad1cb7d9832fcc40fb))


### Code Refactoring

* **memory_service:** migrate 7 legacy flag reads onto deployment_mode (F3 Phase 2c) ([#178](https://github.com/caura-ai/caura-memclaw/issues/178)) ([17d8635](https://github.com/caura-ai/caura-memclaw/commit/17d8635a4265b9d90b70965b9ade1c0d4d59f48b))
* **pipeline:** migrate parallel_embed_enrich onto deployment_mode (F3 Phase 2 batch 1) ([#174](https://github.com/caura-ai/caura-memclaw/issues/174)) ([1a36481](https://github.com/caura-ai/caura-memclaw/commit/1a364819162e28fb68e202be4c7c5068f475e260))

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
