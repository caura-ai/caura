# @caura/client

The official TypeScript client for [Caura](https://caura.ai) — governed shared
memory for AI agent fleets.

```bash
npm install @caura/client
```

```ts
import { Caura } from "@caura/client";
```

## Why this is a thin package

The implementation lives in
[`@caura/memclaw-client`](https://www.npmjs.com/package/@caura/memclaw-client),
which this re-exports in full. That package keeps its name permanently: it has
installed users, and renaming a published package strands everyone who depends
on it. `@caura/client` is the canonical name going forward; the older one keeps
resolving forever.

The same split exists on PyPI, where `caura` is a metapackage for
`caura-client`.

## Not `npm install caura`

The unscoped name is unavailable. npm's registry rejects it as too similar to
`csurf`, a long-established package, and that rejection is not appealable in
practice — the scoped name is the supported route rather than a workaround.
