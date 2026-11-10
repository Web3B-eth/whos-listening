"""Where whos-listening's settings come from. Nothing here imports Hermes.

Two places, merged setting by setting:

1. **Hermes' own config**, under ``plugins.entries.whos-listening.settings``. That is what the
   plugin's settings form in Hermes Desktop (Capabilities → Plugins) writes, for whichever gateway
   Desktop is connected to. On Hermes Cloud it is the only place you can reach.
2. **settings.json** in the profile's whos-listening folder.

A setting made in Hermes wins. An empty one there ("default", "", an empty list) means "not set
here", so settings.json, then the built-in default, applies.

The gateway's built-in default is Private, except when Hermes gets its config from a remote
config service (``HERMES_CONFIG_BACKEND=remote``, as on Hermes Cloud): then the machine is someone
else's, and the default is Shared. You can still set it either way.

The command line can't ask Hermes, so the gateway keeps a copy of the Hermes half next to
settings.json (``hermes-settings.json``) each time it reads it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Optional

from .routes import RouteSettings
from .sync import SyncSettings

PLUGIN_ID = "whos-listening"
SETTINGS_FILE = "settings.json"
SNAPSHOT_FILE = "hermes-settings.json"     # the Hermes half, for the command line
UNSET = "default"                           # the first choice of a Desktop menu: not set there

# A setting in Hermes' config (a flat key, so Desktop's form shows the saved value) and where it
# goes in the settings document (settings.json's shape).
FIELDS: dict[str, tuple[str, ...]] = {
    "gateway": ("gateway",),
    "review": ("review",),
    "sync_repo": ("sync", "repo"),
    "sync_name": ("sync", "name"),
    "sync_interval": ("sync", "interval"),
    "private_platforms": ("private_platforms",),
    "shared_platforms": ("shared_platforms",),
    "private_hosts": ("private_hosts",),
    "shared_hosts": ("shared_hosts",),
    "routes": ("routes",),
}
_LISTS = {"private_platforms", "shared_platforms", "private_hosts", "shared_hosts"}

FROM_HERMES = "Hermes settings"
FROM_FILE = "settings.json"
FROM_DEFAULT = "default"
FROM_HOSTED = "default on a hosted Hermes"


class SettingsError(Exception):
    """The settings can't be read, or a value has the wrong shape. Callers fail safe (Shared)."""


@dataclass(frozen=True)
class Sources:
    """What the settings are made from."""

    file: Path                                  # settings.json (it may not exist)
    hermes: Optional[Mapping[str, Any]] = None  # the plugin's settings in Hermes; None: couldn't be read
    hosted: bool = False                        # Hermes gets its config from a remote service
    hermes_known: bool = True                   # False: no Hermes to ask (the command line, no copy yet)


@dataclass(frozen=True)
class Settings:
    """The merged settings document and where each setting in it came from."""

    doc: dict
    origin: dict = field(default_factory=dict)  # top-level key ("sync.repo" for sync) -> FROM_*


def hermes_part(config: Any) -> dict:
    """``plugins.entries.whos-listening.settings`` from a Hermes config, or {}."""
    node: Any = config
    for key in ("plugins", "entries", PLUGIN_ID, "settings"):
        if not isinstance(node, Mapping):
            return {}
        node = node.get(key)
    return dict(node) if isinstance(node, Mapping) else {}


