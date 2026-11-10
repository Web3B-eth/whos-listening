"""Where whos-listening's settings come from. Nothing here imports Hermes.

Two places, merged setting by setting:

1. **Hermes' own config**, under ``plugins.entries.whos-listening.settings``. That is what the
   plugin's settings form in Hermes Desktop (Settings → Plugins) writes, for whichever gateway
   Desktop is connected to. On Hermes Cloud it is the only place you can reach.
2. **settings.json** in the profile's whos-listening folder.

A setting made in Hermes wins. An empty one there ("default", "", an empty list) means "not set
here", so settings.json, then the built-in default, applies.

The gateway's built-in default is Private, except on a hosted Hermes, where the machine is someone
else's and the default is Shared (see ``hosted_reason``). You can still set it either way.

The command line can't ask Hermes, so the gateway keeps a copy of the Hermes half next to
settings.json (``hermes-settings.json``) each time it reads it.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Optional

from .routes import RouteSettings
from .sync import SyncSettings, repo_name

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
FROM_BOTH = "Hermes settings and settings.json"


class SettingsError(Exception):
    """The settings can't be read, or a value has the wrong shape. Callers fail safe (Shared)."""


@dataclass(frozen=True)
class Sources:
    """What the settings are made from."""

    file: Path                                  # settings.json (it may not exist)
    hermes: Optional[Mapping[str, Any]] = None  # the plugin's settings in Hermes; None: couldn't be read
    hosted: Any = False                         # why this Hermes is hosted (hosted_reason), "" if not
    hermes_known: bool = True                   # False: no Hermes to ask (the command line, no copy yet)


@dataclass(frozen=True)
class Settings:
    """The merged settings document and where each setting in it came from."""

    doc: dict
    origin: dict = field(default_factory=dict)  # top-level key ("sync.repo" for sync) -> FROM_*
    hosted: str = ""                            # why this Hermes counts as hosted, "" if it doesn't


def hosted_reason(environ: Mapping[str, str]) -> str:
    """Why this Hermes runs on someone else's machine ("" if, as far as we can tell, it doesn't).

    - Its config comes from a remote service (``HERMES_CONFIG_BACKEND`` other than ``file``).
    - It runs on a Fly.io machine (``FLY_APP_NAME`` and ``FLY_MACHINE_ID``, which Fly sets in
      every machine and Hermes itself uses to recognize Fly). Hermes Cloud runs there.
    """
    backend = str(environ.get("HERMES_CONFIG_BACKEND", "") or "").strip().lower()
    if backend not in ("", "file"):
        return "Hermes gets its config from a remote service"
    if str(environ.get("FLY_APP_NAME", "") or "").strip() and str(environ.get("FLY_MACHINE_ID", "") or "").strip():
        return "it runs on a Fly.io machine, as Hermes Cloud does"
    return ""


def _why_hosted(hosted: Any) -> str:
    if isinstance(hosted, str):
        return hosted.strip()
    return "Hermes gets its config from a remote service" if hosted else ""     # an older copy: True


def hermes_part(config: Any) -> dict:
    """``plugins.entries.whos-listening.settings`` from a Hermes config, or {}."""
    node: Any = config
    for key in ("plugins", "entries", PLUGIN_ID, "settings"):
        if not isinstance(node, Mapping):
            return {}
        node = node.get(key)
    return dict(node) if isinstance(node, Mapping) else {}


def _is_set(key: str, value: Any, where: str) -> bool:
    if value is None:
        return False
    if where == FROM_FILE and key == "gateway":
        # As settings.json has always read it: an empty value means the default, and any other
        # value that isn't "private" (a typo, "default", spaces) counts as Shared.
        return bool(value)
    if isinstance(value, str):
        return value.strip() != "" and value.strip().lower() != UNSET
    if isinstance(value, (list, dict)):
        return bool(value)
    if key == "sync_interval" and not isinstance(value, bool) and value == 0:
        return False                    # Desktop can't empty a number field: 0 means the default
    return True


