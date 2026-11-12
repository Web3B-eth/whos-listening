"""A small fake hub for hub mode's tests (docs/hub-protocol.md). Local only, made-up memories.

It speaks protocol version 1 and passes the cases in tests/vectors/hub/hub.json, so it doubles as
a reference for what a hub does. It is not a real store: memories live in memory, and the
rules are the simplest that follow the protocol:

- A verdict is Private when the platform and the route are on the trusted lists and every side
  route from ``hello`` (fallbacks, compression) is trusted too. Anything else is Shared.
- ``context`` gives Private memories only on a Private verdict for the believed route.
- A tool call's ``wl_turn`` ties it to a chat. Without a valid one, the call is from an unknown
  chat: Shared memories only, and a save is handled as from a Shared chat after outside content.
- A save in a chat that read outside content waits for the user; otherwise it is saved.

Hub tool results tag Private text with ``<wl-private id>``, ``<`` and ``>`` replaced.

Usage: python fake_hub.py PORT LOGFILE TOKEN [MEMORIES.json]
Calls go to ``http://127.0.0.1:PORT/v1/<call>``; the stdio MCP server in fake_hub_mcp.py sends
tool calls to ``/tool/<name>``. Every call and tool call is logged to LOGFILE as JSON lines.
"""

from __future__ import annotations

import hmac
import json
import re
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import urlsplit

CALLS = ("hello", "turn-start", "context", "verdict", "turn-report")
TOOLS = [
    {"name": "memory_search", "outside_content": False, "redact_args": ["query"]},
    {"name": "memory_save", "outside_content": False, "redact_args": ["text", "reason"]},
    {"name": "memory_forget", "outside_content": False, "redact_args": ["reason"]},
]
TRUSTED_PLATFORMS = ["cli", "tui", "desktop", "local"]
TOKEN_TTL = 3600


