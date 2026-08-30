# @caura/sdk

Alias for [`@caura/client`](https://www.npmjs.com/package/@caura/client), the
official TypeScript client for [Caura](https://caura.ai) — governed shared
memory for AI agent fleets.

```bash
npm install @caura/sdk
```

```ts
import { Caura } from '@caura/sdk';
```

`@caura/client` is the canonical name. This alias exists so that install
instructions pointing at `@caura/sdk` resolve instead of 404ing, and it depends
directly on the canonical implementation rather than chaining through another
alias.
