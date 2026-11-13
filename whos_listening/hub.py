"""Hub mode: the plugin as the Hermes-side client of a hub that keeps the memories.

The protocol is docs/hub-protocol.md, with test cases in tests/vectors/hub/. In hub mode the
plugin keeps the request guard and the layers it can see (gateway, platform, model route); the
hub stores the memories, serves the memory tools over MCP and decides the tier. Everything
fails closed: a hub that can't be reached, or answers something this module doesn't
understand, means no memories and a Shared route.

This module holds the calls, the answers' checks and the per-session state. The Hermes glue
(reading the token, config and settings, and the hooks) is in provider.py. Nothing here
imports Hermes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

from . import recall
from .routes import ALWAYS_SHARED_PROVIDERS, Route, RouteSettings, Verdict, _domain_match, gateway_verdict, host_of
from .store import PRIVATE, SHARED

logger = logging.getLogger(__name__)

PROTOCOL = 1
TOKEN_ENV = "WHOS_LISTENING_HUB_TOKEN"
TIMEOUTS = {"hello": 5.0, "turn-start": 2.0, "context": 3.0, "verdict": 2.0, "turn-report": 2.0}
MAX_ANSWER_BYTES = 256 * 1024
FAILED_VERDICT_SECONDS = 60.0
QUERY_MAX_CHARS = 4000
WL_TURN = "wl_turn"
LOOPBACK = ("127.0.0.1", "::1", "localhost")

TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TURN_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
REF_RE = re.compile(r"^[A-Za-z0-9#._:-]{0,64}$")
ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
MAX_TEXT, MAX_LABEL, MAX_HELD_BACK = 1000, 80, 200

RANGES = {"verdict_ttl_seconds": (0, 3600, 300), "hello_interval_seconds": (300, 86400, 3600),
          "token_ttl_seconds": (0, 3600, 3600)}


class HubError(Exception):
    """A call that failed. ``code`` is the hub's error code, or one of ours (``timeout``,
    ``unreachable``, ``redirect``, ``too_big``, ``not_json``, ``bad_answer``, ``http_<status>``)."""

    def __init__(self, code: str, status: int = 0):
        super().__init__(code)
        self.code, self.status = code, status


# -- names -------------------------------------------------------------------------

def url_problem(url: str) -> str:
    """Why ``url`` can't be the hub's address ("" if it can): https, or http on a loopback host."""
    raw = str(url or "").strip()
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
    except ValueError:
        return "hub.url isn't a valid address"
    if not host:
        return "hub.url has no host"
    if parts.username or parts.password:
        return "hub.url must not hold a user name or password (the token goes in Hermes' secrets)"
    if parts.scheme == "https" or (parts.scheme == "http" and host in LOOPBACK):
        return ""
    return "hub.url must start with https:// (http:// only for 127.0.0.1, ::1 or localhost)"


def _sanitize(part: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", str(part or ""))


def mcp_tool_name(server: str, tool: str) -> str:
    """The name Hermes gives an MCP server's tool (tools/mcp_tool_schema.py in Hermes 0.21.4 and
    0.23.0): ``mcp__<server>__<tool>``, cut to 64 characters with a hash ending when longer."""
    full = f"mcp__{_sanitize(server)}__{_sanitize(tool)}"
    if len(full) <= 64:
        return full
    suffix = "_" + hashlib.sha256(full.encode("utf-8")).hexdigest()[:8]
    return full[:64 - len(suffix)] + suffix


def mcp_server_prefix(server: str) -> str:
    return f"mcp__{_sanitize(server)}__"


def find_mcp_server(hub_url: str, servers: Any) -> tuple[str, str]:
    """The MCP server in Hermes' ``mcp_servers`` whose address has the hub's host, and why not
    when there is none or more than one."""
    host = host_of(hub_url)
    names = [str(name) for name, cfg in (servers.items() if isinstance(servers, Mapping) else ())
             if isinstance(cfg, Mapping) and host and host_of(str(cfg.get("url") or "")) == host]
    if len(names) == 1:
        return names[0], ""
    if not names:
        return "", f"no MCP server in Hermes' config has the hub's host ({host or '?'}); set hub.mcp_server"
    return "", "several MCP servers have the hub's host; set hub.mcp_server"


_PLACEHOLDER_RE = re.compile(r"\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)\}")


def bearer_of(server_config: Any, resolve: Optional[Callable[[str], str]] = None) -> str:
    """The bearer token in an MCP server entry's ``Authorization`` header. A ``${VAR}`` (or
    ``${env:VAR}``) in it is looked up with ``resolve``, which in Hermes asks Hermes' own secret
    store. "" if there is no bearer, or a placeholder can't be filled in."""
    headers = server_config.get("headers") if isinstance(server_config, Mapping) else None
    if not isinstance(headers, Mapping):
        return ""
    for key, value in headers.items():
        if str(key).strip().lower() != "authorization" or not isinstance(value, str):
            continue
        missing = []

        def fill(m: "re.Match[str]") -> str:
            got = (resolve(m.group(1)) if resolve else "") or ""
            if not got:
                missing.append(m.group(1))
            return str(got)

        scheme, _, token = _PLACEHOLDER_RE.sub(fill, value).strip().partition(" ")
        token = token.strip()
        if scheme.lower() == "bearer" and token and not missing and "${" not in token:
            return token
    return ""


