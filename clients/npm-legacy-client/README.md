# @caura/memclaw-client <!-- legacy-name-ok: permanent compatibility alias -->

This package preserves the original published install name. The implementation
now ships in [`@caura/client`](https://www.npmjs.com/package/@caura/client),
which this package re-exports in full.

```bash
npm install @caura/memclaw-client
```

```ts
import { Caura } from "@caura/memclaw-client";
```

Versions 1.0.0 and 1.0.1 contained the implementation directly and remain
available for existing lockfiles. Starting with 1.0.2, this package is the
forwarding alias, so consumers keep the same API while new development uses
the canonical Caura name.

## License

Apache-2.0
