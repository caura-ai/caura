# Caura — governed shared memory for AI agent fleets

Caura (formerly MemClaw) is an Agent DB — governed shared memory for AI agent fleets. Agents commit what they learn once; every agent in the fleet recalls it through MCP tools, REST, or Caura Rail (preview), subject to tenant isolation, visibility scope (scope_agent / scope_team / scope_org) and caller trust level. <!-- legacy-name-floor: taught as the former name -->

## This package is a redirect

`memclaw-client` was the name of the Python client before MemClaw became Caura. From 0.5.0 it is an empty shell that installs [`caura-client`](https://pypi.org/project/caura-client/), the official Python client, and nothing else: there is no `memclaw_client` module and no `MemClaw` class. <!-- legacy-name-floor: the retired name this page redirects from -->

If an install instruction still says `pip install memclaw-client`, change it to: <!-- legacy-name-floor: the retired install command being corrected -->

```bash
pip install caura-client
```

and change the import to:

```python
from caura_client import Caura
```

The name is kept on PyPI so that old instructions resolve to Caura rather than
to whoever might claim a freed name. Nothing new will ever ship under it.

## Links

- What Caura is: https://caura.ai/docs
- Canonical package: [caura-client](https://pypi.org/project/caura-client/)
- Source: https://github.com/caura-ai/caura (Apache-2.0)
- Issues: https://github.com/caura-ai/caura/issues
- Benchmark: https://github.com/caura-ai/caura-longmemeval (LongMemEval harness)
