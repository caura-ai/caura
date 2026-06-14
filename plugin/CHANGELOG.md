# Changelog

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
