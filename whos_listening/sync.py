"""Sync between gateways: Shared memories and forgets, through one JSON file both gateways reach.

Each gateway (your computer, a server, Hermes Cloud) runs its own whos-listening with its own
store. To share what they know, each one reads a sync file, merges it with its own Shared
memories, applies what is newer, and writes the merged file back. Every gateway only makes
outgoing calls, so a computer that can't be reached from outside works too.

What travels, and what never does:

- **Shared memories only.** Private memories are never read for sync, and a Private memory is
  never changed by it. Pending memories wait until the user approves them.
- **A decision made only here stays here.** Forgetting a Private memory, or turning down a held
  save (including one that arrived by sync), is never sent; and an arrival whose words were
  forgotten that way here is kept out here without being forgotten anywhere else.
- **Forgets travel without their text**: a forgotten memory goes out as its id and the hash of
  its words (the same hash a tombstone keeps), so a forget on one gateway forgets it everywhere.
- **Last change wins**, by each memory's change time; at the same time a forget wins.
- **An arrival is checked.** A memory that comes in passes the rule screen (review.py); one it
  flags waits for the user, like a held save.

The file lives in a private GitHub repository, read and written through the Contents API with a
token the user creates for that one repository. GitHub only accepts a write against the version
it was read at, so two gateways can't overwrite each other: the loser reads again and retries.

Nothing here imports Hermes; the provider passes in the token it gets from Hermes' secrets.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from . import review
from .store import (
    ACTIVE, FORGOTTEN, PENDING, SHARED, Provenance, Store, StoreError, clean_text, fingerprint, now_iso,
)

FORMAT = "whos-listening-sync"
VERSION = 1
TOKEN_ENV = "WHOS_LISTENING_SYNC_TOKEN"        # read through Hermes' secrets, never from a file
DEFAULT_PATH = "whos-listening/shared.json"
DEFAULT_API = "https://api.github.com"
DEFAULT_INTERVAL = 60                          # seconds between automatic syncs
MAX_FILE_BYTES = 900_000                       # the Contents API serves files up to 1 MB
ATTEMPTS = 4                                   # reads and merges after a write conflict

_UID = re.compile(r"^[0-9a-f]{32}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_NAME = re.compile(r"[^A-Za-z0-9 _.-]")


class SyncError(Exception):
    """Sync could not run or finish. The message is safe to show: it never holds the token."""


class Conflict(SyncError):
    """The file changed between our read and our write."""


# -- settings ------------------------------------------------------------------------------

@dataclass(frozen=True)
class SyncSettings:
    """The ``"sync"`` object in settings.json. Sync is on when ``repo`` is set."""

    repo: str = ""                     # "owner/name" of a private GitHub repository
    path: str = DEFAULT_PATH           # the file inside it
    branch: str = ""                   # "" means the repository's default branch
    name: str = ""                     # this gateway's name in the file ("" picks one)
    api_url: str = DEFAULT_API
    interval: int = DEFAULT_INTERVAL
    error: str = ""                    # why the settings were refused ("" if they weren't)

    @property
    def enabled(self) -> bool:
        return bool(self.repo) and not self.error

    @classmethod
    def from_dict(cls, data: Any) -> "SyncSettings":
        if data is None:
            return cls()
        if not isinstance(data, Mapping):
            return cls(error='"sync" must be an object')
        repo = str(data.get("repo") or "").strip()
        path = str(data.get("path") or DEFAULT_PATH).strip().strip("/")
        branch = str(data.get("branch") or "").strip()
        name = _NAME.sub("", str(data.get("name") or "")).strip()[:40]
        api_url = str(data.get("api_url") or DEFAULT_API).strip().rstrip("/")
        try:
            interval = int(data.get("interval") or DEFAULT_INTERVAL)
        except (TypeError, ValueError):
            interval = DEFAULT_INTERVAL
        interval = min(max(interval, 30), 86_400)
        if not repo:
            return cls()
        if not _REPO.match(repo):
            return cls(error='"sync.repo" must look like "owner/name"')
        if not path or ".." in path.split("/") or "\\" in path:
            return cls(error='"sync.path" must be a plain path inside the repository')
        if branch and not re.match(r"^[A-Za-z0-9_./-]+$", branch):
            return cls(error='"sync.branch" has characters a branch name can\'t have')
        parsed = urllib.parse.urlsplit(api_url)
        loopback = parsed.hostname in ("127.0.0.1", "localhost", "::1")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            return cls(error='"sync.api_url" must be https')
        return cls(repo=repo, path=path, branch=branch, name=name, api_url=api_url, interval=interval)


def load_settings(settings_path: str | Path) -> SyncSettings:
    """Sync settings from settings.json. A missing file means sync is off; a broken one too, with
    the reason in ``error``."""
    p = Path(settings_path)
    if not p.exists():
        return SyncSettings()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return SyncSettings(error="settings.json can't be read")
    return SyncSettings.from_dict(data.get("sync") if isinstance(data, Mapping) else None)


# -- the file ------------------------------------------------------------------------------

@dataclass(frozen=True)
class Record:
    """One memory as the sync file holds it."""

    uid: str
    status: str                         # ACTIVE or FORGOTTEN
    fingerprint: str                    # hash of the words (store.fingerprint)
    updated_at: str
    created_at: str = ""
    text: Optional[str] = None          # only for an active memory
    origin: str = ""                    # the gateway that first saved it


def _ts(stamp: str) -> datetime:
    try:
        t = datetime.fromisoformat(str(stamp))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def parse(raw: Optional[bytes]) -> dict[str, Record]:
    """The records in a sync file. Entries that aren't well formed are dropped (and so are gone
    from the file after our next write). A file from a newer format raises."""
    if not raw:
        return {}
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SyncError("The sync file is not valid JSON.") from exc
    if not isinstance(doc, Mapping) or doc.get("format") != FORMAT:
        raise SyncError("The sync file is not a whos-listening sync file.")
    if not isinstance(doc.get("version"), int) or doc["version"] > VERSION:
        raise SyncError("The sync file was written by a newer whos-listening; update this one first.")
    out: dict[str, Record] = {}
    memories = doc.get("memories")
    for uid, entry in (memories.items() if isinstance(memories, Mapping) else ()):
        rec = _record(str(uid), entry)
        if rec is not None:
            out[rec.uid] = rec
    return out


def _record(uid: str, entry: Any) -> Optional[Record]:
    if not _UID.match(uid) or not isinstance(entry, Mapping):
        return None
    status, updated = entry.get("status"), str(entry.get("updated_at") or "")
    if status not in (ACTIVE, FORGOTTEN) or _ts(updated).year < 2000:
        return None
    origin = _NAME.sub("", str(entry.get("origin") or "")).strip()[:40]
    created = str(entry.get("created_at") or "")
    if status == ACTIVE:
        try:
            text = clean_text(entry.get("text"))
        except StoreError:
            return None
        # The hash always comes from the words themselves, never from the file.
        return Record(uid, ACTIVE, fingerprint(text), updated, created, text, origin)
    fp = str(entry.get("fingerprint") or "")
    if not _HASH.match(fp):
        return None
    return Record(uid, FORGOTTEN, fp, updated, created, None, origin)   # a forget carries no text


def render(records: Mapping[str, Record]) -> bytes:
    memories = {}
    for uid in sorted(records):
        r = records[uid]
        entry = {"status": r.status, "fingerprint": r.fingerprint, "updated_at": r.updated_at,
                 "created_at": r.created_at, "origin": r.origin}
        if r.status == ACTIVE:
            entry["text"] = r.text
        memories[uid] = entry
    doc = {"format": FORMAT, "version": VERSION, "memories": memories}
    return (json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode("utf-8")


def newer(a: Record, b: Optional[Record]) -> bool:
    """Whether ``a`` wins over ``b``: the later change, and at the same time a forget."""
    if b is None:
        return True
    ka = (_ts(a.updated_at), a.status == FORGOTTEN)
    kb = (_ts(b.updated_at), b.status == FORGOTTEN)
    return ka > kb


# -- the transport ---------------------------------------------------------------------------

class GitHubFile:
    """One file in a GitHub repository, through the Contents API. Standard library only."""

    def __init__(self, settings: SyncSettings, token: str, *, timeout: float = 15.0,
                 opener: Callable[..., Any] = urllib.request.urlopen):
        if not settings.enabled:
            raise SyncError(settings.error or "Sync is off: no repository set.")
        if not token:
            raise SyncError(f"Sync needs a GitHub token in {TOKEN_ENV}.")
        self._s, self._token, self._timeout, self._open = settings, token, timeout, opener
        self._url = (f"{settings.api_url}/repos/{settings.repo}/contents/"
                     f"{urllib.parse.quote(settings.path)}")

    def _call(self, method: str, url: str, body: Optional[dict] = None) -> tuple[int, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "whos-listening",
            **({"Content-Type": "application/json"} if data is not None else {}),
        })
        try:
            with self._open(req, timeout=self._timeout) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read() or b"null")
            except ValueError:
                payload = None
            return exc.code, payload
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SyncError(f"GitHub could not be reached ({type(exc).__name__}).") from None

    def _refused(self, status: int) -> SyncError:
        if status in (401, 403):
            return SyncError(f"GitHub refused the token ({status}). It needs read and write access to "
                             f"the contents of {self._s.repo}.")
        if status == 404:
            return SyncError(f"GitHub can't find {self._s.repo} (404), or the token can't see it.")
        return SyncError(f"GitHub answered {status}.")

    def read(self) -> tuple[Optional[bytes], Optional[str]]:
        """The file's bytes and version (``sha``), or ``(None, None)`` if it doesn't exist yet."""
        url = self._url + (f"?ref={urllib.parse.quote(self._s.branch)}" if self._s.branch else "")
        status, payload = self._call("GET", url)
        if status == 404 and isinstance(payload, Mapping) and str(payload.get("message", "")).lower() == "not found":
            # The Contents API says "Not Found" both for a missing file and a missing repository.
            # Writing tells them apart: a missing repository refuses the write.
            return None, None
        if status != 200:
            raise self._refused(status)
        if not isinstance(payload, Mapping) or payload.get("type") != "file":
            raise SyncError(f"{self._s.path} in {self._s.repo} is not a file.")
        if int(payload.get("size") or 0) > MAX_FILE_BYTES or payload.get("encoding") != "base64":
            raise SyncError("The sync file is too large.")
        try:
            raw = base64.b64decode(str(payload.get("content") or ""), validate=False)
        except ValueError as exc:
            raise SyncError("GitHub sent a file that couldn't be decoded.") from exc
        return raw, str(payload.get("sha") or "") or None

    def write(self, raw: bytes, sha: Optional[str], message: str) -> None:
        if len(raw) > MAX_FILE_BYTES:
            raise SyncError("The sync file would be too large.")
        body: dict[str, Any] = {"message": message, "content": base64.b64encode(raw).decode("ascii")}
        if sha:
            body["sha"] = sha
        if self._s.branch:
            body["branch"] = self._s.branch
        status, payload = self._call("PUT", self._url, body)
        if status in (200, 201):
            return
        if status == 409 or (status == 422 and "sha" in json.dumps(payload or "").lower()):
            raise Conflict("The sync file changed while we merged.")
        raise self._refused(status)


# -- the merge -------------------------------------------------------------------------------

@dataclass
class SyncResult:
    arrived: int = 0          # memories added here
    held: int = 0             # of those, waiting for the user (the rule screen flagged them)
    forgotten: int = 0        # memories forgotten here because they were forgotten elsewhere
    restored: int = 0         # memories restored here because they were restored elsewhere
    sent: int = 0             # changes from here that the file didn't have
    duplicates: int = 0       # arrivals skipped because the same words are already here
    kept_out: int = 0         # arrivals skipped because their words were forgotten here
    wrote: bool = False
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.arrived:
            waits = f"{self.held} waits" if self.held == 1 else f"{self.held} wait"
            parts.append(f"{self.arrived} arrived" + (f" ({waits} for you: /memories pending)" if self.held else ""))
        if self.forgotten:
            parts.append(f"{self.forgotten} forgotten here")
        if self.restored:
            parts.append(f"{self.restored} restored here")
        if self.kept_out:
            parts.append(f"{self.kept_out} kept out (forgotten here)")
        if self.sent:
            parts.append(f"{self.sent} sent")
        return ", ".join(parts) or "nothing new"


def local_record(m, origin: str) -> Record:
    return Record(m.uid, m.status, fingerprint(m.text), m.updated_at, m.created_at,
                  m.text if m.status == ACTIVE else None, origin)


def _log(store: Store, memory_id: int, action: str) -> None:
    store.log_event(memory_id, action, "sync")


def _turned_down_here(store: Store, m) -> bool:
    """A memory forgotten while it waited, by the user on this gateway (not by a forget that
    arrived by sync, which a restore elsewhere may undo)."""
    if m.status != FORGOTTEN or m.forgotten_from != PENDING:
        return False
    last = store.events(memory_id=m.id, limit=1)
    return not (last and last[0].action == "synced_forget")


def sync_once(store: Store, transport, name: str, *, now: Callable[[], str] = now_iso) -> SyncResult:
    """Read the file, merge, apply what's newer here, write the file back. Retries on a write
    conflict (another gateway wrote in between). Applying is idempotent, so a retry is safe."""
    total = SyncResult()
    for _ in range(ATTEMPTS):
        raw, sha = transport.read()
        remote = parse(raw)
        result = _merge_and_apply(store, remote, name)
        _add_up(total, result.result)       # changes applied here in an earlier attempt still count
        result = _Pass(result.merged, total)
        merged = result.merged
        nothing_to_write = (raw is None and not merged) or (
            raw is not None and render(merged) == render(remote) and render(remote) == raw)
        if nothing_to_write:
            store.meta_set("sync.last_ok", now())
            return result.result
        try:
            transport.write(render(merged), sha, f"whos-listening sync from {name}")
        except Conflict:
            continue
        result.result.wrote = True
        store.meta_set("sync.last_ok", now())
        return result.result
    raise SyncError("The sync file kept changing while we merged. Try again.")


def _add_up(total: SyncResult, part: SyncResult) -> None:
    for name in ("arrived", "held", "forgotten", "restored", "duplicates", "kept_out"):
        setattr(total, name, getattr(total, name) + getattr(part, name))
    total.sent = part.sent              # what this attempt sends is what the file gets


@dataclass
class _Pass:
    merged: dict[str, Record]
    result: SyncResult


def _merge_and_apply(store: Store, remote: dict[str, Record], name: str) -> _Pass:
    result = SyncResult()
    merged = dict(remote)

    # 1. What this gateway has (Shared only; a pending memory is not shared yet).
    for m in store.shared_for_sync():
        if not m.uid or m.tier != SHARED:
            continue
        if m.status == FORGOTTEN and m.forgotten_from == PENDING:
            continue            # never active here: a turn-down is local, a synced forget is already out
        theirs = remote.get(m.uid)
        ours = local_record(m, theirs.origin if theirs and theirs.origin else name)
        if newer(ours, theirs) and ours != theirs:
            merged[m.uid] = ours
            result.sent += 1

    # 2. What is newer elsewhere, applied here.
    for uid, r in list(merged.items()):
        m = store.by_uid(uid)
        if m is not None:
            if m.tier != SHARED or m.status == r.status or not newer(r, local_record(m, name)):
                continue        # never touch a Private memory
            if r.status == FORGOTTEN:
                store.forget(m.id, at=r.updated_at, travels=True)   # a held one too: forgotten elsewhere
                _log(store, m.id, "synced_forget")
                result.forgotten += 1
            elif m.status == FORGOTTEN and not _turned_down_here(store, m):
                store.restore(m.id, at=r.updated_at)   # one that was held comes back waiting for the user
                _log(store, m.id, "synced_restore")
                result.restored += 1
            # A held memory keeps waiting for the user; one turned down here stays turned down.
            continue

        if r.status == FORGOTTEN:
            # Forgotten elsewhere: the same words here (saved separately) go too, unless they
            # changed here after that forget. And they can't come back from chat.
            for same in store.shared_with_fingerprint(r.fingerprint, (ACTIVE, PENDING)):
                if _ts(same.updated_at) <= _ts(r.updated_at):
                    store.forget(same.id, at=r.updated_at, travels=True)
                    _log(store, same.id, "synced_forget")
                    result.forgotten += 1
            if not store.shared_with_fingerprint(r.fingerprint, (ACTIVE, PENDING)) and \
                    store.tombstone(r.fingerprint) is None:
                store.add_tombstone(r.fingerprint, SHARED, r.updated_at)
            continue

        # An active memory this gateway hasn't seen.
        tomb = store.tombstone(r.fingerprint)
        if tomb:
            at, tier, travels = tomb
            if tier == SHARED and travels and _ts(at) >= _ts(r.updated_at):
                # These words were forgotten here after that save: the forget wins everywhere.
                merged[uid] = replace(r, status=FORGOTTEN, text=None, updated_at=at)
                result.sent += 1
                continue
            if tier != SHARED or not travels:
                result.kept_out += 1    # forgotten privately or turned down here: stays out here
                continue
        if store.shared_with_fingerprint(r.fingerprint, (ACTIVE, PENDING)):
            result.duplicates += 1      # the same words are already here: keep one line
            continue
        origin = r.origin or "another gateway"
        concern = review.rule_concern(r.text or "")
        m = store.import_synced(
            uid, r.text or "",
            Provenance(written_by="sync", via="sync", route=f"sync from {origin}", route_tier=SHARED),
            created_at=r.created_at or r.updated_at, updated_at=r.updated_at,
            status=PENDING if concern else ACTIVE,
            pending_reason=f"It arrived by sync from {origin}. {concern}" if concern else "")
        _log(store, m.id, "synced_in")
        result.arrived += 1
        result.held += 1 if concern else 0
    return _Pass(merged, result)


def gateway_name(store: Store, settings: SyncSettings) -> str:
    """This gateway's name in the file: the one in settings, else one made up once and kept."""
    if settings.name:
        return settings.name
    name = store.meta_get("sync.name")
    if not name:
        from .store import new_uid
        name = f"gateway-{new_uid()[:6]}"
        store.meta_set("sync.name", name)
    return name


