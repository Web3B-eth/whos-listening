"""The whos-listening command line, for the owner of this computer.

    python -m whos_listening.cli [--home HERMES_HOME] [COMMAND ...]

Run it from the plugin's folder (``$HERMES_HOME/plugins/whos-listening``). It takes the same
commands as ``/memories`` and shows Private text, since it runs on this computer. It also has
``purge N``, which deletes a forgotten memory's text from the file for good; that is only here,
not in chat. Nothing here imports Hermes.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import commands
from .store import SQLiteStore, StoreError

DATA_DIR, DB_FILE = "whos-listening", "memories.sqlite"


def default_home() -> Path:
    env = os.environ.get("HERMES_HOME", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".hermes"


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
        if args.command and args.command[0].lower() == "purge":
            if len(args.command) != 2:
                print("Usage: purge N", file=sys.stderr)
                return 2
            print(purge(store, args.command[1]))
        else:
            print(commands.run(store, " ".join(args.command), show_private=True, via="cli"))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