# -- request objects -----------------------------------------------------------------

def clean_base_url(base_url: str) -> str:
    """``base_url`` without a user name, password, query or fragment."""
    raw = str(base_url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        netloc = host + (f":{parts.port}" if parts.port else "")
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except ValueError:
        return ""


def hub_route(value: Any) -> Optional[Route]:
    """A route the hub names in a ``context`` answer, or None when it isn't a well-formed one."""
    if not isinstance(value, Mapping):
        return None
    provider, model, base_url = value.get("provider"), value.get("model"), value.get("base_url", "")
    if not all(isinstance(v, str) for v in (provider, model, base_url)):
        return None
    if not provider.strip() or not model.strip() or len(provider) + len(model) + len(base_url) > 600:
        return None
    return Route(provider=provider.strip(), model=model.strip(), base_url=clean_base_url(base_url))


def route_obj(route: Optional[Route]) -> Optional[dict]:
    if route is None:
        return None
    return {"provider": str(route.provider or ""), "model": str(route.model or ""),
            "base_url": clean_base_url(route.base_url)}


def hello_routes(config: Any) -> list[dict]:
    """Hermes' configured routes by role, names and addresses only (never keys): the main model,
    each fallback, the compression model and the other helper tasks (``auxiliary``). A named
    custom endpoint is provider ``"custom"``, as Hermes reports it in a real request."""
    out: list[dict] = []
    if not isinstance(config, Mapping):
        return out
    from .routes import _fallback_entries, config_route, default_route_from_config

    main = default_route_from_config(config)
    if main is not None:
        out.append({"role": "main", **route_obj(main)})
    for entry in _fallback_entries(config):
        route = config_route(config, entry.get("provider"), entry.get("model"), entry.get("base_url"))
        out.append({"role": "fallback", **route_obj(route)})
    aux = config.get("auxiliary") if isinstance(config.get("auxiliary"), Mapping) else {}
    for task, cfg in aux.items():
        if not isinstance(cfg, Mapping) or "provider" not in cfg:
            continue
        route = route_obj(config_route(config, cfg.get("provider") or "auto", cfg.get("model"), cfg.get("base_url")))
        if task == "compression":
            out.append({"role": "compression", **route})
        else:
            out.append({"role": "auxiliary", "name": str(task), **route})
    if not any(r["role"] == "compression" for r in out):
        out.append({"role": "compression", "provider": "auto", "model": "", "base_url": ""})
    return out


# -- answers -------------------------------------------------------------------------

@dataclass(frozen=True)
class HubTool:
    name: str                       # the hub's MCP name
    wire: str                       # the name Hermes gives it (mcp_tool_name)
    outside_content: bool
    redact_args: tuple[str, ...]
    role: str = ""                  # what the tool is for (TOOL_ROLES), "" when the hub didn't say


# What a hub tool is for, from its optional ``role`` in ``hello``. Any other value is ignored, so a
# hub can add roles later; the system prompt note names the save tool from it.
TOOL_ROLES = frozenset({"save", "search", "forget", "list", "context"})
SEARCH_ROLES = frozenset({"search", "list", "context"})


@dataclass(frozen=True)
class Hello:
    hub_name: str
    tools: tuple[HubTool, ...]
    server_prefix: str              # every tool of the hub's MCP server starts with this
    server_outside_content: bool = True
    verdict_ttl_seconds: int = 300
    hello_interval_seconds: int = 3600
    wants_user_messages: bool = False

    def tool(self, wire_name: str) -> Optional[HubTool]:
        return next((t for t in self.tools if t.wire == wire_name), None)

    def tool_for(self, role: str) -> Optional[HubTool]:
        """The first hub tool with this ``role``, or None."""
        return next((t for t in self.tools if t.role == role), None)

    def search_tools(self) -> frozenset:
        """Hermes names of the hub tools that only look memories up (role search, context or list):
        the guard blanks their replayed arguments to "" instead of the withheld placeholder."""
        return frozenset(t.wire for t in self.tools if t.role in SEARCH_ROLES)

    def redactions(self) -> dict[str, tuple[str, ...]]:
        """What the guard blanks: each hub memory tool's Hermes name and its ``redact_args``."""
        return {t.wire: t.redact_args for t in self.tools}

    def counts_as_outside(self, tool_name: str) -> Optional[bool]:
        """Whether a call to ``tool_name`` brings in outside content, or None when the tool isn't
        the hub's (then the usual rule applies)."""
        tool = self.tool(tool_name)
        if tool is not None:
            return tool.outside_content
        if tool_name.startswith(self.server_prefix):
            return self.server_outside_content
        return None


def _clamp(value: Any, key: str) -> int:
    low, high, default = RANGES[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(min(max(value, low), high))


def _role(value: Any) -> str:
    """A tool's ``role``, or "" when it is missing, not text or not one we know."""
    return value if isinstance(value, str) and value in TOOL_ROLES else ""


def parse_hello(body: Mapping, mcp_server: str) -> Hello:
    """A ``hello`` answer, or HubError("bad_answer") if a tool entry can't be read."""
    tools = body.get("tools")
    if not isinstance(tools, list):
        raise HubError("bad_answer")
    parsed = []
    for t in tools:
        if not isinstance(t, Mapping):
            raise HubError("bad_answer")
        name, redact = t.get("name"), t.get("redact_args", [])
        if not isinstance(name, str) or not TOOL_NAME_RE.match(name):
            raise HubError("bad_answer")
        if not isinstance(redact, list) or not all(isinstance(a, str) for a in redact):
            raise HubError("bad_answer")
        parsed.append(HubTool(name=name, wire=mcp_tool_name(mcp_server, name),
                              outside_content=t.get("outside_content") is not False,   # anything else counts
                              redact_args=tuple(dict.fromkeys(redact)),
                              role=_role(t.get("role"))))
    hub = body.get("hub")
    name = hub.get("name") if isinstance(hub, Mapping) else ""
    return Hello(hub_name=str(name or "the hub")[:80].replace("\n", " "), tools=tuple(parsed),
                 server_prefix=mcp_server_prefix(mcp_server),
                 server_outside_content=body.get("server_outside_content") is not False,
                 verdict_ttl_seconds=_clamp(body.get("verdict_ttl_seconds"), "verdict_ttl_seconds"),
                 hello_interval_seconds=_clamp(body.get("hello_interval_seconds"), "hello_interval_seconds"),
                 wants_user_messages=body.get("wants_user_messages") is True)


def _escape(text: str) -> str:
    return text.replace("<", "‹").replace(">", "›")


def memory_line(m: Any, keep_private: bool) -> Optional[str]:
    """One line of the memory block for a memory from ``context``, or None to drop it."""
    if not isinstance(m, Mapping):
        return None
    tier, ref, mid, text = m.get("tier"), m.get("ref", ""), m.get("id"), m.get("text")
    label = m.get("label")
    if tier not in (PRIVATE, SHARED) or (tier == PRIVATE and not keep_private):
        return None
    if not isinstance(ref, str) or not REF_RE.match(ref) or not isinstance(mid, str) or not ID_RE.match(mid):
        return None
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        return None
    if label is not None and (not isinstance(label, str) or len(label) > MAX_LABEL
                              or "\n" in label or "\r" in label):
        return None
    body = (f"[{_escape(label)}] " if label else "") + _escape(text)
    if tier == PRIVATE:
        body = recall.PRIVATE_OPEN.format(id=mid) + body + recall.PRIVATE_CLOSE
    return "- " + (f"{ref} " if ref else "") + body


def build_block(memories: Any, held_back: Any, *, keep_private: bool,
                max_chars: int = recall.MAX_BLOCK_CHARS) -> str:
    """The memory block recall adds, from a ``context`` answer (docs/hub-protocol.md, context)."""
    lines = [line for line in (memory_line(m, keep_private) for m in (memories if isinstance(memories, list) else []))
             if line is not None]
    note = ""
    if isinstance(held_back, str) and held_back.strip():
        reason = " ".join(held_back.split())[:MAX_HELD_BACK].rstrip(".")
        note = f"(Private memories are not used in this chat: {_escape(reason)}.)"
    return recall.block_of_lines(lines, max_chars=max_chars, note=note)


def local_shared(settings: Optional[RouteSettings], platform: str, route: Optional[Route]) -> Optional[Verdict]:
    """Hub mode's local layers: a Shared verdict when the plugin's own settings make the request
    Shared, else None (the hub decides). Settings that would make something Private don't apply."""
    if settings is None:
        return Verdict(SHARED, "settings unreadable")
    gateway = gateway_verdict(settings)
    if gateway is not None:
        return gateway
    name = str(platform or "").strip().lower()
    if name and name in settings.shared_platforms:
        return Verdict(SHARED, f"{name} is in shared_platforms")
    if route is None:
        return Verdict(SHARED, "no route known")
    provider, model = (route.provider or "").strip().lower(), (route.model or "").strip().lower()
    if provider in ALWAYS_SHARED_PROVIDERS:
        return Verdict(SHARED, ALWAYS_SHARED_PROVIDERS[provider])
    host = route.host
    for h in settings.shared_hosts:
        if host and _domain_match(host, h):
            return Verdict(SHARED, f"your shared host {h}")
    for key in (f"{provider}/{model}" if provider and model else None, f"*/{model}" if model else None,
                provider or None):
        if key and settings.routes.get(key) == SHARED:
            return Verdict(SHARED, f"your setting for {key}")
    return None


def decide_tier(settings: Optional[RouteSettings], platform: str, route: Optional[Route],
                ask_hub: Callable[[], tuple[str, str]]) -> tuple[str, str]:
    """The tier the guard applies to a request in hub mode, and where it came from
    (``tier_source``): Shared when a local layer says so (``local``), else the hub's verdict from
    ``ask_hub`` (``hub``, ``kept`` or ``failed``), Private only if it is exactly ``private``."""
    if local_shared(settings, platform, route) is not None:
        return SHARED, "local"
    tier, source = ask_hub()
    return (PRIVATE if tier == PRIVATE else SHARED), source


# -- transport -----------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):          # the token must not go anywhere else
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def http_post(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
    """POST and return (status, at most MAX_ANSWER_BYTES + 1 bytes of the answer). Redirects
    come back as their status. Raises HubError on a timeout or a connection failure."""
    req = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(MAX_ANSWER_BYTES + 1)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(MAX_ANSWER_BYTES + 1)
        except Exception:
            raw = b""
        return exc.code, raw
    except TimeoutError as exc:
        raise HubError("timeout") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        raise HubError("timeout" if isinstance(reason, TimeoutError) else "unreachable") from exc
    except OSError as exc:
        raise HubError("unreachable") from exc


def answer_of(status: int, raw: bytes) -> dict:
    """The answer as a dict, or HubError: anything but a 200 JSON object holding ``protocol: 1``
    and ``ok: true`` is a failure."""
    if 300 <= status < 400:
        raise HubError("redirect", status)
    if len(raw) > MAX_ANSWER_BYTES:
        raise HubError("too_big", status)
    try:
        body = json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, ValueError):
        body = None
    if not isinstance(body, dict):
        raise HubError("not_json" if status == 200 else f"http_{status}", status)
    if status != 200 or body.get("ok") is not True:
        code = body.get("error")
        raise HubError(code if isinstance(code, str) and re.match(r"^[a-z_]{1,40}$", code) else f"http_{status}", status)
    if body.get("protocol") != PROTOCOL or isinstance(body.get("protocol"), bool):
        raise HubError("unsupported_protocol", status)
    return body


# -- the client ----------------------------------------------------------------------

@dataclass
class _Session:
    platform: str = ""
    agent_context: str = "primary"
    turn_number: str = ""
    turn_id: str = ""
    token: str = ""
    token_expires: float = 0.0
    turn_started: threading.Event = field(default_factory=threading.Event)
    requests: list = field(default_factory=list)
    turn_outside: list = field(default_factory=list)    # outside tools this turn, names only
    outside_reported: str = ""


@dataclass(frozen=True)
class Kept:
    tier: str
    reason: str
    expires: float
    ok: bool                        # a usable answer (False: failed, kept 60 s as Shared)


class HubClient:
    """One hub, as one profile's settings name it. Thread-safe; never raises from its calls."""

    def __init__(self, url: str, mcp_server: str, *, transport: Callable = http_post,
                 clock: Callable[[], float] = time.monotonic, timeouts: Optional[Mapping[str, float]] = None):
        self.url = str(url).rstrip("/")
        self.mcp_server = mcp_server
        self._transport, self._clock = transport, clock
        self._timeouts = {**TIMEOUTS, **(timeouts or {})}
        self._lock = threading.Lock()
        self.hello: Optional[Hello] = None
        self.hello_at = 0.0                     # clock time of the last good hello
        self.hello_wall = 0.0                   # wall time of it, for /memories settings
        self.hello_error = "not tried yet"
        self.last_failure: Optional[tuple[str, str, float]] = None   # (call, code, wall time) of the last failed call
        self.last_success_wall = 0.0
        self._hello_due = True
        self._hello_running: Optional[threading.Event] = None
        self._verdicts: OrderedDict[tuple, Kept] = OrderedDict()
        self._sessions: OrderedDict[str, _Session] = OrderedDict()

    # -- calls --

    def call(self, name: str, body: dict, token: str) -> dict:
        """One call. Returns the answer, or raises HubError. Logs the call's name, status and
        time only: never a body, the token, a memory or the user's message."""
        if not token:
            raise HubError("no_token")
        started = time.monotonic()
        status = 0
        try:
            status, raw = self._transport(f"{self.url}/{name}", json.dumps({"protocol": PROTOCOL, **body}).encode(),
                                          {"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
                                          self._timeouts[name])
            answer = answer_of(status, raw)
            logger.info("whos-listening hub: %s %s in %d ms", name, status, (time.monotonic() - started) * 1000)
            self.last_success_wall = time.time()
            return answer
        except HubError as exc:
            self.last_failure = (name, exc.code, time.time())
            logger.warning("whos-listening hub: %s failed (%s%s) in %d ms", name,
                           f"{exc.status or status} " if (exc.status or status) else "", exc.code,
                           (time.monotonic() - started) * 1000)
            if exc.code in ("unauthorized", "unsupported_protocol"):
                with self._lock:
                    self._hello_due = True
            raise
        except Exception as exc:             # a broken transport counts as a failed call
            self.last_failure = (name, "unreachable", time.time())
            logger.warning("whos-listening hub: %s failed (%s)", name, type(exc).__name__)
            raise HubError("unreachable") from exc

    # -- hello --

    @property
    def ready(self) -> bool:
        return self.hello is not None

    def hello_needed(self) -> bool:
        with self._lock:
            if self._hello_running is not None:
                return False
            if self._hello_due or self.hello is None:
                return True
            return self._clock() - self.hello_at >= self.hello.hello_interval_seconds

    def start_hello(self) -> Optional[threading.Event]:
        """Mark a hello as running and return its done-event, or None if one already runs."""
        with self._lock:
            if self._hello_running is not None:
                return None
            self._hello_running = threading.Event()
            return self._hello_running

    def run_hello(self, body: dict, token: str, done: Optional[threading.Event] = None) -> bool:
        """Send ``hello`` and keep what it says. Until one succeeds, hub mode is not ready; a later
        failure keeps the last good answer (calls fail on their own if the hub is gone)."""
        try:
            answer = self.call("hello", body, token)
            hello = parse_hello(answer, self.mcp_server)
            with self._lock:
                self.hello, self.hello_at, self.hello_wall = hello, self._clock(), time.time()
                self.hello_error, self._hello_due = "", False
            return True
        except HubError as exc:
            with self._lock:
                self.hello_error = exc.code
                self._hello_due = False if self.hello is not None else True
                if exc.code in ("unauthorized", "unsupported_protocol", "bad_answer"):
                    self.hello = None        # the hub can't be spoken to this way any more
                if self.hello is not None:
                    self.hello_at = self._clock() - self.hello.hello_interval_seconds + 60   # retry in a minute
            return False
        finally:
            with self._lock:
                running, self._hello_running = self._hello_running, None
            for event in {e for e in (done, running) if e is not None}:
                event.set()

    def wait_hello(self, timeout: Optional[float] = None) -> bool:
        """Wait for a running hello (up to its timeout). Returns whether hub mode is ready."""
        with self._lock:
            running = self._hello_running
        if running is not None:
            running.wait(self._timeouts["hello"] + 1 if timeout is None else timeout)
        return self.ready

    # -- sessions --

    def session(self, session_id: str) -> _Session:
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                s = self._sessions[session_id] = _Session()
            self._sessions.move_to_end(session_id)
            while len(self._sessions) > 512:
                self._sessions.popitem(last=False)
            return s

    def note_session(self, session_id: str, *, platform: str = "", agent_context: str = "") -> None:
        if not session_id:
            return
        s = self.session(session_id)
        with self._lock:
            if platform:
                s.platform = platform
            if agent_context:
                s.agent_context = agent_context

    def carry_over(self, old: str, new: str) -> None:
        """A compressed, branched or resumed chat under a new id keeps its platform."""
        if old and new and old != new:
            before = self.session(old)
            self.note_session(new, platform=before.platform, agent_context=before.agent_context)

    def session_obj(self, session_id: str, outside_tool: str) -> dict:
        s = self.session(session_id)
        return {"id": session_id, "platform": s.platform, "agent_context": s.agent_context or "primary",
                "outside_tool": outside_tool or ""}

    def turn_obj(self, session_id: str) -> dict:
        s = self.session(session_id)
        return {"number": s.turn_number, "id": s.turn_id}

    # -- turn-start and the turn token --

    def begin_turn(self, session_id: str, turn_number: str) -> threading.Event:
        """A new turn: forget the old token and requests; the returned event is set once
        ``turn-start`` has answered (or failed)."""
        s = self.session(session_id)
        with self._lock:
            s.turn_number, s.turn_id = str(turn_number or ""), ""
            s.token, s.token_expires = "", 0.0
            s.requests, s.turn_outside = [], []
            s.turn_started = threading.Event()
            return s.turn_started

    def turn_start(self, session_id: str, body: dict, token: str, done: Optional[threading.Event] = None) -> Optional[str]:
        """Send ``turn-start``; keep and return the turn token, or None."""
        try:
            answer = self.call("turn-start", body, token)
            turn_token = answer.get("turn_token")
            if not isinstance(turn_token, str) or not TURN_TOKEN_RE.match(turn_token):
                return None
            ttl = _clamp(answer.get("token_ttl_seconds"), "token_ttl_seconds")
            s = self.session(session_id)
            with self._lock:
                if done is None or s.turn_started is done:      # not overtaken by a newer turn
                    s.token, s.token_expires = turn_token, self._clock() + ttl
            return turn_token
        except HubError:
            return None
        finally:
            if done is not None:
                done.set()

    def current_token(self, session_id: str, wait: bool = True) -> str:
        """The session's turn token ("" if none), waiting for its ``turn-start`` up to the timeout."""
        s = self.session(session_id)
        if wait:
            s.turn_started.wait(self._timeouts["turn-start"])
        with self._lock:
            return s.token if s.token and self._clock() < s.token_expires else ""

    # -- context --

    def context(self, body: dict, token: str, *, keep_private: bool,
                recheck: Optional[Callable[[Route], bool]] = None) -> str:
        """Send ``context``; the memory block for this turn, or "" (no memories) on any failure.

        When the plugin didn't know the turn's route (``route_source`` ``unknown``) and the hub
        says which route it knows the session is on (``route``), ``recheck`` decides again with
        that route whether the plugin's own layers let Private memories through."""
        try:
            answer = self.call("context", body, token)
        except HubError:
            return ""
        memories, held_back = answer.get("memories"), answer.get("held_back", "")
        if not isinstance(memories, list):
            return ""
        if not keep_private and recheck is not None and body.get("route_source") == "unknown":
            known = hub_route(answer.get("route"))
            if known is not None:
                keep_private = bool(recheck(known))
        max_chars = body.get("max_chars") if isinstance(body.get("max_chars"), int) else recall.MAX_BLOCK_CHARS
        return build_block(memories, held_back, keep_private=keep_private, max_chars=max_chars)

    # -- verdict --

    @staticmethod
    def verdict_key(session: Mapping, route: Mapping) -> tuple:
        return (session.get("id"), session.get("platform"), session.get("agent_context"),
                route.get("provider"), route.get("model"), route.get("base_url"))

    def kept_verdict(self, key: tuple) -> Optional[Kept]:
        with self._lock:
            kept = self._verdicts.get(key)
            if kept is not None and self._clock() < kept.expires:
                return kept
            return None

    def verdict(self, session: dict, route: dict, token: str) -> tuple[str, str, str]:
        """The hub's tier for a request: (tier, source, reason). ``source`` is ``kept`` for a fresh
        kept answer, ``hub`` for a new one and ``failed`` when there's no usable answer (Shared)."""
        key = self.verdict_key(session, route)
        kept = self.kept_verdict(key)
        if kept is not None:
            return kept.tier, "kept" if kept.ok else "failed", kept.reason
        ttl = self.hello.verdict_ttl_seconds if self.hello is not None else RANGES["verdict_ttl_seconds"][2]
        try:
            answer = self.call("verdict", {"session": session, "route": route}, token)
            tier = answer.get("tier")
            reason = answer.get("reason") if isinstance(answer.get("reason"), str) else ""
            ok = tier in (PRIVATE, SHARED)
        except HubError as exc:
            tier, reason, ok = SHARED, f"no answer from the hub ({exc.code})", False
        if not ok:
            tier, reason = SHARED, reason or "the hub's answer had no usable tier"
        reason = " ".join(str(reason).split())[:120]
        with self._lock:
            self._verdicts[key] = Kept(tier, reason, self._clock() + (ttl if ok else FAILED_VERDICT_SECONDS), ok)
            self._verdicts.move_to_end(key)
            while len(self._verdicts) > 512:
                self._verdicts.popitem(last=False)
        return tier, "hub" if ok else "failed", reason

    def kept_verdicts(self) -> list[tuple[tuple, Kept, float]]:
        """The fresh kept verdicts, with seconds left, for /memories settings."""
        now = self._clock()
        with self._lock:
            return [(k, v, v.expires - now) for k, v in self._verdicts.items() if now < v.expires]

    # -- turn-report --

    def record_request(self, session_id: str, turn_id: str, route: dict, tier: str, source: str) -> None:
        if not session_id:
            return
        s = self.session(session_id)
        with self._lock:
            if turn_id:
                s.turn_id = turn_id
            s.requests = (s.requests + [{"route": route, "tier": tier, "tier_source": source}])[-50:]

    def take_turn(self, session_id: str) -> tuple[list, list]:
        """This turn's requests and outside tools, for its ``turn_end`` report."""
        s = self.session(session_id)
        with self._lock:
            out, s.requests = (s.requests, list(s.turn_outside)), []
            s.turn_outside = []
            return out

    def note_outside(self, session_id: str, tool: str, first_in_chat: bool) -> bool:
        """Note an outside tool this turn. True when this is the chat's first one, which gets an
        ``outside`` report (``first_in_chat``: no earlier one is known for this session)."""
        s = self.session(session_id)
        with self._lock:
            if tool not in s.turn_outside:
                s.turn_outside = (s.turn_outside + [tool])[-20:]
            if s.outside_reported or not first_in_chat:
                return False
            s.outside_reported = tool
            return True

    def turn_report(self, body: dict, token: str) -> bool:
        """Send ``turn-report``. Returns whether it failed (a failure is only logged)."""
        try:
            self.call("turn-report", body, token)
            return False
        except HubError:
            return True


def turn_report_body(event: str, session: dict, turn: dict, outside_tools: Iterable[str],
                     requests: Optional[list] = None) -> dict:
    body = {"event": event, "session": session, "turn": turn, "outside_tools": [str(t) for t in outside_tools]}
    if event == "turn_end":
        body["requests"] = list(requests or [])
    return body
