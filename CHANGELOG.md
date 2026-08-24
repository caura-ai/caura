# Changelog

All notable changes to Caura are documented in this file. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Releases are produced by [release-please](https://github.com/googleapis/release-please-action)
from [Conventional Commits](https://www.conventionalcommits.org/).

Only the most recent releases are listed below. Every release, including those
no longer shown here, is published with its full notes at
[github.com/caura-ai/caura/releases](https://github.com/caura-ai/caura/releases).

## [2.30.0](https://github.com/caura-ai/caura/compare/backend-v2.29.0...backend-v2.30.0) (2026-08-23)


### Features

* **env:** read CAURA_* everywhere the old names are read ([#886](https://github.com/caura-ai/caura/issues/886)) ([74b8a07](https://github.com/caura-ai/caura/commit/74b8a07386cbd2338c4816c0a0eeb049c7d2bb6c))
* **installers:** write CAURA_* into new installs ([#895](https://github.com/caura-ai/caura/issues/895)) ([7a091f3](https://github.com/caura-ai/caura/commit/7a091f30a74b6d4c98affc63993724791c5752f4))
* **llm:** give the retry loop a deadline it can actually enforce ([#862](https://github.com/caura-ai/caura/issues/862)) ([b184c28](https://github.com/caura-ai/caura/commit/b184c280491375494c24df995f07cdc1792eeb88))
* **llm:** honour Retry-After, or hand the call to the fallback provider ([#861](https://github.com/caura-ai/caura/issues/861)) ([bdb20a2](https://github.com/caura-ai/caura/commit/bdb20a24d2479b7e7edc04e84e09625fcb71aa3c))
* **storage:** one live memory per (tenant, fleet, agent, content_hash) ([#842](https://github.com/caura-ai/caura/issues/842)) ([f718790](https://github.com/caura-ai/caura/commit/f718790bf6f7751854090b6baa86fe2118ba42d7))


### Bug Fixes

* **api:** the read params the plugin advertises must reach the query ([#846](https://github.com/caura-ai/caura/issues/846)) ([4acc306](https://github.com/caura-ai/caura/commit/4acc306db3e9ff4d2f9c75061719f4f21f724e6e))
* **auto-chunk:** the deferred parent must be completed, not abandoned ([#860](https://github.com/caura-ai/caura/issues/860)) ([384ecd8](https://github.com/caura-ai/caura/commit/384ecd8f80f49ec143c2b854641f9c75db91421a))
* **consumer:** route the embed/enrich back-channel read-backs to the writer ([#838](https://github.com/caura-ai/caura/issues/838)) ([4f95b6d](https://github.com/caura-ai/caura/commit/4f95b6dc5d55cca10ba945cc22441abaf0c5ea69)), closes [#812](https://github.com/caura-ai/caura/issues/812)
* **contradictions:** a detection lock must not outlive the run it guards ([#849](https://github.com/caura-ai/caura/issues/849)) ([3559f82](https://github.com/caura-ai/caura/commit/3559f82348b5231fe3585ea47cef4c66e87ea4aa))
* **crystallizer:** a crashed run must not disable crystallization forever ([#843](https://github.com/caura-ai/caura/issues/843)) ([5c6ddff](https://github.com/caura-ai/caura/commit/5c6ddff7b4a78f24551df2395a34ad1ad28ddd9b))
* **embedding:** a full backend must be told once, not six times ([#854](https://github.com/caura-ai/caura/issues/854)) ([e1732c1](https://github.com/caura-ai/caura/commit/e1732c1f2299d0eb5d11eafeaf1eed62f3e42e0f))
* **embedding:** a gate timeout is capacity, not a provider failure ([#850](https://github.com/caura-ai/caura/issues/850)) ([14e27bf](https://github.com/caura-ai/caura/commit/14e27bf9736c5126a2ebfc162236bc14aab3cf42))
* **embedding:** blank text is a caller error, not a backend outage ([#851](https://github.com/caura-ai/caura/issues/851)) ([70a357f](https://github.com/caura-ai/caura/commit/70a357f7a8def950f504a4067b10932563d459cd))
* **embedding:** the concurrency gate must cover every caller ([#848](https://github.com/caura-ai/caura/issues/848)) ([9bf6243](https://github.com/caura-ai/caura/commit/9bf624303f7389da25919e7748bfc2099dfb632f))
* **gates:** correct a stale summary and two latent bugs in the shared checks ([#889](https://github.com/caura-ai/caura/issues/889)) ([a27ad71](https://github.com/caura-ai/caura/commit/a27ad716c9cdb58c56747d1aec6cc1231e364df0))
* **governance:** a dropped memory must not leave its children behind ([#853](https://github.com/caura-ai/caura/issues/853)) ([1773596](https://github.com/caura-ai/caura/commit/1773596b8715e485a4c0d74478e537983e87fd6d))
* **governance:** the auto-chunk branch must apply the verdict it computes ([#857](https://github.com/caura-ai/caura/issues/857)) ([d111edb](https://github.com/caura-ai/caura/commit/d111edb8e6748263e9750ddf341bdd4187d09282))
* **lifecycle:** a tick broken by a wiring bug must not report success ([#837](https://github.com/caura-ai/caura/issues/837)) ([a1a9bf5](https://github.com/caura-ai/caura/commit/a1a9bf53fd2df27567846738499088206e028bb6)), closes [#818](https://github.com/caura-ai/caura/issues/818)
* **llm:** one attempt must be one request ([#859](https://github.com/caura-ai/caura/issues/859)) ([58e62ae](https://github.com/caura-ai/caura/commit/58e62ae4b995aa0eaee25ea8626a9ec7de3a4cb5))
* **ratchet:** a line moved between two files is not a new name ([#885](https://github.com/caura-ai/caura/issues/885)) ([8034a0d](https://github.com/caura-ai/caura/commit/8034a0d03aa98fc490aea38b25002add3fbbcee3))
* **ratchet:** name the line the change added, and group repeated exemptions ([#888](https://github.com/caura-ai/caura/issues/888)) ([f490c42](https://github.com/caura-ai/caura/commit/f490c4225c8b1724c092bbeee919e661e9cc776b))
* **sdk:** read recall memories from the key the server actually sends ([#835](https://github.com/caura-ai/caura/issues/835)) ([fa91932](https://github.com/caura-ai/caura/commit/fa91932d2f55e0a9e7da4d2089baac0c1d8287a2)), closes [#811](https://github.com/caura-ai/caura/issues/811)
* **sentinel:** a file the gate cannot read exits 2, not a traceback ([#891](https://github.com/caura-ai/caura/issues/891)) ([8089c9d](https://github.com/caura-ai/caura/commit/8089c9d8be1bc1ff66366e1377b4cd2d57703d12))
* **sentinel:** the log message is the first argument, not any of them ([#893](https://github.com/caura-ai/caura/issues/893)) ([609439c](https://github.com/caura-ai/caura/commit/609439c56f56082f2c0c78c9765c345e1a85aee3))
* **storage:** duplicate content hashes must 409, not 500 forever ([#839](https://github.com/caura-ai/caura/issues/839)) ([2dbab05](https://github.com/caura-ai/caura/commit/2dbab0590f9fc2b77ec028cfa7f4fffacfc3fdd3)), closes [#814](https://github.com/caura-ai/caura/issues/814)
* **storage:** org hard-purge must reach every tenant-scoped table ([#844](https://github.com/caura-ai/caura/issues/844)) ([548dcec](https://github.com/caura-ai/caura/commit/548dcec9918ca0649850412a8a9f0e285d2934b1))
* **write:** a committed row must not be abandoned by a failed entity link ([#840](https://github.com/caura-ai/caura/issues/840)) ([bc36e28](https://github.com/caura-ai/caura/commit/bc36e287fc13361e09fd55b21b668a9a9348605d)), closes [#815](https://github.com/caura-ai/caura/issues/815)
* **write:** the server-internal write paths must consult a dedup lookup ([#841](https://github.com/caura-ai/caura/issues/841)) ([1346007](https://github.com/caura-ai/caura/commit/13460074a33d97f01b39d24ca2b5b2475a5a3717))


### Dependencies

* **actions:** bump the actions group across 1 directory with 3 updates ([#867](https://github.com/caura-ai/caura/issues/867)) ([cabb31c](https://github.com/caura-ai/caura/commit/cabb31c485bac8428364708a7df76bae90c4c77b))
* **actions:** bump the actions-majors group across 1 directory with 3 updates ([#790](https://github.com/caura-ai/caura/issues/790)) ([717d1f3](https://github.com/caura-ai/caura/commit/717d1f3a4fa9341725e82d1cc07ac1facc588b57))
* update cachetools requirement from &gt;=7.1.4 to &gt;=7.1.7 ([#741](https://github.com/caura-ai/caura/issues/741)) ([675b0e5](https://github.com/caura-ai/caura/commit/675b0e5af7ffaaa49654a980caec31b5bbf77f00))
* update croniter requirement from &gt;=2.0 to &gt;=6.2.4 ([#636](https://github.com/caura-ai/caura/issues/636)) ([b1657a4](https://github.com/caura-ai/caura/commit/b1657a4f2f60b42df0ff7a0b0f61a9f1a4d8e7bc))
* update redis requirement from &lt;6,&gt;=5.0 to &gt;=8.1.0,&lt;9 ([#742](https://github.com/caura-ai/caura/issues/742)) ([0fbf436](https://github.com/caura-ai/caura/commit/0fbf436ade987abf211bfb33e28a76a645a1896f))
* update requests requirement from &gt;=2.31 to &gt;=2.34.2 ([#637](https://github.com/caura-ai/caura/issues/637)) ([1386980](https://github.com/caura-ai/caura/commit/1386980caa5ea89fd52973c55f3e273cf4cdf7b8))
* update structlog requirement from &lt;26,&gt;=25.4 to &gt;=26.1.0,&lt;27 ([#633](https://github.com/caura-ai/caura/issues/633)) ([b65ecbe](https://github.com/caura-ai/caura/commit/b65ecbe17605f2a80af319028e703e5e36c0ebc8))


### Documentation

* four README references that point at things which do not exist ([#877](https://github.com/caura-ai/caura/issues/877)) ([20a59e0](https://github.com/caura-ai/caura/commit/20a59e0d32bfe1065bffe72e934422b7e87e6688))
* **llm:** say why the Vertex and Gemini SDK imports are deferred, and enforce it ([#864](https://github.com/caura-ai/caura/issues/864)) ([c159bf1](https://github.com/caura-ai/caura/commit/c159bf1d58f1bab564c355e83f9a45cdd7bc6e7c))
* stop promising 'npm install caura' — npm blocks the bare name ([#876](https://github.com/caura-ai/caura/issues/876)) ([1e83e80](https://github.com/caura-ai/caura/commit/1e83e8056c065aa43b051a6c8a4ffa08a7219f18))
* the same broken cd in the setup docs [#877](https://github.com/caura-ai/caura/issues/877) fixes in the README ([#878](https://github.com/caura-ai/caura/issues/878)) ([8d66a9c](https://github.com/caura-ai/caura/commit/8d66a9c458bc01b23a358cc264544821df999b08))
* the two things the README still gets wrong after [#877](https://github.com/caura-ai/caura/issues/877) and [#878](https://github.com/caura-ai/caura/issues/878) ([858db67](https://github.com/caura-ai/caura/commit/858db673265a617ac39a3e3c689d376b20265f1a))
* the two things the README still gets wrong after [#877](https://github.com/caura-ai/caura/issues/877) and [#878](https://github.com/caura-ai/caura/issues/878) ([#879](https://github.com/caura-ai/caura/issues/879)) ([858db67](https://github.com/caura-ai/caura/commit/858db673265a617ac39a3e3c689d376b20265f1a))

## [2.29.0](https://github.com/caura-ai/caura/compare/backend-v2.28.0...backend-v2.29.0) (2026-08-19)


### Features

* **api:** type the memory-get 200 body, so the frozen contract actually pins it ([#778](https://github.com/caura-ai/caura/issues/778)) ([f6e66bc](https://github.com/caura-ai/caura/commit/f6e66bc2bc93827afcbdb34982372ab82a25c490))
* **db:** documents.created_at / updated_at NOT NULL ([#827](https://github.com/caura-ai/caura/issues/827)) ([79ccad8](https://github.com/caura-ai/caura/commit/79ccad843637c3029d3d3f9b0fc3adfdd6d6955a))
* **embeddings:** make stale vectors detectable via embedding provenance ([#786](https://github.com/caura-ai/caura/issues/786)) ([a98fb13](https://github.com/caura-ai/caura/commit/a98fb137d1104847b64c202f21d8a02e8f26a4fe))
* **llm:** say so when the fallback provider tier is skipped ([#805](https://github.com/caura-ai/caura/issues/805)) ([1bba410](https://github.com/caura-ai/caura/commit/1bba4108daa1723f222f9a5e2e322fd030ee2b18))
* **usage:** durable per-tenant counters behind the usage meter ([#828](https://github.com/caura-ai/caura/issues/828)) ([ad55757](https://github.com/caura-ai/caura/commit/ad557578838efe9c9b3d6091d9e7dc8442b15195))
* **usage:** read half of tenant_usage_counters for the platform ([#829](https://github.com/caura-ai/caura/issues/829)) ([39f1f89](https://github.com/caura-ai/caura/commit/39f1f8997792ba032b8d2bf58da276c8afa5e9fd))
* **usage:** route usage metering through a service hook ([#824](https://github.com/caura-ai/caura/issues/824)) ([3beaf71](https://github.com/caura-ai/caura/commit/3beaf7199bff97bd4eef31777e5b0a4a700cc21b))


### Bug Fixes

* **contradiction:** abstain instead of guessing when no LLM answered ([#821](https://github.com/caura-ai/caura/issues/821)) ([3ab567b](https://github.com/caura-ai/caura/commit/3ab567b6c14c2d47864b6e8a9913d238c37af3b7))
* **core-api:** add the missing capability gates on three write routes ([#799](https://github.com/caura-ai/caura/issues/799)) ([e272e17](https://github.com/caura-ai/caura/commit/e272e17879dc71ffe0559a0ecb1453b7467ec2b3))
* **core-api:** apply LLM governance on the inline fast-write path ([#806](https://github.com/caura-ai/caura/issues/806)) ([7fb43d7](https://github.com/caura-ai/caura/commit/7fb43d7d6a701bdfb4fe05e9b373566a76edd4e3))
* **core-api:** bind read-path identity to the authenticated agent ([#801](https://github.com/caura-ai/caura/issues/801)) ([9beab13](https://github.com/caura-ai/caura/commit/9beab138008a489d2ad1475455f85008e372e94b))
* **core-api:** make /stm/promote pay the LTM write gates ([#804](https://github.com/caura-ai/caura/issues/804)) ([db7b297](https://github.com/caura-ai/caura/commit/db7b2976b61050c6bbbd1ccad3064fc74c815ee2))
* **core-api:** require GATEWAY_SHARED_SECRET in production ([#802](https://github.com/caura-ai/caura/issues/802)) ([32143cb](https://github.com/caura-ai/caura/commit/32143cb50b46f606f555750ed5bf36777bbc74dd))
* **documents:** a NULL timestamp must serialise, not 500 the read ([#826](https://github.com/caura-ai/caura/issues/826)) ([8ccc3f3](https://github.com/caura-ai/caura/commit/8ccc3f3c1ce7bd91c9f46cbe8cb55b438e5e2006))
* **embedding:** reserve capacity so write bursts can't starve recall ([#830](https://github.com/caura-ai/caura/issues/830)) ([082864e](https://github.com/caura-ai/caura/commit/082864e5c25cd1a9bd9b63735b03fe339aaa8e29))
* **enrichment:** discard a junk title instead of persisting its repr ([#798](https://github.com/caura-ai/caura/issues/798)) ([2a4bd6f](https://github.com/caura-ai/caura/commit/2a4bd6f04c3327d3cdc570738b5dc3e7754b818e))
* **enrichment:** stop dropping atomic facts when their embedding fails ([#792](https://github.com/caura-ai/caura/issues/792)) ([6fd44af](https://github.com/caura-ai/caura/commit/6fd44afbdd623b80366f910b0e11d86f40bbfe3e))
* **enrichment:** tolerate a junk summary or pii_types ([#795](https://github.com/caura-ai/caura/issues/795)) ([3873b2c](https://github.com/caura-ai/caura/commit/3873b2cd6c16530bc77401b12332f6d526882c52))
* **evolve:** a disabled or failed provider must not fabricate a rule ([#825](https://github.com/caura-ai/caura/issues/825)) ([621cb22](https://github.com/caura-ai/caura/commit/621cb226f108d1182bae8af7995db5a6beb9f027))
* **extraction:** drop malformed items instead of the whole extraction ([#794](https://github.com/caura-ai/caura/issues/794)) ([863ca93](https://github.com/caura-ai/caura/commit/863ca93828899a1dfb4314f53c373b3c97f186ae))
* **extraction:** stop the regex heuristic guessing entity_type=person ([#807](https://github.com/caura-ai/caura/issues/807)) ([acd5c3b](https://github.com/caura-ai/caura/commit/acd5c3b944dd6b6e5434ceac296ec3e739ee8ae6))
* **extraction:** tolerate a null cluster_id instead of losing the whole graph ([#788](https://github.com/caura-ai/caura/issues/788)) ([0861f4f](https://github.com/caura-ai/caura/commit/0861f4fd7a9455f421b22fefcf210580d62a369d))
* **fallbacks:** a no-LLM stand-in must not retire the data it replaces ([#822](https://github.com/caura-ai/caura/issues/822)) ([845cfec](https://github.com/caura-ai/caura/commit/845cfec7790a9fc1d291860d2295d6fb450155d0))
* **forge:** give the cron's poison checker the shape the distill seam calls ([#833](https://github.com/caura-ai/caura/issues/833)) ([057df31](https://github.com/caura-ai/caura/commit/057df3115847f27abbaaac3cf49e4072c16cfd9b)), closes [#818](https://github.com/caura-ai/caura/issues/818)
* **forge:** make a wiring bug fail CI, and stop it reading as storage trouble ([#834](https://github.com/caura-ai/caura/issues/834)) ([a6d5731](https://github.com/caura-ai/caura/commit/a6d57313b2dd8229346a41356738c633cf984c9e)), closes [#818](https://github.com/caura-ai/caura/issues/818)
* **insights:** pin findings to status=active and sweep pending zombies ([#797](https://github.com/caura-ai/caura/issues/797)) ([9af95e1](https://github.com/caura-ai/caura/commit/9af95e1f5e84dd824b6d6633cea07cc6442aacea))
* **recall:** label the no-LLM summary as unsynthesized ([#823](https://github.com/caura-ai/caura/issues/823)) ([7e354e2](https://github.com/caura-ai/caura/commit/7e354e24828f346da751aaaa707cd1bd2ca57b16))
* **search:** stop entity_lookup answering a query it cannot fill ([#832](https://github.com/caura-ai/caura/issues/832)) ([9f97bff](https://github.com/caura-ai/caura/commit/9f97bffb40b9e82eba6f8c05a83132d03b4755b4)), closes [#813](https://github.com/caura-ai/caura/issues/813)


### Documentation

* **core-api:** record the two invariants [#806](https://github.com/caura-ai/caura/issues/806)'s review surfaced ([#810](https://github.com/caura-ai/caura/issues/810)) ([a6c6547](https://github.com/caura-ai/caura/commit/a6c6547a9da2bafcd6b5f80e22c128a776e88b66))
* fix stale brand references in the README and client packages ([#784](https://github.com/caura-ai/caura/issues/784)) ([b002521](https://github.com/caura-ai/caura/commit/b002521237a2247ac7ad316e5210c6da4dd40477))

## [2.28.0](https://github.com/caura-ai/caura/compare/backend-v2.27.0...backend-v2.28.0) (2026-08-13)


### Features

* **bench:** first-stage blend A/B on LoCoMo, and share the harness plumbing ([#714](https://github.com/caura-ai/caura/issues/714)) ([f4280d7](https://github.com/caura-ai/caura/commit/f4280d73774d607aef2c9cc214f78792d0d28df9))
* **bench:** LoCoMo rerank A/B harness ([#712](https://github.com/caura-ai/caura/issues/712)) ([9369e6e](https://github.com/caura-ai/caura/commit/9369e6e8ca9f69a7cc40daf91cb88ad9ce3fa3d4))
* **broker:** gate the four memory operations the broker already calls ([#753](https://github.com/caura-ai/caura/issues/753)) ([c974bb4](https://github.com/caura-ai/caura/commit/c974bb4736b201779650ee37a0ee23a3f172caf4))
* **embeddings:** nightly sweep that re-embeds NULL-embedding rows ([#767](https://github.com/caura-ai/caura/issues/767)) ([780e652](https://github.com/caura-ai/caura/commit/780e652fa0ea24e522c479e1c660672c037f2939))
* **memory:** conflict-record write machinery — storage + resolver (A55 1d) ([#764](https://github.com/caura-ai/caura/issues/764)) ([14f1b2b](https://github.com/caura-ai/caura/commit/14f1b2b90ebab0cc2bcbb51359462ebfb64fcc30))
* **memory:** contradiction classification logic — L1/L2/L3 (A55 1d) ([#759](https://github.com/caura-ai/caura/issues/759)) ([9b9d3ae](https://github.com/caura-ai/caura/commit/9b9d3aea0eb07b23840271c6ac9768f282cb7b56))
* **memory:** contradiction engine seam + arch flag (A55 Phase 1) ([#754](https://github.com/caura-ai/caura/issues/754)) ([541cc05](https://github.com/caura-ai/caura/commit/541cc050d2fc7e3b64c62cb3a29fd50f35642c0b))
* **memory:** unified contradiction-model schema + read serialization (A55) ([#752](https://github.com/caura-ai/caura/issues/752)) ([e601021](https://github.com/caura-ai/caura/commit/e6010219148eb3a12d305c8ee27a795e28737ae9))
* **memory:** wire conflict-record write into the detector, flag-gated (A55 1d) ([#766](https://github.com/caura-ai/caura/issues/766)) ([fab52e4](https://github.com/caura-ai/caura/commit/fab52e4b98a7c18ee98d94f32e8994aa58d752fe))
* **observability:** make the unembedded-row backlog measurable ([#776](https://github.com/caura-ai/caura/issues/776)) ([af9e2fb](https://github.com/caura-ai/caura/commit/af9e2fb7aa3d8f1719893ed51cf0cce3051b5083))
* **search:** index memories.title, at the same weight as content ([#731](https://github.com/caura-ai/caura/issues/731)) ([621afea](https://github.com/caura-ai/caura/commit/621afea7c32e204175a80694de2a536412ce1675))
* **write:** honour per-item write_mode on the bulk path ([#710](https://github.com/caura-ai/caura/issues/710)) ([6ab8f2d](https://github.com/caura-ai/caura/commit/6ab8f2d26dff92227053ec5c4970cfbf6904eb06))


### Bug Fixes

* **capture:** mirror the shared pipeline's diagnostics, effort and decline test ([#755](https://github.com/caura-ai/caura/issues/755)) ([5c0f7f6](https://github.com/caura-ai/caura/commit/5c0f7f6898cddf8760f6a7ac4591ca06e950e7c6))
* **crystallizer:** match the /lifecycle-candidates contract; drop inline remediation ([#765](https://github.com/caura-ai/caura/issues/765)) ([154350b](https://github.com/caura-ai/caura/commit/154350b62ae735e4130c45dbef006d911332566d))
* **crystallizer:** read the embedding-coverage keys the endpoint returns ([#763](https://github.com/caura-ai/caura/issues/763)) ([7219d80](https://github.com/caura-ai/caura/commit/7219d80d0c461daa8d5296e9447585260a04d0af))
* **embedding:** chunk bulk embeds to the backend's batch cap, and report bulk-only outages ([#721](https://github.com/caura-ai/caura/issues/721)) ([d812a9d](https://github.com/caura-ai/caura/commit/d812a9db7903fe531b4d246cda7153b198175315))
* **embedding:** key degraded-provider stats per backend, not per process ([#726](https://github.com/caura-ai/caura/issues/726)) ([048e2e1](https://github.com/caura-ai/caura/commit/048e2e19f6c79f1945fcf4ff2f3889d93be0fa56))
* **embedding:** let bulk callers pass their budget so a slow provider is attributable ([#724](https://github.com/caura-ai/caura/issues/724)) ([052d700](https://github.com/caura-ai/caura/commit/052d700755e97a6012b127a9a0b58216ddfa65c0))
* **embedding:** set per-phase httpx timeouts, matching the LLM client ([#761](https://github.com/caura-ai/caura/issues/761)) ([ef8be4b](https://github.com/caura-ai/caura/commit/ef8be4b2a5fa6a0ba56558e6db70334e7480af6f))
* **events:** declare the embed-backfill subscription in the manifest ([#772](https://github.com/caura-ai/caura/issues/772)) ([c5e0543](https://github.com/caura-ai/caura/commit/c5e0543ea0c7d091525b1c1d352baf5e2e76d3d9))
* **memory:** null the vector when a content-change re-embed fails ([#775](https://github.com/caura-ai/caura/issues/775)) ([4c87f85](https://github.com/caura-ai/caura/commit/4c87f85b15f26fead20ebd7a02985546bdd6d110))
* **memory:** unshadow HTTPException so pipeline failures surface as 500 ([#760](https://github.com/caura-ai/caura/issues/760)) ([077f30a](https://github.com/caura-ai/caura/commit/077f30a42a9e23e268911a1065eb36bf7eb88a03))
* **reports:** anchor working-on lane keywords to word starts ([#773](https://github.com/caura-ai/caura/issues/773)) ([e4a695e](https://github.com/caura-ai/caura/commit/e4a695e68fd24dee2c64b82dd14d6e33aa3cdbde))
* **rerank:** chunk the remote candidate pool so the sidecar's cap can't reject it ([#709](https://github.com/caura-ai/caura/issues/709)) ([c987649](https://github.com/caura-ai/caura/commit/c9876491066c8e7778a93a5b054020331676fc2f))
* **rerank:** distinguish permanent config faults from transient failures ([#708](https://github.com/caura-ai/caura/issues/708)) ([ce3fc51](https://github.com/caura-ai/caura/commit/ce3fc514e4bd5bfffe2536c77dc9faac2fc7a199))
* **search:** declare the scored-search wire contract once, in common/ ([#727](https://github.com/caura-ai/caura/issues/727)) ([6483a39](https://github.com/caura-ai/caura/commit/6483a39a11857d4cae2807d2e8c0644951e69f93))
* **search:** deliver every scoring knob to the SQL on both search paths ([#723](https://github.com/caura-ai/caura/issues/723)) ([9590c4c](https://github.com/caura-ai/caura/commit/9590c4ce826d9c47f31d05953455d6909db79db8))
* **search:** let SEARCH_OVERFETCH_FACTOR reach the SQL LIMIT ([#725](https://github.com/caura-ai/caura/issues/725)) ([f94572a](https://github.com/caura-ai/caura/commit/f94572a2687110c82a43368c92b46ccf68e1b6ec))
* **search:** move 034's backfill out of the startup path ([#732](https://github.com/caura-ai/caura/issues/732)) ([9fe4557](https://github.com/caura-ai/caura/commit/9fe4557c0b138c214936b5d532b3f4350456d4ee))
* **search:** put fts_score on the cosine scale ([#687](https://github.com/caura-ai/caura/issues/687)) ([#722](https://github.com/caura-ai/caura/issues/722)) ([4f0eb2b](https://github.com/caura-ai/caura/commit/4f0eb2b13942206c7baa33d5b3eec256e581f066))
* **search:** reserve candidate and result slots for FTS-only rows ([#687](https://github.com/caura-ai/caura/issues/687)) ([#700](https://github.com/caura-ai/caura/issues/700)) ([4ae972b](https://github.com/caura-ai/caura/commit/4ae972b9e2502e02505e036cf4c6b93e0e7e3079))
* **search:** settle graph_max_hops at 3 across every surface ([#730](https://github.com/caura-ai/caura/issues/730)) ([0f8d4c8](https://github.com/caura-ai/caura/commit/0f8d4c8ae1b3e15add572d92b0888f05063c9f43))
* **search:** ship the 034 backfill inside the image ([#734](https://github.com/caura-ai/caura/issues/734)) ([e78cd29](https://github.com/caura-ai/caura/commit/e78cd293b5ba572c7cea3b0efac49d6a808d8b34))


### Performance

* **contradiction:** batch the Path C entity-aware per-candidate LLM judge (A61) ([#771](https://github.com/caura-ai/caura/issues/771)) ([0e4c9f8](https://github.com/caura-ai/caura/commit/0e4c9f8009a84707aa97f12c280c303493db36fa))
* **contradiction:** batch the semantic per-candidate LLM judge (A61) ([#770](https://github.com/caura-ai/caura/issues/770)) ([86c24c8](https://github.com/caura-ai/caura/commit/86c24c872f63d4e97dbf6a2948b1616ca29830c0))
* **db:** index the FK columns that reference memories.id / entities.id ([#751](https://github.com/caura-ai/caura/issues/751)) ([160f271](https://github.com/caura-ai/caura/commit/160f271ebda845b591c6f993cf5b5de39ddfbb28))


### Dependencies

* **actions:** bump actions/setup-python from 5 to 7 ([#640](https://github.com/caura-ai/caura/issues/640)) ([0dee432](https://github.com/caura-ai/caura/commit/0dee4323ed0ef64c1e9b43e7c4c4825fecc7beaf))
* **actions:** bump googleapis/release-please-action from 4.4.1 to 5.0.0 ([#639](https://github.com/caura-ai/caura/issues/639)) ([93a5bbb](https://github.com/caura-ai/caura/commit/93a5bbb8a9a19c39a0f375a19af4170404b2e30f))
* **actions:** bump the actions group across 1 directory with 3 updates ([#638](https://github.com/caura-ai/caura/issues/638)) ([408f23b](https://github.com/caura-ai/caura/commit/408f23bc3da16b07147fdc720463e1fad14b5a51))
* bump the uv-minor-patch group across 4 directories with 11 updates ([#747](https://github.com/caura-ai/caura/issues/747)) ([a72a5d3](https://github.com/caura-ai/caura/commit/a72a5d3b251735eb2a91c3036577fb80bf5bdcd8))
* **plugin:** bump @types/node from 26.1.1 to 26.2.0 in /plugin in the npm-minor-patch group across 1 directory ([#688](https://github.com/caura-ai/caura/issues/688)) ([a40ca4a](https://github.com/caura-ai/caura/commit/a40ca4ab6b7e8f2b9cf9f9aa7c5e16af0092c232))


### Documentation

* **write:** document embedding_pending and the strong-mode opt-out ([#706](https://github.com/caura-ai/caura/issues/706)) ([187b5b5](https://github.com/caura-ai/caura/commit/187b5b5465d7659624e13f455e662c9fce67c483))


### Code Refactoring

* **search:** derive SearchProfileUpdate from the knob table ([#736](https://github.com/caura-ai/caura/issues/736)) ([a5a55f2](https://github.com/caura-ai/caura/commit/a5a55f2e529c6d1bef682765a3f669e9c20c388b))
* **search:** one knob table instead of four registration points ([#728](https://github.com/caura-ai/caura/issues/728)) ([ae56d08](https://github.com/caura-ai/caura/commit/ae56d08affb7b942e52166752d1e1077fbcc1ef2))
* **search:** one resolver for both search paths, and A47 reaches legacy ([#729](https://github.com/caura-ai/caura/issues/729)) ([724f4e8](https://github.com/caura-ai/caura/commit/724f4e878458e6024d7b061c1f55f959c8278eac))