def _is_set(key: str, value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != "" and value.strip().lower() != UNSET
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def _check(key: str, value: Any, where: str) -> Any:
    """The value if it has the right shape for ``key``; raises otherwise (a list given as a string
    must not be read letter by letter)."""
    name = ".".join(FIELDS.get(key, (key,)))
    if key in _LISTS:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise SettingsError(f'"{name}" in {where} must be a list of names')
    elif key == "routes":
        if not isinstance(value, Mapping) or not all(isinstance(v, str) for v in value.values()):
            raise SettingsError(f'"routes" in {where} must map routes to "private" or "shared"')
    elif key == "sync_interval":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingsError(f'"sync.interval" in {where} must be a number of seconds')
    elif not isinstance(value, str):
        raise SettingsError(f'"{name}" in {where} must be text')
    return value


def _read_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SettingsError(f"{path.name} can't be read") from exc
    if not isinstance(data, Mapping):
        raise SettingsError(f"{path.name} must hold a JSON object")
    return dict(data)


def load(sources: Sources) -> Settings:
    """The settings in effect. Raises SettingsError if either place can't be read or holds a
    value of the wrong shape, so callers can fail safe."""
    if sources.hermes is None and sources.hermes_known:
        raise SettingsError("Hermes' settings for whos-listening can't be read")
    doc = _read_file(Path(sources.file))
    origin: dict[str, str] = {}
    for key, path in FIELDS.items():
        value = doc
        for part in path:
            value = value.get(part) if isinstance(value, Mapping) else None
        if _is_set(key, value):
            if key in _LISTS or key == "routes":
                _check(key, value, FROM_FILE)      # the rest settings.json has always taken loosely
            origin[".".join(path)] = FROM_FILE
    if isinstance(doc.get("sync"), Mapping):
        doc["sync"] = dict(doc["sync"])     # (any other "sync" turns only sync off, as it always has)
    for key, value in (sources.hermes or {}).items():
        if key not in FIELDS or not _is_set(key, value):
            continue                    # unknown keys and "not set here" fall through
        value = _check(key, value, FROM_HERMES)
        path = FIELDS[key]
        if len(path) == 2:
            doc.setdefault("sync", {})
            if not isinstance(doc["sync"], dict):
                doc["sync"] = {}
            doc["sync"][path[1]] = value
        else:
            doc[path[0]] = value
        origin[".".join(path)] = FROM_HERMES
    for key in ("gateway", "review"):
        if not _is_set(key, doc.get(key)):
            doc.pop(key, None)          # "default" in the file: the built-in default
    if "gateway" not in doc:
        doc["gateway"] = "shared" if sources.hosted else "private"
        origin["gateway"] = FROM_HOSTED if sources.hosted else FROM_DEFAULT
    return Settings(doc, origin)


def route_settings(s: Settings) -> RouteSettings:
    return replace(RouteSettings.from_dict(s.doc), gateway_hosted=s.origin.get("gateway") == FROM_HOSTED)


def sync_settings(s: Settings) -> SyncSettings:
    return SyncSettings.from_dict(s.doc.get("sync"))


def snapshot_path(data_dir: Path) -> Path:
    return Path(data_dir) / SNAPSHOT_FILE


def write_snapshot(data_dir: Path, hermes: Mapping[str, Any], hosted: bool,
                   cache: Optional[dict] = None) -> None:
    """Keep a copy of the Hermes half for the command line. Written only when it changed;
    ``cache`` (path -> text last written) saves reading the file each time."""
    p = snapshot_path(data_dir)
    keep = {k: v for k, v in hermes.items() if k in FIELDS}
    text = json.dumps({"hosted": bool(hosted), "settings": keep}, indent=1, sort_keys=True) + "\n"
    if cache is not None and cache.get(str(p)) == text:
        return
    try:
        if p.exists() and p.read_text(encoding="utf-8") == text:
            if cache is not None:
                cache[str(p)] = text
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(p)
        if cache is not None:
            cache[str(p)] = text
    except OSError:
        pass                            # only the command line misses it, and it says so


def read_snapshot(data_dir: Path) -> Optional[tuple[dict, bool]]:
    """The Hermes half as the gateway last saw it, or None if there is no copy (or it's broken)."""
    p = snapshot_path(data_dir)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, Mapping) or not isinstance(data.get("settings"), Mapping):
        return None
    return dict(data["settings"]), bool(data.get("hosted"))


def describe(s: Settings, *, token_set: Optional[bool] = None) -> str:
    """The settings in effect, one per line, each with where it came from."""
    d, o = s.doc, s.origin

    def line(label: str, key: str, value: Any) -> str:
        return f"{label}: {value}  ({o.get(key, FROM_DEFAULT)})"

    sync = d.get("sync") if isinstance(d.get("sync"), Mapping) else {}
    rows = [
        line("gateway", "gateway", d.get("gateway")),
        line("review", "review", d.get("review") or "ask"),
        line("sync", "sync.repo", sync.get("repo") or "off"),
    ]
    if sync.get("repo"):
        rows.append(line("sync name", "sync.name", sync.get("name") or "(made up)"))
        rows.append(line("sync interval", "sync.interval", f"{sync.get('interval') or 60} s"))
        if token_set is not None:
            rows.append(f"sync token: {'set' if token_set else 'missing'}  (Hermes secrets)")
    for key in ("private_platforms", "shared_platforms", "private_hosts", "shared_hosts"):
        if d.get(key):
            rows.append(line(key, key, ", ".join(d[key])))
    if d.get("routes"):
        rows.append(line("routes", "routes", ", ".join(f"{k} = {v}" for k, v in sorted(d["routes"].items()))))
    return "\n".join(rows)
