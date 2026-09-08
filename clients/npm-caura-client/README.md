# caura-client

Unscoped alias for [`@caura/client`](https://www.npmjs.com/package/@caura/client),
the official TypeScript client for [Caura](https://caura.ai) — governed shared
memory for AI agent fleets.

```bash
npm install caura-client
```

```ts
import { Caura } from 'caura-client';
```

`@caura/client` is the canonical name. This alias exists because the Python
client is installed as `pip install caura-client` on PyPI, so
`npm install caura-client` is a name people will guess — and unlike a scoped npm
name, an unscoped one is publishable by any third party. Owning it keeps that
install line resolving to the canonical implementation instead of to whoever
claims it first. It depends directly on `@caura/client` rather than chaining
through another alias.

The bare name `caura` is not publishable on npm at all: the registry's
name-similarity filter blocks it (against `csurf`) for every publisher, with
no exception process — npm support confirmed 2026-08-28. That applies to
everyone equally, so the bare name needs no defensive registration.
