# Changelog

Only the most recent releases are listed below. Every release, including those
no longer shown here, is published with its full notes at
[github.com/caura-ai/caura/releases](https://github.com/caura-ai/caura/releases).

## [2.19.1](https://github.com/caura-ai/caura/compare/plugin-v2.19.0...plugin-v2.19.1) (2026-08-26)


### Bug Fixes

* **api:** reject unknown fields on write bodies instead of dropping them (SAFE-01) ([#1005](https://github.com/caura-ai/caura/issues/1005)) ([94cd4f1](https://github.com/caura-ai/caura/commit/94cd4f1d5a2fbfb72dea157a49a5212990bdabf7))
* **keystones:** correct keystone_trust_hint's stored-shape claim ([#1004](https://github.com/caura-ai/caura/issues/1004)) ([92768f4](https://github.com/caura-ai/caura/commit/92768f4459f30a453db8c460f43312c05c3e4cb1))


### Documentation

* **keystones:** the self-author tier needs an explicit agent_id ([#1001](https://github.com/caura-ai/caura/issues/1001)) ([a79a2ea](https://github.com/caura-ai/caura/commit/a79a2eaa1e0c2855c8650f8c786e949bedb0e4f8))

## [2.19.0](https://github.com/caura-ai/caura/compare/plugin-v2.18.0...plugin-v2.19.0) (2026-08-25)


### Features

* **api:** MCP/REST search parity per the ratified wire contract (C31) ([#962](https://github.com/caura-ai/caura/issues/962)) ([d9bf825](https://github.com/caura-ai/caura/commit/d9bf825041d3ffa317ae4c47921d0dade2ff6a1c))


### Bug Fixes

* the four P1 behavior defects from the sunset report ([#964](https://github.com/caura-ai/caura/issues/964)) ([fd756e8](https://github.com/caura-ai/caura/commit/fd756e89f3e960ff94f962947738cc9c79cb616d))

## [2.18.0](https://github.com/caura-ai/caura/compare/plugin-v2.17.0...plugin-v2.18.0) (2026-08-25)


### Features

* **api:** structured errors, safe deletes, and client alias packages ([#950](https://github.com/caura-ai/caura/issues/950)) ([e9ae581](https://github.com/caura-ai/caura/commit/e9ae58141e5c96be6d9146b6dc238bdcde813cab))


### Bug Fixes

* **mcp:** stop the identity surface misdescribing itself ([#951](https://github.com/caura-ai/caura/issues/951)) ([a28938d](https://github.com/caura-ai/caura/commit/a28938d34af3b501ec097dcacadb83f35e307f6f))
* **plugin:** caura_list scope='all' spans fleets instead of narrowing to one ([#904](https://github.com/caura-ai/caura/issues/904)) ([2bdf359](https://github.com/caura-ai/caura/commit/2bdf35933e747cda1f57fb21a30b2d52e8c9b684))
* **plugin:** the strings the plugin emits used the previous brand name ([#902](https://github.com/caura-ai/caura/issues/902)) ([b8450a6](https://github.com/caura-ai/caura/commit/b8450a64c2780206fae5e65d0d40442487e492f0))
* stop minting old-brand strings into new installs and registrations ([#928](https://github.com/caura-ai/caura/issues/928)) ([f877e09](https://github.com/caura-ai/caura/commit/f877e098076eae1d570b03a4c5e0c0c01fed1b2b))


### Documentation

* **mcp:** scope has no single default, so the SoT descriptions stop naming one ([#910](https://github.com/caura-ai/caura/issues/910)) ([754336e](https://github.com/caura-ai/caura/commit/754336e950573ad08ef5f936f0ec8cf904d3e64c))
* **plugin:** an omitted scope is not scope='agent' on caura_list/caura_stats ([#906](https://github.com/caura-ai/caura/issues/906)) ([41ee5a6](https://github.com/caura-ai/caura/commit/41ee5a685fa62ad02c4fae9562cf8066feb4a1a8))
* teach the CAURA_* names everywhere humans read ([#929](https://github.com/caura-ai/caura/issues/929)) ([815221d](https://github.com/caura-ai/caura/commit/815221de4ac44e1db01e8b4c30b5eb53c1be9743))


### Code Refactoring

* **plugin:** collapse the plugin id to one constant and pin both ends ([#941](https://github.com/caura-ai/caura/issues/941)) ([3e62c74](https://github.com/caura-ai/caura/commit/3e62c749afd8280c6c4911191f1cbfd844baa9f6))

## [2.17.0](https://github.com/caura-ai/caura/compare/plugin-v2.16.1...plugin-v2.17.0) (2026-08-23)


### Features

* **env:** read CAURA_* everywhere the old names are read ([#886](https://github.com/caura-ai/caura/issues/886)) ([74b8a07](https://github.com/caura-ai/caura/commit/74b8a07386cbd2338c4816c0a0eeb049c7d2bb6c))

## [2.16.1](https://github.com/caura-ai/caura/compare/plugin-v2.16.0...plugin-v2.16.1) (2026-08-13)


### Dependencies

* **plugin:** bump @types/node from 26.1.1 to 26.2.0 in /plugin in the npm-minor-patch group across 1 directory ([#688](https://github.com/caura-ai/caura/issues/688)) ([a40ca4a](https://github.com/caura-ai/caura/commit/a40ca4ab6b7e8f2b9cf9f9aa7c5e16af0092c232))


### Documentation

* **write:** document embedding_pending and the strong-mode opt-out ([#706](https://github.com/caura-ai/caura/issues/706)) ([187b5b5](https://github.com/caura-ai/caura/commit/187b5b5465d7659624e13f455e662c9fce67c483))
