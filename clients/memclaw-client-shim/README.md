# memclaw-client → caura-client

MemClaw was renamed **Caura** in 2026. From 0.5.0 this package is an empty
shell that depends on [`caura-client`](https://pypi.org/project/caura-client/).

Nothing breaks: `from memclaw_client import MemClaw` works exactly as before
(permanent alias, same objects). New code should use:

```python
from caura_client import Caura
```
