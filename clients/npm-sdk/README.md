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
on the canonical package rather than re-exporting the implementation directly —
so there is one place to change if the implementation is ever renamed.
