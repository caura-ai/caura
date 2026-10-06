"""Memory API plus stateless remote peer MCP; REST collaboration runs separately."""

import os

from core_api.app import app as app
from core_api.bus_mcp import register_peer

register_peer(api_url=os.getenv("COLLABORATION_API_URL", "http://collaboration-api:8000"))
