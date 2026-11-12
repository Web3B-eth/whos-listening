"""The fake hub's memory tools as a stdio MCP server, for the end-to-end checks. Local only.

Hermes starts it from ``mcp_servers`` in the scratch config. Each tool call goes to the fake hub
(fake_hub.py) over HTTP, which decides what the calling chat may see from the call's
``wl_turn``. Needs the ``mcp`` package (Hermes' ``mcp`` extra); run it with Hermes' Python.

Usage: python fake_hub_mcp.py HUB_PORT TOKEN
"""

from __future__ import annotations

import json
import sys
import urllib.request

from mcp.server.mcpserver import MCPServer

PORT, TOKEN = int(sys.argv[1]), sys.argv[2]
server = MCPServer("fake-hub")
TURN = "Leave empty; Hermes fills it in."


def _forward(name: str, args: dict) -> str:
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/tool/{name}", data=json.dumps(args).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())["result"]


@server.tool(description="Search the user's long-term memories. wl_turn: " + TURN)
def memory_search(query: str, wl_turn: str = "") -> str:
    return _forward("memory_search", {"query": query, "wl_turn": wl_turn})


@server.tool(description="Save a lasting fact about the user, in their own words. tier: private or shared. "
                         "wl_turn: " + TURN)
def memory_save(text: str, tier: str = "private", reason: str = "", wl_turn: str = "") -> str:
    return _forward("memory_save", {"text": text, "tier": tier, "reason": reason, "wl_turn": wl_turn})


@server.tool(description="Forget a memory by its number, such as S1. wl_turn: " + TURN)
def memory_forget(ref: str, reason: str = "", wl_turn: str = "") -> str:
    return _forward("memory_forget", {"ref": ref, "reason": reason, "wl_turn": wl_turn})


if __name__ == "__main__":
    server.run("stdio")
