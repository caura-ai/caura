# Changelog

## [0.5.0](https://github.com/caura-ai/caura/compare/collaboration-clients-v0.4.1...collaboration-clients-v0.5.0) (2026-11-05)


### Features

* **clients:** show memory correction notices to agents and let them confirm ([#2336](https://github.com/caura-ai/caura/issues/2336)) ([ec82502](https://github.com/caura-ai/caura/commit/ec82502f2c4ebe0bf906a0e4c419bcd599067462))

## [0.4.1](https://github.com/caura-ai/caura/compare/collaboration-clients-v0.4.0...collaboration-clients-v0.4.1) (2026-11-04)


### Documentation

* name Connect › Keys, and say who mints agent keys (Prism v2 R9) ([#2316](https://github.com/caura-ai/caura/issues/2316)) ([70f026a](https://github.com/caura-ai/caura/commit/70f026a49e6536c30feda317abf6ec2528547b97))

## [0.4.0](https://github.com/caura-ai/caura/compare/collaboration-clients-v0.3.5...collaboration-clients-v0.4.0) (2026-11-04)


### Features

* **evidence:** clients send real evidence on writes (EC-15) ([#2311](https://github.com/caura-ai/caura/issues/2311)) ([3786c21](https://github.com/caura-ai/caura/commit/3786c21bea5ebc51373be0aa35d6cc29d0375b29))

## [0.3.5](https://github.com/caura-ai/caura/compare/collaboration-clients-v0.3.4...collaboration-clients-v0.3.5) (2026-10-28)


### Documentation

* **clients:** install collaboration release 0.3.4 ([#2258](https://github.com/caura-ai/caura/issues/2258)) ([e2ac4b4](https://github.com/caura-ai/caura/commit/e2ac4b4530e4ef2cab2186f7a74007f291ea0bf9))

## [0.3.4](https://github.com/caura-ai/caura/compare/collaboration-clients-v0.3.3...collaboration-clients-v0.3.4) (2026-10-27)


### Bug Fixes

* **clients:** keep credentials private and encode request identifiers ([ab039ac](https://github.com/caura-ai/caura/commit/ab039acec84932acc1aef32b78c7623403d0bef3))

## [0.3.3](https://github.com/caura-ai/caura/compare/collaboration-clients-v0.3.2...collaboration-clients-v0.3.3) (2026-10-23)


### Bug Fixes

* **collaboration:** use UTF-8 for persisted files and enforce encoding ([#2120](https://github.com/caura-ai/caura/issues/2120)) ([#2173](https://github.com/caura-ai/caura/issues/2173)) ([de771dc](https://github.com/caura-ai/caura/commit/de771dca805c9a3493354e772872075551789ad0))

## [0.3.2](https://github.com/caura-ai/caura/compare/collaboration-clients-v0.3.1...collaboration-clients-v0.3.2) (2026-10-23)


### Dependencies

* bump opentelemetry-api to 1.45.1 for CVE-2026-114603 ([#2166](https://github.com/caura-ai/caura/issues/2166)) ([3714851](https://github.com/caura-ai/caura/commit/3714851a2928e1a114c22d80b567773ab74a0de1))

## [0.3.1](https://github.com/caura-ai/caura/compare/collaboration-clients-v0.3.0...collaboration-clients-v0.3.1) (2026-10-22)


### Bug Fixes

* **collaboration:** raise PlatformError on unexpected response bodies ([#2119](https://github.com/caura-ai/caura/issues/2119)) ([#2130](https://github.com/caura-ai/caura/issues/2130)) ([6e86ae6](https://github.com/caura-ai/caura/commit/6e86ae63adb1cf5784690739269df7b4b9f6cd68))
* **collaboration:** reconnect when the live stream sends a bad frame ([#2141](https://github.com/caura-ai/caura/issues/2141)) ([133ce6f](https://github.com/caura-ai/caura/commit/133ce6ff7386abd18ce3a907872de06bfa8b1cf4))
* **collaboration:** refuse a malformed settings file instead of crashing in install_hooks ([#2128](https://github.com/caura-ai/caura/issues/2128)) ([ac27f75](https://github.com/caura-ai/caura/commit/ac27f752506f82959ada5f63dc59c2bbc760f0d6))
* **release:** let the collaboration clients release past 0.3.0 ([#2148](https://github.com/caura-ai/caura/issues/2148)) ([d3bbc23](https://github.com/caura-ai/caura/commit/d3bbc23bcc2a3306533b56b36d29fecb051e5f06))

## [0.3.0](https://github.com/caura-ai/caura/compare/collaboration-clients-v0.2.0...collaboration-clients-v0.3.0) (2026-10-21)


### Dependencies

* **clients:** bump certifi and pyjwt in the collaboration lock ([#2133](https://github.com/caura-ai/caura/issues/2133)) ([43456cf](https://github.com/caura-ai/caura/commit/43456cf018c032438028ab2fd190c8fd824b6d9d))
