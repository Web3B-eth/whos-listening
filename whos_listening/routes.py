"""Which tier a model route has, and what route a session is believed to be on.

A route is where a request goes: Hermes' provider name, the model and the ``base_url``.
Named custom endpoints all report provider ``"custom"`` in Hermes 0.23.0, so the host in
``base_url`` is what we classify by. See docs/spike.md.

Nothing here imports Hermes.
"""

from __future__ import annotations

import ipaddress
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from . import venice
from .store import PRIVATE, SHARED, TIERS

# Venice: its private models are Private by default; its anonymized ones (passed on to the company
# that made them) are Shared unless the user says otherwise (venice_anonymized). See venice.py.
VENICE_DOMAINS = ("venice.ai",)
LOCAL_NAMES = ("localhost",)
LOCAL_SUFFIXES = (".localhost", ".local")
# Providers that are Shared whatever the settings say, because the guard can't check what
# they send. Hermes' MoA builds its requests from a copy made before middleware runs.
ALWAYS_SHARED_PROVIDERS = {"moa": "MoA sends the chat to several models the guard can't check"}
# Hermes platforms where the chat stays on this computer (or the user's own gateway). On any
# other platform (Telegram, Signal, Slack, the API server, cron, subagents, ...) every route
# counts as Shared: the chat app's servers see the replies, whatever model wrote them.
LOCAL_PLATFORMS = frozenset({"cli", "local", "tui", "desktop"})


def platform_verdict(platform: str, settings: Optional["RouteSettings"] = None) -> Optional["Verdict"]:
    """The platform layer: Shared for a non-local platform; None when it may see Private memories,
    or when the platform isn't given (the caller then skips this layer). Hermes' plain CLI passes
    an empty platform to middleware; the provider itself always gets one (``cli`` by default).
    ``shared_platforms`` wins over the defaults and ``private_platforms``."""
    name = str(platform or "").strip().lower()
    if not name:
        return None
    settings = settings or RouteSettings()
    if name in settings.shared_platforms:
        return Verdict(SHARED, f"{name} is in shared_platforms")
    if name in LOCAL_PLATFORMS or name in settings.private_platforms:
        return None
    return Verdict(SHARED, f"this chat is on {name}, whose servers see the replies")


GATEWAY_SHARED = "this gateway is set to shared (the \"gateway\" setting)"
GATEWAY_HOSTED = ("this gateway is a hosted Hermes (on someone else's machine, as on Hermes Cloud), "
                  "so it counts as shared unless you set \"gateway\" to private")


def gateway_verdict(settings: Optional["RouteSettings"] = None) -> Optional["Verdict"]:
    """The gateway layer: the machine running Hermes, which stores the memories and builds every
    prompt. Private unless settings say otherwise; None means it may hold Private memories."""
    settings = settings or RouteSettings()
    if settings.gateway != PRIVATE:
        return Verdict(SHARED, GATEWAY_HOSTED if settings.gateway_hosted else GATEWAY_SHARED)
    return None


def private_storage_refusal(settings: Optional["RouteSettings"]) -> str:
    """Why this gateway may not keep a new Private memory ("" if it may). ``None`` means the
    settings can't be read, which refuses too: they might be the ones that say it is Shared."""
    if settings is None:
        return "No private memory was saved: the settings can't be read."
    verdict = gateway_verdict(settings)
    if verdict is not None:
        return f"No private memory was saved: {verdict.reason}, so it keeps none."
    return ""


def layered_verdict(route: Optional[Route], platform: str, settings: Optional["RouteSettings"] = None) -> "Verdict":
    """A request is Private only if the gateway, the platform and the model route all are."""
    for layer in (gateway_verdict(settings), platform_verdict(platform, settings)):
        if layer is not None:
            return layer
    return classify(route, settings)


@dataclass(frozen=True)
class Route:
    provider: str = ""
    model: str = ""
    base_url: str = ""

    @property
    def host(self) -> str:
        return host_of(self.base_url)

    def label(self) -> str:
        parts = [self.provider or "?"]
        if self.host:
            parts.append(f"@ {self.host}")
        if self.model:
            parts.append(f"/ {self.model}")
        return " ".join(parts)


