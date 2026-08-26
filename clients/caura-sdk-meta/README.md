# caura-sdk

Metapackage for [`caura-client`](https://pypi.org/project/caura-client/), the
official Python client for [Caura](https://caura.ai) — governed shared memory
for AI agent fleets.

```bash
pip install caura-sdk
```

```python
from caura_sdk import Caura
```

`caura-sdk`, [`caura`](https://pypi.org/project/caura/) and
[`caura-client`](https://pypi.org/project/caura-client/) all install the same
client. `caura-client` is the canonical name; the other two exist so that
install instructions pointing at them resolve instead of 404ing.
