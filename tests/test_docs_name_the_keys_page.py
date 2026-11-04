"""The docs name the keys page the app has, and what it makes (Prism App Review v2 R9).

The dashboard's API Credentials page is now Connect > Keys, and it makes only
tenant-scoped keys: members see no key forms, and the admin form makes API keys
only. So no one finds a "Create agent key" form there. An agent key, which
``caura-bus setup`` needs, comes from caura.ai's provisioning route and needs
the Manage API credentials permission. Closes N075, N076 and N079; N077 is pinned in
``clients/collaboration/tests/test_join_command.py``, and #2170 closed N078
(``test_key_remediation_names_who_makes_keys.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[1]
COLLABORATION_DOCS = (
    "clients/collaboration/README.md",
    "docs/agent-collaboration/AGENT_COLLABORATION.md",
)
WHO_MINTS = (
    "On caura.ai, someone with the **Manage API credentials** permission mints one with "
    "`POST /api/v1/admin/agent-keys/provision`"
)
TENANT_KEYS_ONLY = (
    "**Connect \u203a Keys** in the Caura app makes only tenant-scoped keys"
)


def _read(path: str) -> str:
    return " ".join((REPO / path).read_text().split())


def test_readme_offers_no_agent_key_form_on_connect_keys() -> None:
    readme = _read("README.md")
    assert "**Create agent key** under Connect \u203a Keys" not in readme
    assert TENANT_KEYS_ONLY in readme
    assert (
        "It needs the **Manage API credentials** permission; without it, ask an organization admin."
        in readme
    )


@pytest.mark.parametrize("path", COLLABORATION_DOCS)
def test_collaboration_docs_name_no_api_credentials_page(path: str) -> None:
    assert "API Credentials" not in _read(path)


@pytest.mark.parametrize("path", COLLABORATION_DOCS)
def test_collaboration_docs_say_who_mints_an_agent_key(path: str) -> None:
    text = _read(path)
    assert WHO_MINTS in text
    assert TENANT_KEYS_ONLY in text