def host_of(base_url: str) -> str:
    raw = str(base_url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    try:
        host = urlsplit(raw).hostname or ""
    except ValueError:
        return ""
    return host.strip().rstrip(".").lower()


def _domain_match(host: str, domain: str) -> bool:
    domain = domain.strip().lstrip(".").rstrip(".").lower()
    return bool(domain) and (host == domain or host.endswith("." + domain))


def is_local_host(host: str) -> bool:
    """localhost, loopback, private LAN and link-local addresses, and ``.local`` names."""
    if not host:
        return False
    if host in LOCAL_NAMES or host.endswith(LOCAL_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_loopback or ip.is_link_local:
        return True
    # is_private also covers documentation and other reserved ranges; keep to real LAN ranges.
    lan = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("fc00::/7"),
    )
    return any(ip in net for net in lan if net.version == ip.version)


@dataclass(frozen=True)
class RouteSettings:
    """The user's tier choices. Saved as JSON in the profile's whos-listening folder.

    - ``routes``: ``{"<provider>": tier, "<provider>/<model>": tier, "*/<model>": tier}``
    - ``private_hosts`` / ``shared_hosts``: extra hosts (a domain also covers its subdomains).
      ``shared_hosts`` wins over every default, so a user can turn a default Private host Shared.
    - ``private_platforms`` / ``shared_platforms``: Hermes platforms added to, or taken off, the
      ones that may see Private memories (``cli``, ``tui``, ``desktop``, ``local`` by default).
    - ``gateway``: ``"private"`` (default) or ``"shared"`` for the machine this Hermes runs on. A
      Shared gateway keeps no Private memories. Any other value counts as Shared.
    - ``review``: ``"auto"`` (default: Auto-review, see autoreview.py) or ``"ask"`` (held saves wait
      for the user). A value that is neither counts as ``"ask"``.

    settings.py merges these from Hermes' own config and settings.json.
    """

    routes: Mapping[str, str] = field(default_factory=dict)
    private_hosts: tuple[str, ...] = ()
    shared_hosts: tuple[str, ...] = ()
    private_platforms: tuple[str, ...] = ()
    shared_platforms: tuple[str, ...] = ()
    gateway: str = PRIVATE
    review: str = "auto"     # "auto": Auto-review checks held saves. "ask": they wait for the user.
    gateway_hosted: bool = False   # gateway is Shared only by the hosted-Hermes default (for the reason)
    duplicates: str = "skip"  # "skip": a near-duplicate save is skipped (duplicates.py). "keep": saved.
    duplicate_judge: str = "clef"  # a second near-duplicate check (judge.py): "clef", "jev", "model" or "off"
    venice_anonymized: str = SHARED   # how to treat Venice's anonymized models: "shared" or "private"

    @classmethod
    def from_dict(cls, data: Any) -> "RouteSettings":
        data = data if isinstance(data, Mapping) else {}
        routes = {}
        for key, tier in dict(data.get("routes") or {}).items():
            tier = str(tier).strip().lower()
            if tier in TIERS and str(key).strip():
                routes[str(key).strip().lower()] = tier
        def hosts(key: str) -> tuple[str, ...]:
            return tuple(str(h).strip().lower() for h in (data.get(key) or []) if str(h).strip())

        review = str(data.get("review") or "auto").strip().lower()
        duplicates = str(data.get("duplicates") or "skip").strip().lower()
        judge = str(data.get("duplicate_judge") or "clef").strip().lower()
        gateway = str(data.get("gateway") or PRIVATE).strip().lower()
        venice_anon = str(data.get("venice_anonymized") or SHARED).strip().lower()
        return cls(routes=routes, private_hosts=hosts("private_hosts"), shared_hosts=hosts("shared_hosts"),
                   private_platforms=hosts("private_platforms"), shared_platforms=hosts("shared_platforms"),
                   gateway=PRIVATE if gateway == PRIVATE else SHARED,       # a typo fails safe
                   review=review if review in ("ask", "auto") else "ask",     # a typo fails safe
                   duplicates=duplicates if duplicates in ("skip", "keep") else "skip",
                   duplicate_judge=judge if judge in ("clef", "jev", "model") else "off",   # a typo sends nothing
                   venice_anonymized=PRIVATE if venice_anon == PRIVATE else SHARED)

    def to_dict(self) -> dict:
        return {"routes": dict(self.routes), "private_hosts": list(self.private_hosts),
                "shared_hosts": list(self.shared_hosts), "private_platforms": list(self.private_platforms),
                "shared_platforms": list(self.shared_platforms), "gateway": self.gateway, "review": self.review,
                "duplicates": self.duplicates, "duplicate_judge": self.duplicate_judge,
                "venice_anonymized": self.venice_anonymized}

    @classmethod
    def load(cls, path: str | Path) -> "RouteSettings":
        """Read the settings file. A missing file means the defaults; a broken one raises."""
        p = Path(path)
        if not p.exists():
            return cls()
        return cls.from_dict(json.loads(p.read_text(encoding="utf-8")))

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(p)


@dataclass(frozen=True)
class Verdict:
    tier: str
    reason: str


def classify(route: Optional[Route], settings: Optional[RouteSettings] = None) -> Verdict:
    """The tier of ``route``. Unknown routes are Shared."""
    settings = settings or RouteSettings()
    if route is None:
        return Verdict(SHARED, "no route known")
    provider = (route.provider or "").strip().lower()
    model = (route.model or "").strip().lower()
    host = route.host
    if provider in ALWAYS_SHARED_PROVIDERS:
        return Verdict(SHARED, ALWAYS_SHARED_PROVIDERS[provider])

    for key in (f"{provider}/{model}" if provider and model else None,
                f"*/{model}" if model else None,
                provider or None):
        if key and key in settings.routes:
            return Verdict(settings.routes[key], f"your setting for {key}")

    if host:
        for h in settings.shared_hosts:
            if _domain_match(host, h):
                return Verdict(SHARED, f"your shared host {h}")
        if any(_domain_match(host, d) for d in VENICE_DOMAINS):
            return _venice_verdict(model, settings)
        for h in settings.private_hosts:
            if _domain_match(host, h):
                return Verdict(PRIVATE, f"your private host {h}")
        if is_local_host(host):
            return Verdict(PRIVATE, f"local address {host}")
        return Verdict(SHARED, f"host {host} is not on the private list")
    return Verdict(SHARED, "no base_url, so the host is unknown")


def _venice_verdict(model: str, settings: "RouteSettings") -> Verdict:
    """Venice's own label decides: private models are Private; anonymized ones (and models not on
    Venice's list, or no list yet) are Shared unless the user trusts anonymized Venice models."""
    label = venice.privacy(model)
    if label == venice.PRIVATE:
        return Verdict(PRIVATE, f"Venice runs {model} privately")
    if settings.venice_anonymized == PRIVATE:
        return Verdict(PRIVATE, f"Venice {model}: you trust Venice's anonymized models")
    if label == venice.ANONYMIZED:
        return Verdict(SHARED, f"Venice passes {model} on to the company that made it (anonymized)")
    return Verdict(SHARED, f"Venice model {model or '?'} isn't on Venice's list of private models")


def default_route_from_config(config: Any) -> Optional[Route]:
    """The route ``config.yaml`` picks when a chat does not choose one.

    Reads only ``model.provider``, ``model.default``, ``model.base_url`` and the matching
    ``providers.<name>.base_url``. Never key fields.
    """
    if not isinstance(config, Mapping):
        return None
    model_cfg = config.get("model")
    if isinstance(model_cfg, str):
        return Route(model=model_cfg)
    if not isinstance(model_cfg, Mapping):
        return None
    provider = str(model_cfg.get("provider") or "").strip()
    model = str(model_cfg.get("default") or model_cfg.get("model") or "").strip()
    base_url = str(model_cfg.get("base_url") or "").strip()
    providers = config.get("providers")
    if not base_url and provider and isinstance(providers, Mapping):
        entry = providers.get(provider)
        if isinstance(entry, Mapping):
            base_url = str(entry.get("base_url") or entry.get("api_base") or "").strip()
            # Hermes reports named custom endpoints as provider "custom" (docs/spike.md).
            provider = "custom" if base_url else provider
    return Route(provider=provider, model=model, base_url=base_url)


def _fallback_entries(config: Mapping) -> list[Mapping]:
    out = []
    for key in ("fallback_providers", "fallback_model"):
        raw = config.get(key)
        for entry in ([raw] if isinstance(raw, Mapping) else raw if isinstance(raw, list) else []):
            if isinstance(entry, Mapping) and str(entry.get("provider") or "").strip():
                out.append(entry)
    return out


def side_call_routes_private(config: Any, settings: Optional[RouteSettings] = None) -> Verdict:
    """Whether every route a Hermes side call could end up on is Private.

    A plugin's side call (PluginLlm) runs on the chat's model, but if that fails Hermes tries the
    ``fallback_providers`` from config.yaml, or, with no main provider set, any logged-in
    provider. Side calls skip middleware, so the guard can't clean them (docs/spike.md).
    """
    if not isinstance(config, Mapping):
        return Verdict(SHARED, "config.yaml could not be read")
    model_cfg = config.get("model")
    main = str(model_cfg.get("provider") or "").strip().lower() if isinstance(model_cfg, Mapping) else ""
    if main in ("", "auto"):
        return Verdict(SHARED, "Hermes picks the provider itself (provider: auto), so a side call could go anywhere")
    providers = config.get("providers") if isinstance(config.get("providers"), Mapping) else {}
    for entry in _fallback_entries(config):
        provider = str(entry.get("provider") or "").strip()
        base_url = str(entry.get("base_url") or "").strip()
        named = providers.get(provider) if isinstance(providers, Mapping) else None
        if not base_url and isinstance(named, Mapping):
            base_url = str(named.get("base_url") or named.get("api_base") or "").strip()
            provider = "custom" if base_url else provider
        verdict = classify(Route(provider, str(entry.get("model") or ""), base_url), settings)
        if verdict.tier != PRIVATE:
            return Verdict(SHARED, f"a fallback provider is shared ({verdict.reason})")
    return Verdict(PRIVATE, "the chat's route and every fallback are private")


class RouteBook:
    """What each session's route is believed to be, from the hooks we see.

    - The guard records the real route of each request it checks. Only the guard does, so
      a route is only ever "known" if the guard is really running: if Hermes failed to
      register it, every route stays unknown (Shared) and no Private memory is added.
    - ``pre_llm_call`` records the model a turn starts with.

    Keyed by session id; holds route names only, never memory text. Bounded so a long-lived
    gateway does not grow it forever.
    """

    def __init__(self, max_sessions: int = 512):
        self._lock = threading.Lock()
        self._seen: OrderedDict[str, Route] = OrderedDict()
        self._turn_model: OrderedDict[str, str] = OrderedDict()
        self._max = max_sessions
        self._guard_ran = False

    @property
    def guard_ran(self) -> bool:
        """Whether the guard has checked any request in this process yet."""
        return self._guard_ran

    def _put(self, d: OrderedDict, key: str, value) -> None:
        d[key] = value
        d.move_to_end(key)
        while len(d) > self._max:
            d.popitem(last=False)

    def saw_request(self, session_id: str, route: Route) -> None:
        """Called by the guard for every request it checks."""
        with self._lock:
            self._guard_ran = True
            if session_id:
                self._put(self._seen, session_id, route)

    def saw_turn_model(self, session_id: str, model: str) -> None:
        if session_id:
            with self._lock:
                self._put(self._turn_model, session_id, str(model or ""))

    def last_seen(self, session_id: str) -> Optional[Route]:
        with self._lock:
            return self._seen.get(session_id)

    def believed(self, session_id: str, config_default: Optional[Route]) -> tuple[Optional[Route], str]:
        """Best guess at the route of the next request, and where the guess came from.

        1. The route of the last request this session sent, if the turn's model still matches.
        2. On a first turn, the ``config.yaml`` default, if the turn's model matches it and the
           guard has already run in this process (so we know it is there to check).
        3. Otherwise nothing, which classifies as Shared.
        """
        with self._lock:
            seen = self._seen.get(session_id)
            turn_model = self._turn_model.get(session_id)
            guard_ran = self._guard_ran
        if seen is not None and (turn_model is None or turn_model == seen.model):
            return seen, "last request in this session"
        if seen is None and not guard_ran:
            return None, "the guard has not run yet"
        if seen is None and config_default is not None and turn_model is not None \
                and config_default.model and turn_model == config_default.model:
            return config_default, "config.yaml default route"
        return None, "route not known yet"
