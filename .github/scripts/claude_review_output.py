"""Refuse output that holds one of this job's credentials, without ever printing
the output or the key.

The exact values, with their URL, base64 and hex encodings, are what can leak:
the reviewer's sandbox holds no other credential. Text merely shaped like one
(a key fixture, a scanner's pattern, a PEM header in a test) is reported as a
warning naming the stream, with nothing echoed. Refusing it failed every review
of a pull request that quotes such text."""

import base64
import contextlib
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
    shaped = []
    for filename in sys.argv[1:]:
        raw = Path(filename).read_text(errors="replace")
        candidates = [raw]
        # Not JSON, or nested too deep to walk: the raw text is scanned all the same.
        with contextlib.suppress(ValueError, RecursionError):
            candidates.extend(strings(json.loads(raw)))
        if any(n in s for s in candidates for n in needles):
            return 1
        if any(pattern.search(s) for s in candidates):
            shaped.append(Path(filename).name)
    for name in shaped:
        print(
            f"::warning::Reviewer {name} holds text shaped like a credential that is "
            "none of this job's secrets; not withheld",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