def _check(key: str, value: Any, where: str) -> None:
    """Refuse a value whose shape would be misread: a list given as text would be read letter by
    letter. Entries inside a list or ``routes`` are sorted out the way settings.json always was."""
    name = ".".join(FIELDS.get(key, (key,)))
    if key in _LISTS and not isinstance(value, list):
        raise SettingsError(f'"{name}" in {where} must be a list, like ["signal"]')
    if key == "routes" and not isinstance(value, Mapping):
        raise SettingsError(f'"routes" in {where} must map routes to "private" or "shared"')
    if where != FROM_HERMES:
        return                          # the rest settings.json has always taken loosely
    if key == "sync_interval" and (isinstance(value, bool) or not isinstance(value, (int, float))):
        raise SettingsError(f'"{name}" in {where} must be a number of seconds')
    if key in ("gateway", "review", "sync_repo", "sync_name") and not isinstance(value, str):
        raise SettingsError(f'"{name}" in {where} must be text')


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


def _routes(*sources: Any) -> dict:
    """Routes from both places in one map. Where they disagree on a route, Shared wins."""
    out: dict[str, str] = {}
    for routes in sources:
        for key, tier in (routes or {}).items():
            k, t = str(key).strip().lower(), str(tier).strip().lower()
            if k and t in ("private", "shared") and out.get(k) != "shared":
                out[k] = t
    return out


def load(sources: Sources) -> Settings:
    """The settings in effect. Raises SettingsError if either place can't be read or holds a
    value whose shape would be misread, so callers can fail safe.

    A value set in Hermes wins, except that lists and ``routes`` combine both places (Desktop's
    form doesn't show what settings.json holds, so a Shared entry there must not be lost), with
    Shared winning where they disagree on a route. A sync repository set in Hermes takes none of
    settings.json's other sync settings (path, branch, api_url belong to the file's repository)."""
    if sources.hermes is None and sources.hermes_known:
        raise SettingsError("Hermes' settings for whos-listening can't be read")
    doc = _read_file(Path(sources.file))
    origin: dict[str, str] = {}
    for key, path in FIELDS.items():
        value = doc
        for part in path:
            value = value.get(part) if isinstance(value, Mapping) else None
        if (key in _LISTS or key == "routes") and value not in (None, ""):
            _check(key, value, FROM_FILE)          # any text here would be misread, "default" too
        if _is_set(key, value, FROM_FILE):
            origin[".".join(path)] = FROM_FILE
        elif len(path) == 1 and key in doc and (key in _LISTS or key == "routes"):
            doc.pop(key)                            # "" or empty: not set
    if isinstance(doc.get("sync"), Mapping):
        doc["sync"] = dict(doc["sync"])     # (any other "sync" turns only sync off, as it always has)

    hermes = {k: v for k, v in (sources.hermes or {}).items() if k in FIELDS and _is_set(k, v, FROM_HERMES)}
    for key, value in hermes.items():
        _check(key, value, FROM_HERMES)
    if "sync_repo" in hermes or (not isinstance(doc.get("sync"), dict) and doc.get("sync") is None):
        if "sync_repo" in hermes:
            for dotted in ("sync.repo", "sync.name", "sync.interval"):
                origin.pop(dotted, None)
        doc["sync"] = {}
    for key, value in hermes.items():
        path = FIELDS[key]
        dotted = ".".join(path)
        if len(path) == 2:
            if isinstance(doc.get("sync"), dict):   # a broken "sync" in the file stays broken
                doc["sync"][path[1]] = value
                origin[dotted] = FROM_HERMES
            continue
        if key in _LISTS and origin.get(dotted) == FROM_FILE:
            doc[key] = list(dict.fromkeys([*doc[key], *value]))
            origin[dotted] = FROM_BOTH
        elif key == "routes" and origin.get(dotted) == FROM_FILE:
            doc[key] = _routes(doc[key], value)
            origin[dotted] = FROM_BOTH
        else:
            doc[key] = value
            origin[dotted] = FROM_HERMES
    if isinstance(doc.get("routes"), Mapping):
        doc["routes"] = _routes(doc["routes"])
    if doc.get("sync") == {}:
        doc.pop("sync")
    if "review" not in origin:
        doc.pop("review", None)
    if "gateway" not in origin:
        doc["gateway"] = "shared" if sources.hosted else "private"
        origin["gateway"] = FROM_HOSTED if sources.hosted else FROM_DEFAULT
    return Settings(doc, origin, _why_hosted(sources.hosted))