def run(store: Store, settings: SyncSettings, token: str, *, transport=None) -> SyncResult:
    """One full sync, recording how it went in the store (``/memories sync status``)."""
    try:
        file = transport or GitHubFile(settings, token)
        result = sync_once(store, file, gateway_name(store, settings))
    except SyncError as exc:
        store.meta_set("sync.last_error", f"{now_iso()} {exc}")
        raise
    except StoreError as exc:           # e.g. another process applied the same arrival first
        store.meta_set("sync.last_error", f"{now_iso()} {exc}")
        raise SyncError(f"Sync stopped: {exc} Try again.") from None
    store.meta_set("sync.last_error", "")
    store.meta_set("sync.last_summary", result.summary())
    return result


def status(store: Store, settings: SyncSettings, has_token: bool) -> str:
    if settings.error:
        return f"Sync is off: {settings.error}."
    if not settings.enabled:
        return ("Sync is off. To turn it on, set the sync repository and token in whos-listening's settings "
                'in Hermes Desktop (Capabilities → Plugins), or add "sync": {"repo": "owner/name"} to '
                f"settings.json and set {TOKEN_ENV} (see the README).")
    lines = [f"Sync with {settings.repo} ({settings.path}) as {gateway_name(store, settings)}."]
    if not has_token:
        lines.append(f"No token: set the sync token in Desktop's plugin settings, or {TOKEN_ENV}.")
    last_ok = store.meta_get("sync.last_ok")
    lines.append(f"Last sync: {last_ok or 'never'}" + (f", {store.meta_get('sync.last_summary')}"
                                                        if last_ok and store.meta_get("sync.last_summary") else ""))
    if store.meta_get("sync.last_error"):
        lines.append(f"Last error: {store.meta_get('sync.last_error')}")
    return "\n".join(lines)
