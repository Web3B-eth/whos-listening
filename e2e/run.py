"""End-to-end checks: real Hermes, a scratch HERMES_HOME, a fake local model server.

    python e2e/run.py [--hermes PATH] [--keep]

What it does, all on this machine with made-up memories:

1. Makes a fresh scratch Hermes home in ``.scratch/e2e-home`` (never ``~/.hermes``), with two
   named endpoints on the fake server: ``private-sim`` (model ``private-model``) and
   ``shared-sim`` (model ``shared-model``). ``settings.json`` makes ``shared-model`` Shared; both
   are on 127.0.0.1, which is Private by default.
2. Copies the plugin's tracked files into the home and seeds two memories.
3. Runs ``hermes chat`` turns and reads what the fake server received.

Hermes' gateway lock folder (shared by every Hermes install for this OS user by default) is
pointed inside the scratch home too. Stops only the processes it started (the fake server, and
the gateway whose PID Hermes writes in the scratch home). Exits non-zero if any check fails.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from whos_listening.store import Provenance, SQLiteStore  # noqa: E402

PRIVATE_TEXT = "Keeps a vegetarian diet on weekdays"
SHARED_TEXT = "Likes spicy food"
PRIVATE_WORD, SHARED_WORD = "vegetarian", "spicy"
QUESTION = "What should I cook for dinner this week?"

CONFIG = """\
# Scratch e2e config. Fake local model server only; the key is a dummy.
model:
  provider: private-sim
  default: private-model
providers:
  private-sim:
    base_url: "http://127.0.0.1:{port}/v1"
    key_env: WL_E2E_DUMMY_KEY
  shared-sim:
    base_url: "http://127.0.0.1:{port}/v1"
    key_env: WL_E2E_DUMMY_KEY
memory:
  provider: whos-listening
  memory_enabled: false
  user_profile_enabled: false