def _host(url: str) -> str:
    try:
        return (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def _tag(memory: dict) -> str:
    text = memory["text"].replace("<", "‹").replace(">", "›")
    return f'<wl-private id="{memory["id"]}">{text}</wl-private>' if memory["tier"] == "private" else text


class FakeHub:
    def __init__(self, token: str, memories: list[dict], *, trusted_platforms: Optional[list] = None,
                 trusted_routes: Optional[list] = None, log_path: str = "", clock=time.monotonic):
        self.token = token
        self.memories = [dict(m) for m in memories]
        self.trusted_platforms = list(trusted_platforms if trusted_platforms is not None else TRUSTED_PLATFORMS)
        self.trusted_routes = list(trusted_routes or [])
        self.log_path, self.clock = log_path, clock
        self.lock = threading.Lock()
        self.side_routes: list[dict] = []
        self.turn_tokens: dict[str, tuple[str, float]] = {}     # token -> (session id, expires)
        self.session_token: dict[str, str] = {}
        self.sessions: dict[str, dict] = {}
        self.writes: list[dict] = []
        self.next_number = 20

    # -- rules --

    def _route_trusted(self, route: Any) -> bool:
        if not isinstance(route, dict):
            return False
        return any(route.get("model") == r.get("model") and _host(route.get("base_url")) == _host(r.get("base_url"))
                   for r in self.trusted_routes)

    def _side_routes_trusted(self) -> bool:
        for r in self.side_routes:
            if r.get("role") in ("fallback", "compression"):
                auto = r.get("provider") == "auto" and not r.get("model") and not r.get("base_url")
                if not auto and not self._route_trusted(r):
                    return False
        return True

    def tier(self, platform: Any, route: Any) -> tuple[str, str]:
        if not isinstance(platform, str) or platform not in self.trusted_platforms:
            return "shared", f"this chat is on {platform or 'an unknown platform'}"
        if not self._route_trusted(route):
            return "shared", "this chat's model isn't trusted with private memories"
        if not self._side_routes_trusted():
            return "shared", "a fallback or compression model isn't trusted"
        return "private", "a trusted model in a local app"

    def _session(self, session: Any) -> dict:
        sid = session.get("id") if isinstance(session, dict) else ""
        s = self.sessions.setdefault(str(sid or ""), {"platform": "", "route": None, "outside": ""})
        if isinstance(session, dict):
            s["platform"] = session.get("platform") if isinstance(session.get("platform"), str) else ""
            if isinstance(session.get("outside_tool"), str) and session["outside_tool"] and not s["outside"]:
                s["outside"] = session["outside_tool"]
        return s

    def _log(self, entry: dict) -> None:
        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"at": time.time(), **entry}, ensure_ascii=False) + "\n")

    # -- the protocol --

    def handle(self, call: str, headers: dict, body: Any) -> tuple[int, dict]:
        auth = next((v for k, v in headers.items() if k.lower() == "authorization"), "")
        if not hmac.compare_digest(str(auth).encode(), f"Bearer {self.token}".encode()):
            return 401, {"protocol": 1, "ok": False, "error": "unauthorized"}
        if not isinstance(body, dict) or call not in CALLS:
            return 400, {"protocol": 1, "ok": False, "error": "bad_request"}
        if body.get("protocol") != 1:
            return 400, {"protocol": 1, "ok": False, "error": "unsupported_protocol"}
        self._log({"call": call, "body": body})
        with self.lock:
            return 200, {"protocol": 1, "ok": True, **getattr(self, "_" + call.replace("-", "_"))(body)}

    def _hello(self, body: dict) -> dict:
        self.side_routes = [r for r in body.get("routes") or [] if isinstance(r, dict) and r.get("role") != "main"]
        return {"hub": {"name": "Fake hub"}, "tools": TOOLS, "server_outside_content": False,
                "verdict_ttl_seconds": 300, "hello_interval_seconds": 3600, "wants_user_messages": False}

    def _turn_start(self, body: dict) -> dict:
        s = self._session(body.get("session"))
        sid = body["session"]["id"]
        s["route"] = body.get("route") if body.get("route_source") in ("last_request", "config_default") else None
        old = self.session_token.pop(sid, None)
        if old:
            self.turn_tokens.pop(old, None)
        token = "tt_" + secrets.token_urlsafe(12)
        self.session_token[sid] = token
        self.turn_tokens[token] = (sid, self.clock() + TOKEN_TTL)
        return {"turn_token": token, "token_ttl_seconds": TOKEN_TTL}

    def _context(self, body: dict) -> dict:
        s = self._session(body.get("session"))
        known = body.get("route_source") in ("last_request", "config_default") and isinstance(body.get("route"), dict)
        tier, why = self.tier(s["platform"], body.get("route")) if known else ("shared", "the chat's model isn't known yet")
        out = [{"ref": m["ref"], "id": m["id"], "tier": m["tier"], "text": m["text"]}
               for m in self.memories if m.get("status", "active") == "active" and (tier == "private" or m["tier"] == "shared")]
        return {"memories": out, "held_back": "" if tier == "private" else why}

    def _verdict(self, body: dict) -> dict:
        s = self._session(body.get("session"))
        s["route"] = body.get("route")
        tier, why = self.tier(s["platform"], body.get("route"))
        return {"tier": tier, "reason": why}

    def _turn_report(self, body: dict) -> dict:
        s = self._session(body.get("session"))
        tools = [t for t in body.get("outside_tools") or [] if isinstance(t, str)]
        if tools and not s["outside"]:
            s["outside"] = tools[0]
        return {}

    # -- the hub's MCP tools --

    def _caller(self, args: dict) -> tuple[str, bool]:
        """The calling chat's tier and whether it read outside content (unknown chat: Shared, yes)."""
        token = args.get("wl_turn")
        found = self.turn_tokens.get(token) if isinstance(token, str) else None
        if found is None or self.clock() >= found[1]:
            return "shared", True
        s = self.sessions.get(found[0]) or {}
        tier, _ = self.tier(s.get("platform"), s.get("route"))
        return tier, bool(s.get("outside"))

    def tool(self, name: str, args: Any) -> tuple[str, dict]:
        """A tool call from MCP: the result text, and how the hub handled it (for tests)."""
        args = args if isinstance(args, dict) else {}
        with self.lock:
            tier, outside = self._caller(args)
            handled = {"route": tier, "outside_content": outside}
            if name == "memory_search":
                words = set(re.findall(r"\w+", str(args.get("query") or "").lower()))
                hits = [m for m in self.memories if m.get("status", "active") == "active"
                        and (tier == "private" or m["tier"] == "shared")
                        and (not words or words & set(re.findall(r"\w+", m["text"].lower())))]
                result = "\n".join(f"- {m['ref']} {_tag(m)}" for m in hits) or "No matching memories."
            elif name == "memory_save":
                text = str(args.get("text") or "").strip()
                want = "private" if args.get("tier", "private") == "private" else "shared"
                kind = want if tier == "private" else "shared"
                self.next_number += 1
                m = {"ref": f"{kind[0].upper()}{self.next_number}", "id": f"m_{self.next_number}", "tier": kind,
                     "text": text, "status": "pending" if outside else "active"}
                self.memories.append(m)
                self.writes.append({"tool": name, "memory": m, **handled})
                result = (f"Waiting for the user's review (this chat read outside content): {_tag(m)}" if outside
                          else f"Saved as {m['ref']}: {_tag(m)}")
            elif name == "memory_forget":
                ref = str(args.get("ref") or "")
                m = next((m for m in self.memories if m["ref"] == ref and (tier == "private" or m["tier"] == "shared")), None)
                self.writes.append({"tool": name, "ref": ref, **handled})
                if m is None:
                    result = f"No memory {ref}."
                elif outside:
                    result = f"Waiting for the user's review (this chat read outside content): forget {ref}."
                else:
                    m["status"] = "forgotten"
                    result = f"Forgot {ref}."
            else:
                return f"Unknown tool {name}.", handled
        self._log({"tool": name, "args": args, "handled_as": handled})
        return result, handled


class _Handler(BaseHTTPRequestHandler):
    hub: FakeHub

    def log_message(self, *_):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        try:
            body = json.loads(raw or b"null")
        except ValueError:
            body = None
        path = self.path.strip("/").split("/")
        if len(path) == 2 and path[0] == "tool":
            auth = self.headers.get("Authorization", "")
            if not hmac.compare_digest(auth.encode(), f"Bearer {self.hub.token}".encode()):
                status, answer = 401, {"error": "unauthorized"}
            else:
                result, handled = self.hub.tool(path[1], body)
                status, answer = 200, {"result": result, "handled_as": handled}
        elif len(path) == 2 and path[0] == "v1":
            status, answer = self.hub.handle(path[1], dict(self.headers), body)
        else:
            status, answer = 404, {"protocol": 1, "ok": False, "error": "bad_request"}
        data = json.dumps(answer).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(hub: FakeHub, port: int) -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,), {"hub": hub})
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def main() -> None:
    port, log, token = int(sys.argv[1]), sys.argv[2], sys.argv[3]
    memories = json.load(open(sys.argv[4], encoding="utf-8")) if len(sys.argv) > 4 else []
    routes = json.load(open(sys.argv[5], encoding="utf-8")) if len(sys.argv) > 5 else []
    serve(FakeHub(token, memories, trusted_routes=routes, log_path=log), port).serve_forever()


if __name__ == "__main__":
    main()
