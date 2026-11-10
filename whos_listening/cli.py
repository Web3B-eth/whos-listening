"""The whos-listening command line, for the owner of this computer.

    python -m whos_listening.cli [--home HERMES_HOME] [COMMAND ...]

Run it from the plugin's folder (``$HERMES_HOME/plugins/whos-listening``). It takes the same
commands as ``/memories`` and shows Private text, since it runs on this computer. It also has
``purge N``, which deletes a forgotten memory's text from the file for good; that is only here,
not in chat. Nothing here imports Hermes, so for whos-listening's settings in Hermes (Desktop's
plugin form) it uses the copy the gateway keeps (settings.py).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import commands
from . import settings as wl_settings
from .routes import RouteSettings, private_storage_refusal
from .store import SQLiteStore, StoreError

DATA_DIR, DB_FILE, SETTINGS_FILE = "whos-listening", "memories.sqlite", "settings.json"


def default_home() -> Path:
    env = os.environ.get("HERMES_HOME", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".hermes"


def load_settings(home: Path) -> wl_settings.Settings:
    """settings.json plus the gateway's copy of the Hermes half. Raises SettingsError."""
    data_dir = home / DATA_DIR
    snap = wl_settings.read_snapshot(data_dir)
    env_hosted = os.environ.get("HERMES_CONFIG_BACKEND", "").strip().lower() not in ("", "file")
    if snap is None:
        sources = wl_settings.Sources(data_dir / SETTINGS_FILE, {}, env_hosted, hermes_known=False)
    else:
        sources = wl_settings.Sources(data_dir / SETTINGS_FILE, snap[0], snap[1] or env_hosted)
    return wl_settings.load(sources)


def route_settings_or_none(home: Path) -> RouteSettings | None:
    try:
        return wl_settings.route_settings(load_settings(home))
    except wl_settings.SettingsError:
        return None


def show_settings(home: Path) -> str:
    try:
        s = load_settings(home)
    except wl_settings.SettingsError as exc:
        return f"{exc}. Until that's fixed, every route counts as shared and no private memory is saved."
    try:
        seen = wl_settings.read_snapshot(home / DATA_DIR) is not None
    except wl_settings.SettingsError:
        seen = False
    note = ("" if seen else "\n(The gateway hasn't run this version of whos-listening yet, so settings made "
            "in Desktop aren't known here yet.)")
    return wl_settings.describe(s) + note


def purge(store: SQLiteStore, arg: str) -> str:
    try:
        memory_id = int(str(arg).lstrip("#"))
    except ValueError:
        return "Give a memory number, like 3 or #3."
    try:
        store.purge(memory_id)
    except StoreError as exc:
        return str(exc)
    return f"Purged #{memory_id}: its text is gone from the file. Its tombstone stays, so it can't be saved again from chat."


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m whos_listening.cli", description=__doc__.splitlines()[0],
                                 epilog=commands.HELP.replace("/memories", "cli") + "\ncli purge N           delete a forgotten memory's text for good",
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", type=Path, default=None, help="the Hermes home (default: $HERMES_HOME or ~/.hermes)")
    ap.add_argument("command", nargs=argparse.REMAINDER, help="a /memories command, or purge N")
    args = ap.parse_args(argv)
    home = (args.home or default_home()).expanduser()
    path = home / DATA_DIR / DB_FILE
    if not path.exists():
        print(f"No whos-listening memories in {home}.", file=sys.stderr)
        return 1
    store = SQLiteStore(path)
    try:
        if args.command and args.command[0].lower() == "settings":
            print(show_settings(home))
        elif args.command and args.command[0].lower() == "purge":
            if len(args.command) != 2:
                print("Usage: purge N", file=sys.stderr)
                return 2
            print(purge(store, args.command[1]))
        else:
            print(commands.run(store, " ".join(args.command), show_private=True, via="cli",
                               private_refused=private_storage_refusal(route_settings_or_none(home))))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