"""
ENV_ALLOW = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TERM")
SETTINGS = {"routes": {"custom/shared-model": "shared", "*/shared-model": "shared"}}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Run:
    def __init__(self, hermes: str, home: Path, log: Path):
        self.hermes, self.home, self.log = hermes, home, log
        self.results: list[tuple[str, bool, str]] = []

    def env(self) -> dict:
        """A small allowlisted environment: no model keys or Hermes settings from the shell can
        reach this Hermes, so nothing but the fake server is ever called."""
        env = {k: os.environ[k] for k in ENV_ALLOW if k in os.environ}
        # Every Hermes install for this OS user shares one gateway lock folder by default
        # (~/.local/state/hermes/gateway-locks). Keep ours inside the scratch home.
        env.update(HERMES_HOME=str(self.home), WL_E2E_DUMMY_KEY="dummy-not-a-secret",
                   HERMES_GATEWAY_LOCK_DIR=str(self.home / "gateway-locks"),
                   XDG_STATE_HOME=str(self.home / "state"))
        return env

    def chat(self, query: str, *, provider: str = "", model: str = "", resume: str = "") -> tuple[str, list[dict]]:
        """One turn. Returns the session id and the requests the fake server got for it.
        A resumed chat keeps its launch route; switch it with a ``/model`` turn instead."""
        start = self.log.stat().st_size if self.log.exists() else 0
        cmd = [self.hermes, "chat", "-Q", "-q", query]
        if provider:
            cmd += ["--provider", provider, "-m", model]
        if resume:
            cmd += ["--resume", resume]
        out = subprocess.run(cmd, env=self.env(), cwd=self.home, stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=180)
        session = ""
        for line in (out.stdout + out.stderr).splitlines():
            if line.startswith("session_id:"):
                session = line.split(":", 1)[1].strip()
        if out.returncode != 0:
            print(out.stdout[-2000:], out.stderr[-2000:], sep="\n", file=sys.stderr)
        with open(self.log, encoding="utf-8") as f:
            f.seek(start)
            requests = [json.loads(line) for line in f if line.strip()]
        return session, requests

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, bool(ok), detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def main_requests(requests: list[dict]) -> list[dict]:
    """The chat's own requests (they carry tools); side calls such as the title don't."""
    return [r for r in requests if r.get("has_tools")]


def has(requests: list[dict], word: str) -> bool:
    return any(word in r["text"] for r in requests)


def _started_at(pid: int) -> float:
    """When ``pid`` started (seconds since the epoch), or 0 if unknown."""
    out = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True,
                         env={"LC_ALL": "C", "PATH": os.environ.get("PATH", "")}).stdout.strip()
    try:
        return time.mktime(time.strptime(out, "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return 0.0


def stop_gateway(home: Path, hermes: str, since: float) -> None:
    """Stop the background gateway our chats started. Its PID is in the scratch home; we only
    signal it if that process runs our Hermes build and started after this run began."""
    try:
        pid = int(json.loads((home / "gateway_state.json").read_text()).get("pid") or 0)
    except Exception:
        return
    if pid <= 0:
        return
    cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
    venv_bin = str(Path(hermes).resolve().parent) + "/"
    started = _started_at(pid)
    if venv_bin in cmd and started and started >= since - 1:
        os.kill(pid, signal.SIGTERM)
        print(f"stopped gateway pid {pid}")
    else:
        print(f"left pid {pid} alone: not clearly started by this run")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hermes", default=str(ROOT / ".local/venv-023/bin/hermes"))
    ap.add_argument("--keep", action="store_true", help="keep the scratch home afterwards")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--control", action="store_true",
                      help="keep the guard but switch off its clean-up: the route-switch checks should "
                           "fail, which shows they catch a real leak")
    mode.add_argument("--no-guard", action="store_true",
                      help="don't register the guard at all: recall should then never add a Private memory")
    args = ap.parse_args()

    t0 = time.time()
    scratch = ROOT / ".scratch"
    home = scratch / "e2e-home"
    if home.exists():
        shutil.rmtree(home)
    plugin_dir = home / "plugins" / "whos-listening"
    plugin_dir.mkdir(parents=True)
    tracked = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.split()
    for rel in tracked:
        src = ROOT / rel
        if src.is_file() and not rel.startswith(("docs/", "tests/", "e2e/", ".github/")):
            (plugin_dir / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, plugin_dir / rel)

    if args.control or args.no_guard:
        provider_py = plugin_dir / "whos_listening" / "provider.py"
        code = provider_py.read_text()
        if args.control:
            line, swap = "    cleaned = guard.guard_request(request, tier)", "    cleaned = None  # e2e --control"
            print("CONTROL RUN: the guard runs but removes nothing; the route-switch leak checks should fail.\n")
        else:
            line = '    ctx.register_middleware("llm_request", _on_llm_request)'
            swap = "    pass  # e2e --no-guard"
            print("NO-GUARD RUN: the guard is not registered; no Private memory should ever be added,\n"
                  "so the 'carries the Private memory' check should fail and the leak checks pass.\n")
        assert line in code
        provider_py.write_text(code.replace(line, swap))

    port = free_port()
    (home / "config.yaml").write_text(CONFIG.format(port=port))
    data = home / "whos-listening"
    data.mkdir()
    (data / "settings.json").write_text(json.dumps(SETTINGS))
    store = SQLiteStore(data / "memories.sqlite")
    seed = Provenance(written_by="user", via="e2e seed")
    store.add(PRIVATE_TEXT, "private", seed)
    shared_id = store.add(SHARED_TEXT, "shared", seed).id
    store.close()

    log = home / "requests.jsonl"
    log.touch()
    fake = subprocess.Popen([sys.executable, str(ROOT / "e2e/fake_llm.py"), str(port), str(log)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    run = Run(args.hermes, home, log)
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)

        # 0. A fresh gateway's first turn carries Shared memories only: recall doesn't trust any
        #    route until the guard has run once in the process.
        _, reqs = run.chat(QUESTION, provider="private-sim", model="private-model")
        run.check("cold start: no Private memory before the guard has run", not has(reqs, PRIVATE_WORD))

        # 1. Private route: the chat sees both memories.
        sid, reqs = run.chat(QUESTION, provider="private-sim", model="private-model")
        main = main_requests(reqs)
        run.check("private route: request sent", bool(main), f"{len(reqs)} requests")
        run.check("private route: carries the Private memory", has(main, PRIVATE_WORD))
        run.check("private route: carries the Shared memory", has(main, SHARED_WORD))

        # 2. Same chat, switched to a Shared route. Turn 1's memory block is replayed from
        #    history; the guard must take the Private memory out of every request.
        run.chat("/model shared-model --provider shared-sim", resume=sid)
        _, reqs = run.chat(QUESTION + " (second turn)", resume=sid)
        main = main_requests(reqs)
        run.check("route switch: request sent", bool(main), f"{len(reqs)} requests")
        run.check("route switch: sent to shared-model", main and all(r["model"] == "shared-model" for r in main))
        run.check("route switch: replayed history kept", has(main, "second turn") and has(main, QUESTION))
        run.check("route switch: no Private memory in any request", not has(reqs, PRIVATE_WORD))
        run.check("route switch: Shared memory still there", has(main, SHARED_WORD))
        run.check("route switch: guard left a marker", has(main, "private memory withheld"))

        # 3. New chat on the Shared route from the start.
        _, reqs = run.chat(QUESTION, provider="shared-sim", model="shared-model")
        run.check("shared route: no Private memory in any request", not has(reqs, PRIVATE_WORD))
        run.check("shared route: carries the Shared memory", has(main_requests(reqs), SHARED_WORD))

        # 4. Forget through the tool, then try to save it again: the tombstone blocks it.
        _, reqs = run.chat(f'Please forget that. [[call wl_forget {{"id": {shared_id}}}]]',
                           provider="private-sim", model="private-model")
        run.check("forget: tool ran", has(reqs, "won't be saved again"))
        _, reqs = run.chat(f'Remember this. [[call wl_remember {{"text": "{SHARED_TEXT.lower()}", "tier": "shared"}}]]',
                           provider="private-sim", model="private-model")
        run.check("forget: re-save blocked by the tombstone", has(reqs, "forgotten at the user"))
        store = SQLiteStore(data / "memories.sqlite")
        active = [m.text for m in store.list()]
        store.close()
        run.check("forget: store has only the Private memory", active == [PRIVATE_TEXT], repr(active))
    finally:
        stop_gateway(home, args.hermes, t0)
        fake.terminate()
        try:
            fake.wait(timeout=5)
        except subprocess.TimeoutExpired:
            fake.kill()
        if not args.keep:
            shutil.rmtree(home, ignore_errors=True)

    failed = [r for r in run.results if not r[1]]
    print(f"\n{len(run.results) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
