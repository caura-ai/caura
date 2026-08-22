# Changelog

## [2.16.1](https://github.com/caura-ai/caura/compare/plugin-v2.16.0...plugin-v2.16.1) (2026-08-13)


### Dependencies

* **plugin:** bump @types/node from 26.1.1 to 26.2.0 in /plugin in the npm-minor-patch group across 1 directory ([#688](https://github.com/caura-ai/caura/issues/688)) ([a40ca4a](https://github.com/caura-ai/caura/commit/a40ca4ab6b7e8f2b9cf9f9aa7c5e16af0092c232))


### Documentation

* **write:** document embedding_pending and the strong-mode opt-out ([#706](https://github.com/caura-ai/caura/issues/706)) ([187b5b5](https://github.com/caura-ai/caura/commit/187b5b5465d7659624e13f455e662c9fce67c483))

## [2.16.0](https://github.com/caura-ai/caura-memclaw/compare/plugin-v2.15.2...plugin-v2.16.0) (2026-07-28)


### Features

* **plugin:** interviewer task-trail capture — mirror OpenClaw task_runs into the interview buffer ([#658](https://github.com/caura-ai/caura-memclaw/issues/658)) ([ba5f4b7](https://github.com/caura-ai/caura-memclaw/commit/ba5f4b76a6a261f3c8884f6bcf624be7e5ea8951))

## [2.15.2](https://github.com/caura-ai/caura-memclaw/compare/plugin-v2.15.1...plugin-v2.15.2) (2026-07-26)


### Bug Fixes

* **mcp:** allow own-fleet list/stats reads at trust 1 ([#617](https://github.com/caura-ai/caura-memclaw/issues/617)) ([1a6fe9f](https://github.com/caura-ai/caura-memclaw/commit/1a6fe9fbf37acee03ef26074dc6f24910ad570da))


### Dependencies

* **plugin:** bump @types/node from 25.4.0 to 26.1.1 in /plugin ([#546](https://github.com/caura-ai/caura-memclaw/issues/546)) ([caa2659](https://github.com/caura-ai/caura-memclaw/commit/caa26595153ffb24e2cadcfeca7572c0d45bb1e3))
* **plugin:** upgrade TypeScript to 7 (supersedes [#547](https://github.com/caura-ai/caura-memclaw/issues/547)) ([#611](https://github.com/caura-ai/caura-memclaw/issues/611)) ([081551e](https://github.com/caura-ai/caura-memclaw/commit/081551e91846221b0ba3582f3ef255a5b1e25647))


### Documentation

* OSS-side fixes from the 2026-07-26 product-coordination audit ([#625](https://github.com/caura-ai/caura-memclaw/issues/625)) ([71debfd](https://github.com/caura-ai/caura-memclaw/commit/71debfd8fb6db2b36e46322c987a8e57aef63597))

## [2.15.1](https://github.com/caura-ai/caura-memclaw/compare/plugin-v2.15.0...plugin-v2.15.1) (2026-07-20)


### Bug Fixes

* **interview:** latency budgets for realistic submit windows (real-LLM pilot finding) ([#572](https://github.com/caura-ai/caura-memclaw/issues/572)) ([740505d](https://github.com/caura-ai/caura-memclaw/commit/740505da50aa6bdc2916a1ca7aecb0a993855718))

## [2.15.0](https://github.com/caura-ai/caura-memclaw/compare/plugin-v2.14.0...plugin-v2.15.0) (2026-07-18)


### Features

* **plugin:** Interviewer Phase 1 — OpenClaw plugin side (durable buffer + interview_request handler) ([#564](https://github.com/caura-ai/caura-memclaw/issues/564)) ([5e2d5e7](https://github.com/caura-ai/caura-memclaw/commit/5e2d5e770aa47e1349a87813921c082ac08b90ab))

## [2.14.0](https://github.com/caura-ai/caura-memclaw/compare/plugin-v2.13.0...plugin-v2.14.0) (2026-07-08)


### Features

* **plugin:** opt-in recall-gating + cross-agent recall (F2/A44) ([#530](https://github.com/caura-ai/caura-memclaw/issues/530)) ([a68b3d0](https://github.com/caura-ai/caura-memclaw/commit/a68b3d0d4d141bd15bc185afb2c4922847d449ec))

## [2.13.0](https://github.com/caura-ai/caura-memclaw/compare/plugin-v2.12.0...plugin-v2.13.0) (2026-07-05)


### Features

* **core-api,plugin:** self-identify MCP writes when the verified id is reserved "main" ([#507](https://github.com/caura-ai/caura-memclaw/issues/507)) ([1bc00c1](https://github.com/caura-ai/caura-memclaw/commit/1bc00c14e2de653f006ab5226e1c22626b5f829d))

## [2.12.0](https://github.com/caura-ai/caura-memclaw/compare/plugin-v2.11.0...plugin-v2.12.0) (2026-06-25)


### Features

* **skill:** curb trivial memory writes in agent guidance ([#494](https://github.com/caura-ai/caura-memclaw/issues/494)) ([d7bca1f](https://github.com/caura-ai/caura-memclaw/commit/d7bca1f9e7a9a38279d3d48f8e717505247bd474))

## [2.11.0](https://github.com/caura-ai/caura-memclaw/compare/plugin-v2.10.1...plugin-v2.11.0) (2026-06-24)


### Features

* **plugin:** additive-mode skill reconcile with ownership marker (PR2) ([#417](https://github.com/caura-ai/caura-memclaw/issues/417)) ([3d970ef](https://github.com/caura-ai/caura-memclaw/commit/3d970ef2cbd26c4ab3830101bb4ae313409545bd))
* **plugin:** auto-register additive skill dirs on OpenClaw's load path (PR3b) ([#425](https://github.com/caura-ai/caura-memclaw/issues/425)) ([819a77f](https://github.com/caura-ai/caura-memclaw/commit/819a77f9110352ad5008c7d1160a60a43b5451fa))
* **plugin:** per-target reconcile observability (PR3a) ([#424](https://github.com/caura-ai/caura-memclaw/issues/424)) ([04120f2](https://github.com/caura-ai/caura-memclaw/commit/04120f241f6083615dcab2de49fca661d692c334))
* **skills:** refresh canonical + plugin memclaw skills, add company-brain ([#482](https://github.com/caura-ai/caura-memclaw/issues/482)) ([aa03c03](https://github.com/caura-ai/caura-memclaw/commit/aa03c03c47aaeef4da3390d2b810ae3b46045bfd))


### Documentation

* **core-api:** clarify memclaw_recall top_k is soft-capped, not rejected ([#481](https://github.com/caura-ai/caura-memclaw/issues/481)) ([6938f08](https://github.com/caura-ai/caura-memclaw/commit/6938f08fd52dd9a9c9efdb3fba42f7b20c4b65d0))


### Code Refactoring

* **plugin:** configurable skill-reconcile targets (PR1 — refactor + config plumbing) ([#413](https://github.com/caura-ai/caura-memclaw/issues/413)) ([57e5e96](https://github.com/caura-ai/caura-memclaw/commit/57e5e962cbc5a0f95555e0bfe762e5a3ae309b49))

## [2.10.1](https://github.com/caura-ai/caura-memclaw/compare/plugin-v2.10.0...plugin-v2.10.1) (2026-06-15)


### Bug Fixes

* **plugin:** send X-Bulk-Attempt-Id header on bulk memory writes ([#355](https://github.com/caura-ai/caura-memclaw/issues/355)) ([a4515ed](https://github.com/caura-ai/caura-memclaw/commit/a4515ed1e5f5f7679daea36c99e58d1d331b3934))

## [2.10.0](https://github.com/caura-ai/caura-memclaw/compare/plugin-v2.9.0...plugin-v2.10.0) (2026-06-13)


### Features

* **skills:** gate the OpenClaw install path to active-only skills ([#317](https://github.com/caura-ai/caura-memclaw/issues/317)) ([50488fc](https://github.com/caura-ai/caura-memclaw/commit/50488fc6eb07d244297ad1d3e065887c8cb3e47e))
* **skills:** per-node skill-reconcile observability on the heartbeat ([#325](https://github.com/caura-ai/caura-memclaw/issues/325)) ([d806114](https://github.com/caura-ai/caura-memclaw/commit/d806114bc943a1d97480584192091e0583d41545))


### Bug Fixes

* **plugin:** point agents at the memclaw skill by name, not by filesystem path (CAURA-000) ([#323](https://github.com/caura-ai/caura-memclaw/issues/323)) ([ec3fcaf](https://github.com/caura-ai/caura-memclaw/commit/ec3fcaf45a7545568e9f713a6f1db631dfa1c2f3))
* **release:** keep plugin/src/version.ts in sync on release-please PRs ([#335](https://github.com/caura-ai/caura-memclaw/issues/335)) ([e26a3c5](https://github.com/caura-ai/caura-memclaw/commit/e26a3c561b542fbf58b4fd556ec2919e87286dfd))

## [2.9.0](https://github.com/caura-ai/caura-memclaw/compare/plugin-v2.8.1...plugin-v2.9.0) (2026-06-08)


### Features

* **plugin:** bump MEMCLAW_KEYSTONES_TOKEN_CAP default 500 → 1500 (CAURA-000) ([#249](https://github.com/caura-ai/caura-memclaw/issues/249)) ([6c3b021](https://github.com/caura-ai/caura-memclaw/commit/6c3b021e4e011e1d1ada5e811b4e3ca68065e615))
* **plugin:** decouple plugin release cadence from backend ([#131](https://github.com/caura-ai/caura-memclaw/issues/131)) ([ac6c0f2](https://github.com/caura-ai/caura-memclaw/commit/ac6c0f2ec05a020b0acb27b6ec7b92a0be338d73))


### Bug Fixes

* **plugin:** bound resolveTenantId fetch with AbortSignal timeout (CAURA-000) ([#292](https://github.com/caura-ai/caura-memclaw/issues/292)) ([a3532a4](https://github.com/caura-ai/caura-memclaw/commit/a3532a433ab4ef5032304a2c125251c7c4bfdea5))
* **plugin:** close install-script alsoAllow drift + manifest version drift (CAURA-444) ([#181](https://github.com/caura-ai/caura-memclaw/issues/181)) ([28f22aa](https://github.com/caura-ai/caura-memclaw/commit/28f22aa72b0ca306b5f1a7e25a6005ead484db02))
* **plugin:** conform to OpenClaw MemoryFlushPlan contract (CAURA-000) ([#191](https://github.com/caura-ai/caura-memclaw/issues/191)) ([ea750c7](https://github.com/caura-ai/caura-memclaw/commit/ea750c7abea37cbce21fef85ffe21db830bb5f07))
* **plugin:** context-engine auto-recall, smoke cleanup & post-upgrade allowlist drift ([#274](https://github.com/caura-ai/caura-memclaw/issues/274)) ([573857c](https://github.com/caura-ai/caura-memclaw/commit/573857c70867541069d4f8e8157864b19372f5d1))
* **plugin:** delegate compaction to OpenClaw runtime SDK to unwedge over-budget groups (CAURA-000) ([#234](https://github.com/caura-ai/caura-memclaw/issues/234)) ([6e70d3f](https://github.com/caura-ai/caura-memclaw/commit/6e70d3f677cfec4029edbada88fe4f1e88cd4732))
* **plugin:** don't create plugins.allow from nothing on autoFix (CAURA-000) ([#307](https://github.com/caura-ai/caura-memclaw/issues/307)) ([2aab828](https://github.com/caura-ai/caura-memclaw/commit/2aab828324ca054024ba075a2bbc682f749c6f8a))
* **plugin:** memoize bootstrap at process level (CAURA-000) ([#303](https://github.com/caura-ai/caura-memclaw/issues/303)) ([3bbb559](https://github.com/caura-ai/caura-memclaw/commit/3bbb559ea8cf397a0a49f3b00db37d5978e9b3df))
* **plugin:** schedule restart AFTER result POST resolves (CAURA-000) ([#306](https://github.com/caura-ai/caura-memclaw/issues/306)) ([fe8ad26](https://github.com/caura-ai/caura-memclaw/commit/fe8ad26df789c60f65d38ea64b2fff5a3f82f210))
* **plugin:** suppress bootstrap agent-id warn + swallow afterTurn 409 (CAURA-000) ([#300](https://github.com/caura-ai/caura-memclaw/issues/300)) ([6705315](https://github.com/caura-ai/caura-memclaw/commit/67053152428d55d2e93f9a552ddd0724d1a73393))
* **plugin:** tolerate undefined config in ContextEngine constructor (CAURA-000) ([#247](https://github.com/caura-ai/caura-memclaw/issues/247)) ([78e10fb](https://github.com/caura-ai/caura-memclaw/commit/78e10fb3cc21021883798d43d8e09f55bb33890d))
* **plugin:** wire keystones into WhatsApp system prompts end-to-end (CAURA-000) ([#212](https://github.com/caura-ai/caura-memclaw/issues/212)) ([cb54bda](https://github.com/caura-ai/caura-memclaw/commit/cb54bda929e9f45744c6baea485e00aede9c682e))


### Documentation

* fix stale API paths, tool counts, and version references ([#255](https://github.com/caura-ai/caura-memclaw/issues/255)) ([496717e](https://github.com/caura-ai/caura-memclaw/commit/496717eeeb28cabdf07d8b690c07e2d03ac7aa2f))

## [2.8.0](https://github.com/caura-ai/caura-memclaw/compare/plugin-v2.7.0...plugin-v2.8.0) (2026-06-03)


### Features

* **plugin:** bump MEMCLAW_KEYSTONES_TOKEN_CAP default 500 → 1500 (CAURA-000) ([#249](https://github.com/caura-ai/caura-memclaw/issues/249)) ([6c3b021](https://github.com/caura-ai/caura-memclaw/commit/6c3b021e4e011e1d1ada5e811b4e3ca68065e615))
* **plugin:** decouple plugin release cadence from backend ([#131](https://github.com/caura-ai/caura-memclaw/issues/131)) ([ac6c0f2](https://github.com/caura-ai/caura-memclaw/commit/ac6c0f2ec05a020b0acb27b6ec7b92a0be338d73))


### Bug Fixes

* **plugin:** close install-script alsoAllow drift + manifest version drift (CAURA-444) ([#181](https://github.com/caura-ai/caura-memclaw/issues/181)) ([28f22aa](https://github.com/caura-ai/caura-memclaw/commit/28f22aa72b0ca306b5f1a7e25a6005ead484db02))
* **plugin:** conform to OpenClaw MemoryFlushPlan contract (CAURA-000) ([#191](https://github.com/caura-ai/caura-memclaw/issues/191)) ([ea750c7](https://github.com/caura-ai/caura-memclaw/commit/ea750c7abea37cbce21fef85ffe21db830bb5f07))
* **plugin:** context-engine auto-recall, smoke cleanup & post-upgrade allowlist drift ([#274](https://github.com/caura-ai/caura-memclaw/issues/274)) ([573857c](https://github.com/caura-ai/caura-memclaw/commit/573857c70867541069d4f8e8157864b19372f5d1))
* **plugin:** delegate compaction to OpenClaw runtime SDK to unwedge over-budget groups (CAURA-000) ([#234](https://github.com/caura-ai/caura-memclaw/issues/234)) ([6e70d3f](https://github.com/caura-ai/caura-memclaw/commit/6e70d3f677cfec4029edbada88fe4f1e88cd4732))
* **plugin:** tolerate undefined config in ContextEngine constructor (CAURA-000) ([#247](https://github.com/caura-ai/caura-memclaw/issues/247)) ([78e10fb](https://github.com/caura-ai/caura-memclaw/commit/78e10fb3cc21021883798d43d8e09f55bb33890d))
* **plugin:** wire keystones into WhatsApp system prompts end-to-end (CAURA-000) ([#212](https://github.com/caura-ai/caura-memclaw/issues/212)) ([cb54bda](https://github.com/caura-ai/caura-memclaw/commit/cb54bda929e9f45744c6baea485e00aede9c682e))


### Documentation

* fix stale API paths, tool counts, and version references ([#255](https://github.com/caura-ai/caura-memclaw/issues/255)) ([496717e](https://github.com/caura-ai/caura-memclaw/commit/496717eeeb28cabdf07d8b690c07e2d03ac7aa2f))

## [2.6.3](https://github.com/caura-ai/caura-memclaw/compare/plugin-v2.6.2...plugin-v2.6.3) (2026-05-28)


### Bug Fixes

* **plugin:** delegate compaction to OpenClaw runtime SDK to unwedge over-budget groups (CAURA-000) ([#234](https://github.com/caura-ai/caura-memclaw/issues/234)) ([6e70d3f](https://github.com/caura-ai/caura-memclaw/commit/6e70d3f677cfec4029edbada88fe4f1e88cd4732))
* **plugin:** wire keystones into WhatsApp system prompts end-to-end (CAURA-000) ([#212](https://github.com/caura-ai/caura-memclaw/issues/212)) ([cb54bda](https://github.com/caura-ai/caura-memclaw/commit/cb54bda929e9f45744c6baea485e00aede9c682e))

## [2.6.2](https://github.com/caura-ai/caura-memclaw/compare/plugin-v2.6.1...plugin-v2.6.2) (2026-05-24)


### Bug Fixes

* **plugin:** conform to OpenClaw MemoryFlushPlan contract (CAURA-000) ([#191](https://github.com/caura-ai/caura-memclaw/issues/191)) ([ea750c7](https://github.com/caura-ai/caura-memclaw/commit/ea750c7abea37cbce21fef85ffe21db830bb5f07))

## [2.6.1](https://github.com/caura-ai/caura-memclaw/compare/plugin-v2.6.0...plugin-v2.6.1) (2026-05-20)


### Bug Fixes

* **plugin:** close install-script alsoAllow drift + manifest version drift (CAURA-444) ([#181](https://github.com/caura-ai/caura-memclaw/issues/181)) ([28f22aa](https://github.com/caura-ai/caura-memclaw/commit/28f22aa72b0ca306b5f1a7e25a6005ead484db02))

## [2.6.0](https://github.com/caura-ai/caura-memclaw/compare/plugin-v2.5.0...plugin-v2.6.0) (2026-05-17)


### Features

* **plugin:** decouple plugin release cadence from backend ([#131](https://github.com/caura-ai/caura-memclaw/issues/131)) ([ac6c0f2](https://github.com/caura-ai/caura-memclaw/commit/ac6c0f2ec05a020b0acb27b6ec7b92a0be338d73))
