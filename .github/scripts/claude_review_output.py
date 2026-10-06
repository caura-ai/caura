"""Refuse suspected credential output without ever printing the output or key."""

import base64
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def main():
    needles = set()
    for name in ("ANTHROPIC_API_KEY", "GH_TOKEN", "CAURA_AGENTS_KEY", "REVIEW_PROXY_TOKEN"):
        value = os.environ.get(name, "")
        if value:
            needles.update((value, quote(value, safe="")))
            needles.add(base64.b64encode(value.encode()).decode())
            needles.add(value.encode().hex())
    pattern = re.compile(
        r"sk-ant-[A-Za-z0-9_-]{16,}|github_pat_[A-Za-z0-9_]{20,}"
        r"|gh[pousr]_[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    )
    for filename in sys.argv[1:]:
        raw = Path(filename).read_text(errors="replace")
        candidates = [raw]
        try:
            candidates.extend(strings(json.loads(raw)))
        except (ValueError, RecursionError):
            pass
        if any(pattern.search(s) or any(n in s for n in needles) for s in candidates):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
