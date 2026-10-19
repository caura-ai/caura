"""``scripts/caura-join.sh`` installs the client from the ref it is fetched from.

The collaboration README pipes the script from ``main``, and the script then
installs the ``caura-bus`` CLI with ``uv tool install git+...@<ref>``. Its
default ref was the release branch ``release/agent-collaboration-rc``, deleted
once that release merged (M-126): the documented join failed, and whoever could
recreate that branch name would choose the code every later ``curl | sh``
installed. Script, header and README now name one ref.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "scripts" / "caura-join.sh").read_text()
README = (ROOT / "clients" / "collaboration" / "README.md").read_text()
FETCH = re.compile(
    r"raw\.githubusercontent\.com/caura-ai/caura/(\S+?)/scripts/caura-join\.sh"
)


def test_the_script_installs_from_the_ref_it_is_fetched_from() -> None:
    default = re.search(r'ref="\$\{CAURA_JOIN_REF:-([^}]+)\}"', SCRIPT)
    assert default, "caura-join.sh no longer reads CAURA_JOIN_REF with a default"
    readme_refs = set(FETCH.findall(README))
    header_refs = set(FETCH.findall(SCRIPT))
    assert readme_refs == {"main"}, readme_refs
    assert header_refs == readme_refs, header_refs
    assert default.group(1) == "main"
