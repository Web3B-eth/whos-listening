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

from .store import PRIVATE, SHARED, TIERS

# Default Private hosts: Venice, plus anything on this machine or the local network.
DEFAULT_PRIVATE_DOMAINS = ("venice.ai",)
LOCAL_NAMES = ("localhost",)
LOCAL_SUFFIXES = (".localhost", ".local")


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
    """

    routes: Mapping[str, str] = field(default_factory=dict)
    private_hosts: tuple[str, ...] = ()
    shared_hosts: tuple[str, ...] = ()

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

        return cls(routes=routes, private_hosts=hosts("private_hosts"), shared_hosts=hosts("shared_hosts"))

    def to_dict(self) -> dict:
        return {"routes": dict(self.routes), "private_hosts": list(self.private_hosts),
                "shared_hosts": list(self.shared_hosts)}

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

    for key in (f"{provider}/{model}" if provider and model else None,
                f"*/{model}" if model else None,
                provider or None):
        if key and key in settings.routes:
            return Verdict(settings.routes[key], f"your setting for {key}")

    if host:
        for h in settings.shared_hosts:
            if _domain_match(host, h):
                return Verdict(SHARED, f"your shared host {h}")
        for h in settings.private_hosts:
            if _domain_match(host, h):
                return Verdict(PRIVATE, f"your private host {h}")
        for d in DEFAULT_PRIVATE_DOMAINS:
            if _domain_match(host, d):
                return Verdict(PRIVATE, f"default private host {d}")
        if is_local_host(host):
            return Verdict(PRIVATE, f"local address {host}")
        return Verdict(SHARED, f"host {host} is not on the private list")
    return Verdict(SHARED, "no base_url, so the host is unknown")


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


class RouteBook:
    """What each session's route is believed to be, from the hooks we see.

    - ``pre_api_request`` (and later the guard) records the real route of a request.
    - ``pre_llm_call`` records the model a turn starts with.

    Keyed by session id; holds route names only, never memory text. Bounded so a long-lived
    gateway does not grow it forever.
    """

    def __init__(self, max_sessions: int = 512):
        self._lock = threading.Lock()
        self._seen: OrderedDict[str, Route] = OrderedDict()
        self._turn_model: OrderedDict[str, str] = OrderedDict()
        self._max = max_sessions

    def _put(self, d: OrderedDict, key: str, value) -> None:
        d[key] = value
        d.move_to_end(key)
        while len(d) > self._max:
            d.popitem(last=False)

    def saw_request(self, session_id: str, route: Route) -> None:
        if session_id:
            with self._lock:
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
        2. On a first turn, the ``config.yaml`` default, if the turn's model matches it.
        3. Otherwise nothing, which classifies as Shared.
        """
        with self._lock:
            seen = self._seen.get(session_id)
            turn_model = self._turn_model.get(session_id)
        if seen is not None and (turn_model is None or turn_model == seen.model):
            return seen, "last request in this session"
        if seen is None and config_default is not None and turn_model is not None \
                and config_default.model and turn_model == config_default.model:
            return config_default, "config.yaml default route"
        return None, "route not known yet"