def route_settings(s: Settings) -> RouteSettings:
    return replace(RouteSettings.from_dict(s.doc), gateway_hosted=s.origin.get("gateway") == FROM_HOSTED)


def sync_settings(s: Settings) -> SyncSettings:
    return SyncSettings.from_dict(s.doc.get("sync"))


def snapshot_path(data_dir: Path) -> Path:
    return Path(data_dir) / SNAPSHOT_FILE


def write_snapshot(data_dir: Path, hermes: Optional[Mapping[str, Any]], hosted: Any,
                   cache: Optional[dict] = None) -> None:
    """Keep a copy of the Hermes half for the command line; ``hermes`` None records that Hermes'
    config couldn't be read, so the command line fails safe too. Written only when it changed;
    ``cache`` (path -> (text, mtime)) saves reading the file each time."""
    p = snapshot_path(data_dir)
    if hermes is None:
        body: dict = {"hosted": _why_hosted(hosted), "unreadable": True}
    else:
        body = {"hosted": _why_hosted(hosted), "settings": {k: v for k, v in hermes.items() if k in FIELDS}}
    text = json.dumps(body, indent=1, sort_keys=True) + "\n"
    try:
        mtime = p.stat().st_mtime_ns if p.exists() else None
        if cache is not None and mtime is not None and cache.get(str(p)) == (text, mtime):
            return
        if mtime is not None and p.read_text(encoding="utf-8") == text:
            if cache is not None:
                cache[str(p)] = (text, mtime)
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")   # one per writer
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(p)
        if cache is not None:
            cache[str(p)] = (text, p.stat().st_mtime_ns)
    except OSError:
        pass                            # the command line then has no copy, and says so


def read_snapshot(data_dir: Path) -> Optional[tuple[dict, str]]:
    """The Hermes half as the gateway last saw it, or None if there is no copy yet. Raises
    SettingsError if the copy is broken or says Hermes' config couldn't be read."""
    p = snapshot_path(data_dir)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SettingsError(f"{SNAPSHOT_FILE} (the gateway's copy of the Hermes settings) can't be read") from exc
    if not isinstance(data, Mapping):
        raise SettingsError(f"{SNAPSHOT_FILE} (the gateway's copy of the Hermes settings) can't be read")
    if data.get("unreadable"):
        raise SettingsError("The gateway couldn't read Hermes' settings for whos-listening when it last tried")
    if not isinstance(data.get("settings"), Mapping):
        raise SettingsError(f"{SNAPSHOT_FILE} (the gateway's copy of the Hermes settings) can't be read")
    return dict(data["settings"]), _why_hosted(data.get("hosted"))


def describe(s: Settings, *, token_set: Optional[bool] = None) -> str:
    """The settings in effect, one per line, each with where it came from."""
    d, o = s.doc, s.origin

    def line(label: str, key: str, value: Any) -> str:
        return f"{label}: {value}  ({o.get(key, FROM_DEFAULT)})"

    sync = d.get("sync") if isinstance(d.get("sync"), Mapping) else {}
    rows = [
        f"this machine: {'hosted, because ' + s.hosted if s.hosted else 'not recognized as hosted (your own, as far as whos-listening can tell)'}",
        line("gateway", "gateway", d.get("gateway")),
        line("review", "review", d.get("review") or "ask"),
        line("sync", "sync.repo", repo_name(sync.get("repo")) or "off"),
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
