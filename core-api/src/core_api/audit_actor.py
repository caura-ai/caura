"""Who did a write, and through which client: the audit row's actor fields.

Two fields, recorded in ``detail`` on the governance writes listed in
``docs/api-reference.md`` (Audit attribution):

``user_id``
    The person the enterprise gateway vouched for. Its auth subrequest resolves
    the credential and sets ``X-User-ID`` on the proxied request, overwriting
    anything the client sent. Core-api reads it only behind the
    ``X-Gateway-Secret`` check, because only that proves the gateway set it.
    Elsewhere the header is the caller's own claim and is not recorded.

``surface``
    The Caura client the request came from, from the ``X-Caura-Surface``
    header. The client declares it and nothing verifies it, so it is for
    metrics and for labelling the audit row, and is NEVER read by an
    authorization gate. The set is closed: a value outside it is dropped, so
    the row says ``None``. It is not refused, because a client whose label is
    unknown here must not lose its request over it.

Both keys are always present on those rows, ``None`` when unknown. An exporter
can then tell "not known" (``None``) from "written before these fields
existed" (key absent).
"""

# ``X-Caura-Surface``, lowercased: the form raw ASGI headers carry, and one
# Starlette's case-insensitive ``request.headers`` also matches.
SURFACE_HEADER = "x-caura-surface"

# The values a header may name: one per client that sends the audited writes.
# A new client gets a value here and a row in the docs table in the same change;
# until then its value is dropped.
#   dashboard        the enterprise web app, outside /prism
#   prism            the enterprise web app, on /prism
#   broker           caura-daemon, which includes the `caura` CLI and its
#                    `caura mcp-server` (they reach core-api through it)
#   openclaw_plugin  the OpenClaw plugin
SURFACES: frozenset[str] = frozenset({"dashboard", "prism", "broker", "openclaw_plugin"})

# What the MCP plane records. Core-api terminates /mcp itself, so it knows the
# transport and takes no header for it; a REST header cannot claim it.
MCP_SURFACE = "mcp"


def parse_surface(raw: str | None) -> str | None:
    """The allow-listed surface ``raw`` names, else ``None``.

    Case and surrounding whitespace are forgiven. Anything else outside
    ``SURFACES`` is dropped without an error.
    """
    if not raw:
        return None
    value = raw.strip().lower()
    return value if value in SURFACES else None


def actor_detail(user_id: str | None, surface: str | None) -> dict[str, str | None]:
    """The ``user_id`` and ``surface`` keys for an audit row's ``detail``."""
    return {"user_id": user_id, "surface": surface}
