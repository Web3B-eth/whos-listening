"""Where memories live: a small `Store` interface and its SQLite (FTS5) version.

Nothing here imports Hermes, so it can be tested with plain pytest.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import unicodedata
import uuid
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from . import order as sync_order

PRIVATE = "private"
SHARED = "shared"
TIERS = (PRIVATE, SHARED)

ACTIVE = "active"
PENDING = "pending"
FORGOTTEN = "forgotten"
STATUSES = (ACTIVE, PENDING, FORGOTTEN)

MAX_TEXT = 500  # a memory is a short fact, not a document

SCHEMA_VERSION = 9


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def stamp_after(previous: str) -> str:
    """The time for a change to a memory last changed at ``previous``: now, or one second after
    ``previous`` if the clock isn't past it yet. A change made after seeing another one (perhaps
    synced from a gateway whose clock runs ahead) is always the later of the two, so sync's
    last-change-wins never undoes it."""
    stamp = now_iso()
    try:
        prev = datetime.fromisoformat(str(previous))
    except ValueError:
        return stamp
    prev = prev if prev.tzinfo else prev.replace(tzinfo=timezone.utc)
    if datetime.fromisoformat(stamp) > prev:
        return stamp
    return (prev.astimezone(timezone.utc) + timedelta(seconds=1)).replace(microsecond=0).isoformat()


def new_uid() -> str:
    """A memory's id across gateways (sync). Random, so it says nothing about the memory."""
    return uuid.uuid4().hex


@dataclass(frozen=True)
class Provenance:
    """Who wrote a memory, on which route, and from which turn."""

    written_by: str = "unknown"      # "assistant" (a tool call), "user" (a command or the CLI)
    via: str = ""                    # the tool or command, e.g. "whos-listening_remember", "cli"
    route: str = ""                  # e.g. "custom @ 127.0.0.1 / cfg-model"
    route_tier: str = ""             # the tier that route had when the memory was written
    session_id: str = ""
    turn: str = ""                   # turn id or number, when Hermes gave one
    author: str = ""                 # the chat author name, when Hermes gave one


@dataclass(frozen=True)
class Memory:
    id: int
    text: str
    tier: str
    status: str
    created_at: str
    updated_at: str
    provenance: Provenance = field(default_factory=Provenance)
    pending_reason: str = ""       # why a pending memory waits for the user
    forget_reason: str = ""        # set when the assistant asked to forget it and the user must OK it
    forgotten_from: str = ""       # the status it had before it was forgotten
    uid: str = ""                  # the same memory's id on every gateway (see sync.py)
    duplicate_of: int = 0          # flagged: it may say what this memory says, for the user to choose
    audience_consent_required: bool = False
    order_key: str = ""
    display_number: int = 0       # per-tier number shown to people; id remains the internal key
    sharing_approval_needed: bool = False
    legacy_alias: bool = False


@dataclass(frozen=True)
class Event:
    """One change to a memory, for ``/memories undo`` and the history in ``show``."""

    id: int
    memory_id: int
    action: str        # see EVENT_ACTIONS
    actor: str         # "assistant" or "user"
    at: str
    undone: bool = False


@dataclass(frozen=True)
class Withdrawal:
    uid: str
    fingerprint: str
    updated_at: str
    created_at: str
    origin: str
    order_key: str


# saved: became active. held: saved as pending. forgot: forgotten. forget_requested: the
# assistant asked to forget and it waits for the user. approved / rejected: the user decided a
# pending save or a forget request. restored: a forget was undone. undo: an undo (not undoable).
# synced_in / synced_forget / synced_restore: a change that arrived from another gateway (sync.py);
# /memories undo skips them.
EVENT_ACTIONS = ("saved", "held", "forgot", "forget_requested", "approved", "approved_forget",
                 "rejected", "rejected_forget", "restored", "undo",
                 "synced_in", "synced_forget", "synced_restore", "synced_withdraw",
                 "share_held", "share_approved", "share_rejected")
SYNC_ACTIONS = ("synced_in", "synced_forget", "synced_restore", "synced_withdraw")


class StoreError(ValueError):
    """A request the store refuses (bad tier, empty text, unknown id...)."""


def display_ref(memory: Memory) -> str:
    return ("P" if memory.tier == PRIVATE else "S") + str(memory.display_number)


class ForgottenError(StoreError):
    """The text matches a memory the user had forgotten (its tombstone blocks re-saving).
    ``tier`` is the forgotten memory's tier, so callers on a Shared route can avoid
    confirming that a Private fact was once saved."""

    def __init__(self, message: str, tier: str = PRIVATE):
        super().__init__(message)
        self.tier = tier


def clean_text(text: str) -> str:
    """One line of visible text: whitespace collapsed, invisible format characters (zero-width
    spaces and the like) removed, so what is screened is what is saved."""
    text = "".join(ch for ch in str(text or "") if unicodedata.category(ch) != "Cf")
    text = " ".join(text.split())
    if not text:
        raise StoreError("A memory needs some text.")
    if len(text) > MAX_TEXT:
        raise StoreError(f"A memory must be {MAX_TEXT} characters or fewer.")
    return text


def fingerprint(text: str) -> str:
    """What a tombstone keeps of a forgotten memory: a hash of its words, not its text.

    Case, punctuation and spacing don't matter, so "Likes jazz." and "likes  JAZZ" match.
    A reworded memory does not match (see README, Limits).
    """
    norm = unicodedata.normalize("NFKC", str(text or "")).casefold()
    norm = norm.translate(_QUOTES)
    words = " ".join(w.strip("'") for w in _WORD.findall(norm) if w.strip("'"))
    return hashlib.sha256(("wl1:" + words).encode("utf-8")).hexdigest()


# Curly and other look-alike apostrophes count as a plain one.
_QUOTES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'", "\u2032": "'", "`": "'"})


def check_tier(tier: str) -> str:
    tier = str(tier or "").strip().lower()
    if tier not in TIERS:
        raise StoreError("Tier must be 'private' or 'shared'.")
    return tier


class Store(ABC):
    """What the rest of the plugin needs from storage."""

    @abstractmethod
    def add(self, text: str, tier: str, provenance: Provenance, *,
            status: str = ACTIVE, pending_reason: str = "", allow_forgotten: bool = False,
            duplicate_of: int = 0, memory_id: Optional[int] = None,
            display_number: Optional[int] = None, sharing_approval_needed: bool = False) -> Memory:
        """Save a memory. Raises ``ForgottenError`` if a tombstone matches the text, unless
        ``allow_forgotten`` (only for the user's own commands), which also lifts the tombstone.
        ``duplicate_of`` flags it as possibly saying what that memory says (see resolve.py)."""

    @abstractmethod
    def get(self, memory_id: int) -> Optional[Memory]: ...

    @abstractmethod
    def resolve_reference(self, token: str, *, allow_legacy: bool = True) -> Optional[Memory]:
        """Resolve a whole Pn/Sn token, or an owner-only legacy internal number."""

    @abstractmethod
    def list(self, *, statuses: Iterable[str] = (ACTIVE,), tiers: Iterable[str] = TIERS,
             limit: int = 50) -> list[Memory]: ...

    @abstractmethod
    def search(self, query: str, *, tiers: Iterable[str] = TIERS, limit: int = 10) -> list[Memory]:
        """Active memories matching ``query``, best match first."""

    @abstractmethod
    def count(self, *, statuses: Iterable[str] = (ACTIVE,), tiers: Iterable[str] = TIERS) -> int: ...

    @abstractmethod
    def set_status(self, memory_id: int, status: str, *, pending_reason: str = "") -> Memory: ...
    def ensure_change_possible(self, memory_id: int) -> None: ...
    def atomic_choice(self): ...

    @abstractmethod
    def claim_memory_id(self) -> int:
        """Commit one ID claim independently of a later memory insert or suppression."""

    @abstractmethod
    def claim_display_number(self, tier: str) -> int:
        """Reserve a per-tier display number before a Shared attempt checks Private state."""

    @abstractmethod
    def promote_shared(self, memory_id: int, expected_order_key: str, *,
                       display_number: Optional[int] = None, actor: str = "assistant") -> Memory:
        """Promote the exact Private row after a checked or numbered sharing choice."""

    @abstractmethod
    def approve_private_only(self, memory_id: int, expected_order_key: str) -> Memory:
        """Resolve a sharing review as Private when duplicate policy forbids sharing."""

    @abstractmethod
    def request_sharing_review(self, memory_id: int, expected_order_key: str) -> Memory:
        """Mark an exact existing Private row for numbered sharing review."""

    @abstractmethod
    def decline_sharing(self, memory_id: int, expected_order_key: str) -> Memory:
        """Drop the sharing request without forgetting the Private fact."""

    @abstractmethod
    def forget(self, memory_id: int, *, at: Optional[str] = None, order_key: Optional[str] = None,
               travels: Optional[bool] = None,
               everywhere: bool = False) -> Memory:
        """Mark a memory forgotten and leave a tombstone so the same text can't come back.
        The text stays in the local file (so Undo works) until ``purge``. ``at`` sets the change
        time (sync applies another gateway's change at that gateway's time). ``travels`` overrides
        whether the tombstone's forget is one other gateways share (default: an active Shared
        memory's forget does; sync passes True for a forget that came from elsewhere).
        ``everywhere``: a Shared memory that arrived by sync and waits here is forgotten on the
        other gateways too, as if it had been active (turning one down is otherwise local)."""

    @abstractmethod
    def set_duplicate_of(self, memory_id: int, other_id: int) -> Memory:
        """Flag (or, with 0, unflag) a memory as possibly saying what ``other_id`` says. Doesn't
        change its time: the flag is local and never syncs."""

    @abstractmethod
    def flagged(self) -> list[Memory]:
        """Active or pending memories flagged as possible duplicates, oldest first."""

    @abstractmethod
    def restore(self, memory_id: int, *, at: Optional[str] = None, order_key: Optional[str] = None) -> Memory:
        """Undo a forget: the memory is active again and its tombstone is lifted."""

    @abstractmethod
    def purge(self, memory_id: int) -> None:
        """Remove a forgotten memory's SQLite row. Its tombstone stays; old bytes may remain."""

    @abstractmethod
    def is_forgotten_text(self, text: str) -> bool: ...

    @abstractmethod
    def request_forget(self, memory_id: int, reason: str) -> Memory:
        """The assistant asked to forget a memory; it keeps its status until the user decides."""

    @abstractmethod
    def clear_forget_request(self, memory_id: int) -> Memory: ...

    @abstractmethod
    def forget_requests(self) -> list[Memory]: ...

    @abstractmethod
    def log_event(self, memory_id: int, action: str, actor: str) -> Event: ...

    @abstractmethod
    def events(self, *, memory_id: Optional[int] = None, limit: int = 20) -> list[Event]:
        """Newest first."""

    @abstractmethod
    def mark_undone(self, event_id: int) -> None: ...

    @abstractmethod
    def last_event_id(self, memory_id: int) -> int:
        """The newest event id for this memory (0 if none)."""

    @abstractmethod
    def apply_review(self, memory_id: int, since_event: int, expected_order_key: str,
                     save: bool, reason: str) -> bool:
        """Apply an Auto-review verdict in one step, and only if the memory is still pending and
        nothing has happened to it since ``since_event`` and ``expected_order_key``.
        Returns whether it applied."""

    @abstractmethod
    def mark_outside(self, session_id: str, tool: str) -> None:
        """Remember that a chat read outside content (the first tool that brought it in)."""

    @abstractmethod
    def outside_tool(self, session_id: str) -> str:
        """The tool that first brought outside content into this chat, or ``""``."""

    # -- sync (see sync.py) ------------------------------------------------

    @abstractmethod
    def by_uid(self, uid: str) -> Optional[Memory]: ...

    @abstractmethod
    def shared_for_sync(self) -> list[Memory]:
        """Every Shared memory that is active or forgotten (pending ones are not shared yet)."""

    @abstractmethod
    def observe_order(self, uid: str, value: str) -> None:
        """Keep the highest same-UID order even when its status is held or locally rejected."""

    @abstractmethod
    def withdrawal(self, uid: str) -> Optional[Withdrawal]: ...

    @abstractmethod
    def withdrawals_for_sync(self) -> list[Withdrawal]: ...

    @abstractmethod
    def withdraw_approval(self, memory_id: int, reason: str = "You undid the approval.", *,
                          require_sharing_choice: bool = False) -> Memory: ...

    @abstractmethod
    def apply_withdrawal(self, uid: str, fp: str, at: str, created_at: str, origin: str,
                         order_key: str) -> None: ...

    @abstractmethod
    def clear_withdrawal_if_older(self, uid: str, order_key: str) -> None: ...

    @abstractmethod
    def import_synced(self, uid: str, text: str, provenance: Provenance, *, created_at: str,
                      updated_at: str, status: str = ACTIVE, pending_reason: str = "",
                      duplicate_of: int = 0, order_key: Optional[str] = None) -> Memory:
        """Add a Shared memory that arrived from another gateway, keeping its id and times. It lifts
        a Shared tombstone for the same words (the caller already decided the arrival is newer),
        never a Private one."""

    @abstractmethod
    def shared_with_fingerprint(self, fp: str, statuses: Iterable[str] = (ACTIVE,)) -> list[Memory]: ...

    @abstractmethod
    def add_tombstone(self, fp: str, tier: str, at: str, order_key: Optional[str] = None) -> None:
        """A tombstone for words this gateway never held (a forget that arrived by sync)."""

    @abstractmethod
    def tombstone(self, fp: str) -> Optional[tuple[str, str, bool]]:
        """``(when, tier, travels)`` of the tombstone for fingerprint ``fp``, or None. ``travels``:
        the forget is one to tell other gateways (an active Shared memory was forgotten, or the
        forget arrived by sync). Forgetting a Private memory or turning down a held save stays
        on this gateway."""

    @abstractmethod
    def tombstone_order(self, fp: str) -> Optional[str]: ...

    @abstractmethod
    def meta_get(self, key: str, default: str = "") -> str: ...

    @abstractmethod
    def meta_set(self, key: str, value: str) -> None: ...

    @abstractmethod
    def close(self) -> None: ...


_WORD = re.compile(r"[\w']+", re.UNICODE)


def fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 query: each word quoted, joined with OR."""
    words = [w.strip("'") for w in _WORD.findall(text or "")]
    words = [w for w in words if len(w) > 1]
    return " OR ".join('"' + w.replace('"', '""') + '"' for w in dict.fromkeys(words))


class SQLiteStore(Store):
    """One SQLite file per Hermes profile. Thread-safe through one lock."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._choice_depth = 0
        self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            try:
                self._db.execute("PRAGMA journal_mode = WAL")
            except sqlite3.OperationalError:
                pass  # another process is switching it at the same moment; one is enough
        self._fts = self._has_fts5()
        self._migrate()

    # -- setup -------------------------------------------------------------

    def _has_fts5(self) -> bool:
        try:
            self._db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp._wl_fts_probe USING fts5(x)")
            self._db.execute("DROP TABLE temp._wl_fts_probe")
            return True
        except sqlite3.OperationalError:
            return False

    _V1 = (
        """CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY,
            text TEXT NOT NULL,
            tier TEXT NOT NULL CHECK (tier IN ('private', 'shared')),
            status TEXT NOT NULL CHECK (status IN ('active', 'pending', 'forgotten')),
            pending_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            written_by TEXT NOT NULL DEFAULT 'unknown',
            via TEXT NOT NULL DEFAULT '',
            route TEXT NOT NULL DEFAULT '',
            route_tier TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '',
            turn TEXT NOT NULL DEFAULT '',
            author TEXT NOT NULL DEFAULT ''
        )""",
        "CREATE INDEX IF NOT EXISTS memories_status_tier ON memories (status, tier)",
    )
    _V1_FTS = (
        "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(text, content='memories', content_rowid='id')",
        """CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts(rowid, text) VALUES (new.id, new.text);
        END""",
        """CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, text) VALUES ('delete', old.id, old.text);
        END""",
        """CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF text ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, text) VALUES ('delete', old.id, old.text);
            INSERT INTO memories_fts(rowid, text) VALUES (new.id, new.text);
        END""",
    )
    # Tombstones keep a hash of a forgotten memory's words, never its text.
    _V2 = (
        """CREATE TABLE IF NOT EXISTS tombstones (
            fingerprint TEXT PRIMARY KEY,
            memory_id INTEGER,
            tier TEXT NOT NULL DEFAULT 'private',
            created_at TEXT NOT NULL
        )""",
    )
    _V3 = (
        """CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY,
            memory_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            actor TEXT NOT NULL,
            at TEXT NOT NULL,
            undone INTEGER NOT NULL DEFAULT 0
        )""",
        "CREATE INDEX IF NOT EXISTS events_memory ON events (memory_id)",
        """CREATE TABLE IF NOT EXISTS outside_content (
            session_id TEXT PRIMARY KEY,
            tool TEXT NOT NULL,
            at TEXT NOT NULL
        )""",
    )
    # Version 4: a cross-gateway id per memory, and a small key-value table for sync state.
    _V4 = (
        "CREATE UNIQUE INDEX IF NOT EXISTS memories_uid ON memories (uid)",
        """CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""",
    )
    # Version 5: a flag for a possible duplicate, for the user to choose which to keep.
    _V5_COLUMNS = (("duplicate_of", "INTEGER NOT NULL DEFAULT 0"),)
    _V6_COLUMNS = (("audience_consent_required", "INTEGER NOT NULL DEFAULT 0"),)
    _V7 = ("CREATE TABLE IF NOT EXISTS memory_id_state (singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
           "next_id INTEGER NOT NULL)",)
    _V8 = (
        "CREATE TABLE IF NOT EXISTS sync_seen (uid TEXT PRIMARY KEY, order_key TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS withdrawals (uid TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, "
        "updated_at TEXT NOT NULL, created_at TEXT NOT NULL, origin TEXT NOT NULL, order_key TEXT NOT NULL)",
    )
    _V9 = (
        "CREATE TABLE IF NOT EXISTS display_id_state (tier TEXT PRIMARY KEY CHECK (tier IN ('private','shared')), "
        "next_number INTEGER NOT NULL)",
        "CREATE UNIQUE INDEX IF NOT EXISTS memories_display_number ON memories (tier, display_number)",
    )
    # Columns added in version 3: (name, definition).
    _V3_COLUMNS = (
        ("forget_reason", "TEXT NOT NULL DEFAULT ''"),
        ("forgotten_from", "TEXT NOT NULL DEFAULT ''"),   # the status before a forget, for restore
    )

    def _version(self) -> int:
        return self._db.execute("PRAGMA user_version").fetchone()[0]

    def _has_column(self, table: str, column: str) -> bool:
        return any(r[1] == column for r in self._db.execute(f"PRAGMA table_info({table})").fetchall())

    def _migrate(self) -> None:
        """Bring the file up to SCHEMA_VERSION in one ``BEGIN IMMEDIATE`` transaction, so two
        processes opening an old file at once can't both migrate it, and a migration that stops
        halfway leaves the file as it was."""
        with self._lock:
            version = self._version()
            if version > SCHEMA_VERSION:
                raise StoreError(f"{self.path} was made by a newer Who's Listening (schema {version}).")
            if version == SCHEMA_VERSION:
                return
            old_isolation = self._db.isolation_level
            self._db.isolation_level = None          # we manage the transaction ourselves
            try:
                self._db.execute("BEGIN IMMEDIATE")
                try:
                    self._migrate_locked(self._version())   # re-read: another process may have won
                    self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    self._db.execute("COMMIT")
                except BaseException:
                    self._db.execute("ROLLBACK")
                    raise
            finally:
                self._db.isolation_level = old_isolation

    def _migrate_locked(self, version: int) -> None:
        if version < 1:
            for sql in self._V1 + (self._V1_FTS if self._fts else ()):
                self._db.execute(sql)
        if version < 2:
            for sql in self._V2:
                self._db.execute(sql)
            for row in self._db.execute(
                    "SELECT id, text, tier FROM memories WHERE status = 'forgotten'").fetchall():
                self._tombstone(row["text"], row["id"], row["tier"])
        if version < 3:
            for name, definition in self._V3_COLUMNS:
                if not self._has_column("memories", name):
                    self._db.execute(f"ALTER TABLE memories ADD COLUMN {name} {definition}")
            for sql in self._V3:
                self._db.execute(sql)
        if version < 4:
            if not self._has_column("memories", "uid"):
                self._db.execute("ALTER TABLE memories ADD COLUMN uid TEXT")
            for (row_id,) in self._db.execute("SELECT id FROM memories WHERE uid IS NULL OR uid = ''").fetchall():
                self._db.execute("UPDATE memories SET uid = ? WHERE id = ?", (new_uid(), row_id))
            if not self._has_column("tombstones", "travels"):
                self._db.execute("ALTER TABLE tombstones ADD COLUMN travels INTEGER NOT NULL DEFAULT 0")
            # An existing Shared forget travels, unless it turned down a save that was never shared.
            self._db.execute("UPDATE tombstones SET travels = 1 WHERE tier = 'shared' AND (memory_id IS NULL OR "
                             "memory_id NOT IN (SELECT id FROM memories WHERE forgotten_from = 'pending'))")
            for sql in self._V4:
                self._db.execute(sql)
        if version < 5:
            for name, definition in self._V5_COLUMNS:
                if not self._has_column("memories", name):
                    self._db.execute(f"ALTER TABLE memories ADD COLUMN {name} {definition}")
        if version < 6:
            for name, definition in self._V6_COLUMNS:
                if not self._has_column("memories", name):
                    self._db.execute(f"ALTER TABLE memories ADD COLUMN {name} {definition}")
            # Old assistant Shared holds may otherwise be approved by a late side-call verdict.
            for row in self._db.execute("SELECT id FROM memories WHERE tier = 'shared' "
                                        "AND status = 'pending' AND written_by = 'assistant'").fetchall():
                # The flag and new event invalidate old Auto-review verdicts. Keep the original
                # timestamp: a legacy row may already have the largest representable date.
                self._db.execute("UPDATE memories SET audience_consent_required = 1, pending_reason = ? WHERE id = ?",
                                 ("Approve sharing with any model on a Private-capable platform.", row["id"]))
                self._db.execute("INSERT INTO events (memory_id, action, actor, at) VALUES (?,?,?,?)",
                                 (row["id"], "share_held", "migration", now_iso()))
        if version < 7:
            for sql in self._V7:
                self._db.execute(sql)
            highest = self._db.execute("SELECT MAX(id) FROM memories").fetchone()[0]
            # SQLite cannot store MAX(id) + 1 when the file already uses the last signed ID.
            # Keep the existing file readable and mark the allocator exhausted instead.
            next_id = min(2**63 - 1, int(highest or 0) + 1)
            self._db.execute("INSERT OR IGNORE INTO memory_id_state (singleton, next_id) VALUES (1, ?)",
                             (next_id,))
            self._db.execute("UPDATE memory_id_state SET next_id = MAX(next_id, ?) WHERE singleton = 1",
                             (next_id,))
        if version < 8:
            if not self._has_column("memories", "order_key"):
                self._db.execute("ALTER TABLE memories ADD COLUMN order_key TEXT NOT NULL DEFAULT ''")
            if not self._has_column("tombstones", "order_key"):
                self._db.execute("ALTER TABLE tombstones ADD COLUMN order_key TEXT NOT NULL DEFAULT ''")
            for sql in self._V8:
                self._db.execute(sql)
            for row in self._db.execute("SELECT id, updated_at FROM memories WHERE order_key = ''").fetchall():
                try:
                    value = sync_order.from_iso(row["updated_at"])
                except sync_order.OrderError as exc:
                    raise StoreError(f"Memory #{row['id']} has an invalid change time; migration stopped.") from exc
                self._db.execute("UPDATE memories SET order_key = ? WHERE id = ?", (value, row["id"]))
            for row in self._db.execute("SELECT fingerprint, created_at FROM tombstones "
                                        "WHERE order_key = ''").fetchall():
                try:
                    value = sync_order.from_iso(row["created_at"])
                except sync_order.OrderError as exc:
                    raise StoreError("A tombstone has an invalid change time; migration stopped.") from exc
                self._db.execute("UPDATE tombstones SET order_key = ? WHERE fingerprint = ?",
                                 (value, row["fingerprint"]))
        if version < 9:
            self._db.execute(self._V9[0])
            if not self._has_column("memories", "display_number"):
                self._db.execute("ALTER TABLE memories ADD COLUMN display_number INTEGER NOT NULL DEFAULT 0")
            if not self._has_column("memories", "sharing_approval_needed"):
                self._db.execute("ALTER TABLE memories ADD COLUMN sharing_approval_needed INTEGER NOT NULL DEFAULT 0")
            if not self._has_column("memories", "legacy_alias"):
                self._db.execute("ALTER TABLE memories ADD COLUMN legacy_alias INTEGER NOT NULL DEFAULT 0")
            if version > 0:
                self._db.execute("UPDATE memories SET legacy_alias = 1")
            # Old hidden proposals become ordinary Private review items. They were never fact
            # screened, so they stay pending; the owner must make an explicit choice.
            self._db.execute("UPDATE memories SET tier = 'private', audience_consent_required = 0, "
                             "sharing_approval_needed = 1, pending_reason = ? "
                             "WHERE audience_consent_required = 1 AND status = 'pending'",
                             ("Private; sharing approval needed.",))
            for tier in TIERS:
                rows = self._db.execute("SELECT id FROM memories WHERE tier = ? ORDER BY id", (tier,)).fetchall()
                for number, row in enumerate(rows, 1):
                    self._db.execute("UPDATE memories SET display_number = ? WHERE id = ?", (number, row["id"]))
                self._db.execute("INSERT OR REPLACE INTO display_id_state (tier, next_number) VALUES (?, ?)",
                                 (tier, len(rows) + 1))
            self._db.execute(self._V9[1])
        if self._fts and not self._db.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'memories_fts'").fetchone():
            # A file made where FTS5 was missing, or damaged: build the search index now.
            for sql in self._V1_FTS:
                self._db.execute(sql)
            self._db.execute("INSERT INTO memories_fts(memories_fts) VALUES ('rebuild')")

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _row(row: sqlite3.Row) -> Memory:
        return Memory(
            id=row["id"], text=row["text"], tier=row["tier"], status=row["status"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            pending_reason=row["pending_reason"], forget_reason=row["forget_reason"],
            forgotten_from=row["forgotten_from"], uid=row["uid"] or "",
            duplicate_of=int(row["duplicate_of"] or 0) if "duplicate_of" in row.keys() else 0,
            audience_consent_required=bool(row["audience_consent_required"]),
            order_key=row["order_key"],
            display_number=int(row["display_number"]),
            sharing_approval_needed=bool(row["sharing_approval_needed"]),
            legacy_alias=bool(row["legacy_alias"]),
            provenance=Provenance(
                written_by=row["written_by"], via=row["via"], route=row["route"],
                route_tier=row["route_tier"], session_id=row["session_id"], turn=row["turn"],
                author=row["author"],
            ),
        )

    @staticmethod
    def _in(values: Iterable[str], allowed: tuple[str, ...]) -> tuple[str, list[str]]:
        vals = [v for v in dict.fromkeys(values) if v in allowed]
        if not vals:
            return "0", []
        return "(" + ",".join("?" * len(vals)) + ")", vals

    def _seen_order(self, uid: str) -> int:
        row = self._db.execute("SELECT order_key FROM sync_seen WHERE uid = ?", (uid,)).fetchone()
        return sync_order.parse(row[0]) if row else 0

    def observe_order(self, uid: str, value: str) -> None:
        observed = sync_order.parse(value)
        with self._lock, self._db:
            if observed > self._seen_order(uid):
                self._db.execute("INSERT OR REPLACE INTO sync_seen (uid, order_key) VALUES (?, ?)",
                                 (uid, sync_order.key(observed)))

    def _change(self, m: Memory, *, at: Optional[str] = None, order_key: Optional[str] = None,
                terminal_tie: bool = False) -> tuple[str, str]:
        """A remote change keeps its order; a local change advances beyond every observed head."""
        if order_key is not None:
            value = sync_order.key(sync_order.parse(order_key))
            return at or sync_order.display(value, now_iso()), value
        if at is not None:
            return at, sync_order.from_iso(at)
        prior = max(sync_order.parse(m.order_key), self._seen_order(m.uid))
        withdrawal = self._db.execute("SELECT order_key FROM withdrawals WHERE uid = ?", (m.uid,)).fetchone()
        if withdrawal:
            prior = max(prior, sync_order.parse(withdrawal[0]))
        tomb = self._db.execute("SELECT tier, travels, order_key FROM tombstones WHERE fingerprint = ?",
                                (fingerprint(m.text),)).fetchone()
        if tomb and (m.tier == PRIVATE or (tomb["tier"] == SHARED and tomb["travels"])):
            prior = max(prior, sync_order.parse(tomb["order_key"]))
        try:
            value = sync_order.next_key(sync_order.key(prior), now_iso())
        except sync_order.OrderError as exc:
            if terminal_tie and prior == sync_order.MAX_VALUE:
                # A genuine forget or UID withdrawal wins an active tie at the last key.
                return now_iso(), sync_order.key(prior)
            raise StoreError("The sync order limit was reached; this change was not made.") from exc
        return sync_order.display(value, now_iso()), value

    # -- Store -------------------------------------------------------------

    def claim_memory_id(self):
        with self._lock:
            if self._db.in_transaction:
                raise StoreError("A memory number must be claimed before starting a database change.")
            old_isolation = self._db.isolation_level
            self._db.isolation_level = None
            try:
                self._db.execute("BEGIN IMMEDIATE")
                try:
                    row = self._db.execute("SELECT next_id FROM memory_id_state WHERE singleton = 1").fetchone()
                    if row is None or row["next_id"] >= 2**63 - 1:
                        raise StoreError("This store has no more memory numbers available.")
                    memory_id = int(row["next_id"])
                    self._db.execute("UPDATE memory_id_state SET next_id = ? WHERE singleton = 1",
                                     (memory_id + 1,))
                    self._db.execute("COMMIT")
                    return memory_id
                except BaseException:
                    self._db.execute("ROLLBACK")
                    raise
            finally:
                self._db.isolation_level = old_isolation

    def claim_display_number(self, tier):
        tier = check_tier(tier)
        with self._lock:
            if self._db.in_transaction:
                raise StoreError("A display number must be claimed before starting a database change.")
            old_isolation = self._db.isolation_level
            self._db.isolation_level = None
            try:
                self._db.execute("BEGIN IMMEDIATE")
                try:
                    row = self._db.execute("SELECT next_number FROM display_id_state WHERE tier = ?", (tier,)).fetchone()
                    if row is None or row["next_number"] >= 2**63 - 1:
                        raise StoreError("This store has no more memory numbers available.")
                    number = int(row["next_number"])
                    self._db.execute("UPDATE display_id_state SET next_number = ? WHERE tier = ?",
                                     (number + 1, tier))
                    self._db.execute("COMMIT")
                    return number
                except BaseException:
                    self._db.execute("ROLLBACK")
                    raise
            finally:
                self._db.isolation_level = old_isolation

    def promote_shared(self, memory_id, expected_order_key, *, display_number=None, actor="assistant"):
        display_number = self.claim_display_number(SHARED) if display_number is None else int(display_number)
        with self._lock:
            old_isolation = self._db.isolation_level
            self._db.isolation_level = None
            try:
                self._db.execute("BEGIN IMMEDIATE")
                try:
                    m = self.get(memory_id)
                    if (m is None or m.tier != PRIVATE or m.status not in (ACTIVE, PENDING)
                            or m.order_key != expected_order_key):
                        raise StoreError("That Private memory changed; review it again before sharing.")
                    if self._db.execute("SELECT 1 FROM tombstones WHERE fingerprint = ?",
                                        (fingerprint(m.text),)).fetchone():
                        raise ForgottenError("These words were forgotten at the user's request.", PRIVATE)
                    stamp, key = self._change(m)
                    self._db.execute("UPDATE memories SET tier = 'shared', display_number = ?, status = 'active', "
                                     "sharing_approval_needed = 0, pending_reason = '', forget_reason = '', duplicate_of = 0, "
                                     "created_at = ?, updated_at = ?, order_key = ?, written_by = ?, via = 'sharing', "
                                     "route = '', route_tier = 'shared', session_id = '', turn = '', author = '' WHERE id = ?",
                                     (display_number, stamp, stamp, key, actor, memory_id))
                    self._db.execute("DELETE FROM withdrawals WHERE uid = ?", (m.uid,))
                    self._db.execute("INSERT INTO events (memory_id, action, actor, at) VALUES (?,?,?,?)",
                                     (memory_id, "share_approved", actor, now_iso()))
                    self._db.execute("COMMIT")
                    return self.get(memory_id)
                except BaseException:
                    self._db.execute("ROLLBACK")
                    raise
            finally:
                self._db.isolation_level = old_isolation

    def approve_private_only(self, memory_id, expected_order_key):
        with self._lock, self._db:
            m = self.get(memory_id)
            if (m is None or m.tier != PRIVATE or m.status not in (ACTIVE, PENDING) or not m.sharing_approval_needed
                    or m.order_key != expected_order_key):
                raise StoreError("That Private review changed; show it again before deciding.")
            stamp, key = self._change(m)
            self._db.execute("UPDATE memories SET status = 'active', sharing_approval_needed = 0, "
                             "pending_reason = '', forget_reason = '', updated_at = ?, order_key = ? WHERE id = ?",
                             (stamp, key, m.id))
            return self.get(m.id)

    def request_sharing_review(self, memory_id, expected_order_key):
        with self._lock, self._db:
            m = self.get(memory_id)
            if (m is None or m.tier != PRIVATE or m.status != ACTIVE or m.order_key != expected_order_key):
                raise StoreError("That Private memory changed; review it again before sharing.")
            if m.sharing_approval_needed:
                return m
            stamp, key = self._change(m)
            self._db.execute("UPDATE memories SET sharing_approval_needed = 1, pending_reason = ?, "
                             "updated_at = ?, order_key = ? WHERE id = ?",
                             ("Private; sharing approval needed.", stamp, key, m.id))
            self._db.execute("INSERT INTO events (memory_id, action, actor, at) VALUES (?,?,?,?)",
                             (m.id, "share_held", "assistant", now_iso()))
            return self.get(m.id)

    def decline_sharing(self, memory_id, expected_order_key):
        with self._lock, self._db:
            m = self.get(memory_id)
            if (m is None or m.tier != PRIVATE or not m.sharing_approval_needed
                    or m.order_key != expected_order_key):
                raise StoreError("That sharing review changed; show it again before deciding.")
            stamp, key = self._change(m)
            reason = m.pending_reason.replace("Private; sharing approval needed.", "").strip()
            if m.status == PENDING and not reason:
                reason = "Needs your review."
            self._db.execute("UPDATE memories SET sharing_approval_needed = 0, pending_reason = ?, "
                             "updated_at = ?, order_key = ? WHERE id = ?", (reason, stamp, key, m.id))
            self._db.execute("INSERT INTO events (memory_id, action, actor, at) VALUES (?,?,?,?)",
                             (m.id, "share_rejected", "user", now_iso()))
            return self.get(m.id)

    def add(self, text, tier, provenance, *, status=ACTIVE, pending_reason="", allow_forgotten=False,
            duplicate_of=0, memory_id=None, display_number=None, sharing_approval_needed=False):
        text = clean_text(text)
        tier = check_tier(tier)
        if status not in (ACTIVE, PENDING):
            raise StoreError("A new memory is either active or pending.")
        memory_id = self.claim_memory_id() if memory_id is None else int(memory_id)
        display_number = self.claim_display_number(tier) if display_number is None else int(display_number)
        stamp = now_iso()
        order_key = sync_order.from_iso(stamp)
        p = provenance
        fp = fingerprint(text)
        with self._lock, self._db:
            tomb = self._db.execute("SELECT tier, travels, order_key FROM tombstones WHERE fingerprint = ?", (fp,)).fetchone()
            if tomb:
                if not allow_forgotten:
                    raise ForgottenError(
                        "This was forgotten at the user's request, so it can't be saved again from chat.",
                        tier=tomb["tier"])
                if tier == PRIVATE or (tomb["tier"] == SHARED and tomb["travels"]):
                    try:
                        order_key = sync_order.next_key(tomb["order_key"], stamp)
                    except sync_order.OrderError as exc:
                        raise StoreError("The sync order limit was reached; this change was not made.") from exc
                    stamp = sync_order.display(order_key, stamp)
                self._db.execute("DELETE FROM tombstones WHERE fingerprint = ?", (fp,))
            cur = self._db.execute(
                """INSERT INTO memories (id, text, tier, status, pending_reason, created_at, updated_at, order_key,
                       written_by, via, route, route_tier, session_id, turn, author, uid, duplicate_of,
                       display_number, sharing_approval_needed)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (memory_id, text, tier, status, pending_reason, stamp, stamp, order_key, p.written_by, p.via, p.route,
                 p.route_tier, p.session_id, str(p.turn), p.author, new_uid(), int(duplicate_of or 0),
                 display_number, int(sharing_approval_needed)),
            )
            return self.get(cur.lastrowid)

    def get(self, memory_id):
        if isinstance(memory_id, str) and re.fullmatch(r"[PpSs][1-9][0-9]*", memory_id):
            return self.resolve_reference(memory_id, allow_legacy=False)
        with self._lock:
            row = self._db.execute("SELECT * FROM memories WHERE id = ?", (int(memory_id),)).fetchone()
        return self._row(row) if row else None

    def resolve_reference(self, token, *, allow_legacy=True):
        word = str(token or "").strip()
        if re.fullmatch(r"[PpSs][1-9][0-9]*", word):
            tier = PRIVATE if word[0].lower() == "p" else SHARED
            with self._lock:
                row = self._db.execute("SELECT * FROM memories WHERE tier = ? AND display_number = ?",
                                       (tier, int(word[1:]))).fetchone()
            return self._row(row) if row else None
        if allow_legacy and re.fullmatch(r"#?[1-9][0-9]*", word):
            m = self.get(int(word.lstrip("#")))
            return m if m is not None and m.legacy_alias else None
        return None

    def list(self, *, statuses=(ACTIVE,), tiers=TIERS, limit=50):
        s_sql, s_vals = self._in(statuses, STATUSES)
        t_sql, t_vals = self._in(tiers, TIERS)
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM memories WHERE audience_consent_required = 0 AND status IN {s_sql} AND tier IN {t_sql} "
                "ORDER BY updated_at DESC, id DESC LIMIT ?",
                (*s_vals, *t_vals, int(limit)),
            ).fetchall()
        return [self._row(r) for r in rows]

    def count(self, *, statuses=(ACTIVE,), tiers=TIERS):
        s_sql, s_vals = self._in(statuses, STATUSES)
        t_sql, t_vals = self._in(tiers, TIERS)
        with self._lock:
            return self._db.execute(
                f"SELECT COUNT(*) FROM memories WHERE audience_consent_required = 0 AND status IN {s_sql} AND tier IN {t_sql}",
                (*s_vals, *t_vals),
            ).fetchone()[0]

    def search(self, query, *, tiers=TIERS, limit=10):
        t_sql, t_vals = self._in(tiers, TIERS)
        if self._fts:
            match = fts_query(query)
            if not match:
                return []
            sql = (f"SELECT m.* FROM memories_fts f JOIN memories m ON m.id = f.rowid "
                   f"WHERE memories_fts MATCH ? AND m.audience_consent_required = 0 AND m.status = 'active' AND m.tier IN {t_sql} "
                   f"ORDER BY bm25(memories_fts) LIMIT ?")
            args = (match, *t_vals, int(limit))
        else:  # pragma: no cover - only on SQLite builds without FTS5
            words = [w for w in _WORD.findall(query or "") if len(w) > 1]
            if not words:
                return []
            like = " OR ".join("m.text LIKE ?" for _ in words)
            sql = (f"SELECT m.* FROM memories m WHERE ({like}) AND m.audience_consent_required = 0 AND m.status = 'active' "
                   f"AND m.tier IN {t_sql} ORDER BY m.updated_at DESC LIMIT ?")
            args = (*[f"%{w}%" for w in words], *t_vals, int(limit))
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [self._row(r) for r in rows]

    def set_status(self, memory_id, status, *, pending_reason=""):
        if status not in STATUSES:
            raise StoreError(f"Unknown status {status!r}.")
        if status == FORGOTTEN:
            return self.forget(memory_id)  # a forget always leaves a tombstone
        with self._lock, self._write():
            m = self.get(memory_id)
            if m is None:
                raise StoreError(f"No memory #{memory_id}.")
            if m.audience_consent_required:
                raise StoreError("That old sharing row is unavailable for this change.")
            if m.sharing_approval_needed and status == ACTIVE:
                raise StoreError("This Private save needs the numbered sharing review choice.")
            stamp, order_key = self._change(m)
            self._db.execute(
                "UPDATE memories SET status = ?, pending_reason = ?, "
                "forget_reason = CASE WHEN ? = 'active' THEN '' ELSE forget_reason END, "
                "updated_at = ?, order_key = ? WHERE id = ?",
                (status, pending_reason if status == PENDING else "", status, stamp, order_key, m.id),
            )
            if status == ACTIVE:
                self._db.execute("DELETE FROM withdrawals WHERE uid = ?", (m.uid,))
            return self.get(m.id)

    def ensure_change_possible(self, memory_id):
        """Check the next local order before a multi-memory owner choice changes other rows."""
        with self._lock:
            m = self.get(memory_id)
            if m is None:
                raise StoreError(f"No memory #{memory_id}.")
            self._change(m)

    @contextmanager
    def atomic_choice(self):
        """Keep a multi-row owner choice together and block competing SQLite writers."""
        with self._lock:
            started = not self._db.in_transaction
            self._db.execute("BEGIN IMMEDIATE" if started else "SAVEPOINT wl_owner_choice")
            self._choice_depth += 1
            try:
                yield
                self._db.execute("COMMIT" if started else "RELEASE SAVEPOINT wl_owner_choice")
            except BaseException:
                if started:
                    self._db.execute("ROLLBACK")
                else:
                    self._db.execute("ROLLBACK TO SAVEPOINT wl_owner_choice")
                    self._db.execute("RELEASE SAVEPOINT wl_owner_choice")
                raise
            finally:
                self._choice_depth -= 1

    @contextmanager
    def _write(self):
        if self._choice_depth:
            yield
        else:
            with self._db:
                yield

    def forget(self, memory_id, *, at=None, order_key=None, travels=None, everywhere=False):
        with self._lock, self._write():
            m = self.get(memory_id)
            if m is None:
                raise StoreError(f"No memory #{memory_id}.")
            if m.audience_consent_required and not (
                    m.tier == SHARED and at is not None and order_key is not None and travels is True):
                raise StoreError("That old sharing row is unavailable for this change.")
            if m.status == FORGOTTEN:
                tomb = self.tombstone(fingerprint(m.text))
                tomb_order = self.tombstone_order(fingerprint(m.text))
                needs_global = (order_key is not None and travels is True and (
                    tomb is None or tomb[1] != SHARED or not tomb[2] or
                    tomb_order is None or sync_order.parse(tomb_order) < sync_order.parse(order_key)))
                if order_key is not None and (
                        sync_order.parse(order_key) > sync_order.parse(m.order_key) or
                        (sync_order.parse(order_key) == sync_order.parse(m.order_key) and needs_global)):
                    stamp, change_order = self._change(m, at=at, order_key=order_key)
                    self._db.execute("UPDATE memories SET updated_at = ?, order_key = ? WHERE id = ?",
                                     (stamp, change_order, m.id))
                    self._tombstone(m.text, m.id, m.tier, stamp, travels=True, order_key=change_order)
                    return self.get(m.id)
                return m
            stamp, change_order = self._change(m, at=at, order_key=order_key,
                                                terminal_tie=at is None and order_key is None)
            self._db.execute("UPDATE memories SET status = 'forgotten', forgotten_from = ?, duplicate_of = 0, "
                             "forget_reason = '', updated_at = ?, order_key = ? WHERE id = ?",
                             (m.status, stamp, change_order, m.id))
            if travels is None:
                travels = m.tier == SHARED and (m.status == ACTIVE or everywhere)
            self._tombstone(m.text, m.id, m.tier, stamp, travels, change_order)
            return self.get(m.id)

    def _tombstone(self, text: str, memory_id: Optional[int], tier: str, at: Optional[str] = None,
                   travels: bool = False, order_key: Optional[str] = None) -> None:
        self._tombstone_fp(fingerprint(text), memory_id, tier, at or now_iso(), travels, order_key)

    def _tombstone_fp(self, fp: str, memory_id: Optional[int], tier: str, at: str, travels: bool = False,
                      order_key: Optional[str] = None) -> None:
        # Keep the strictest tier if the same words were forgotten more than once, and keep a
        # forget that travels travelling.
        cols = "tier, travels, order_key" if self._has_column("tombstones", "order_key") else (
            "tier, travels" if self._has_column("tombstones", "travels") else "tier")
        old = self._db.execute(f"SELECT {cols} FROM tombstones WHERE fingerprint = ?", (fp,)).fetchone()
        if old and old["tier"] == PRIVATE:
            tier = PRIVATE
        if "travels" not in cols:       # only while migrating an older file
            self._db.execute("INSERT OR REPLACE INTO tombstones (fingerprint, memory_id, tier, created_at) "
                             "VALUES (?,?,?,?)", (fp, memory_id, tier, at))
            return
        travels = bool(travels or (old and old["travels"])) and tier == SHARED
        if "order_key" in cols:
            change_order = order_key or sync_order.from_iso(at)
            if old and sync_order.parse(old["order_key"]) > sync_order.parse(change_order):
                change_order = old["order_key"]
                at = self._db.execute("SELECT created_at FROM tombstones WHERE fingerprint = ?", (fp,)).fetchone()[0]
            self._db.execute("INSERT OR REPLACE INTO tombstones "
                             "(fingerprint, memory_id, tier, created_at, travels, order_key) VALUES (?,?,?,?,?,?)",
                             (fp, memory_id, tier, at, int(travels), change_order))
        else:
            self._db.execute("INSERT OR REPLACE INTO tombstones (fingerprint, memory_id, tier, created_at, travels) "
                             "VALUES (?,?,?,?,?)", (fp, memory_id, tier, at, int(travels)))

    def restore(self, memory_id, *, at=None, order_key=None):
        with self._lock, self._db:
            m = self.get(memory_id)
            if m is None:
                raise StoreError(f"No memory #{memory_id}.")
            if m.audience_consent_required and not (
                    m.tier == SHARED and at is not None and order_key is not None):
                raise StoreError("That old sharing row cannot be restored.")
            if m.status != FORGOTTEN:
                raise StoreError(f"Memory #{m.id} is not forgotten.")
            stamp, change_order = self._change(m, at=at, order_key=order_key)
            fp = fingerprint(m.text)
            others = self._db.execute(
                "SELECT text FROM memories WHERE status = 'forgotten' AND id != ?", (m.id,)).fetchall()
            if not any(fingerprint(r["text"]) == fp for r in others):
                # Keep the tombstone if another forgotten memory has the same words, and never lift
                # a Private one by restoring a Shared memory (it may stand for a purged Private fact).
                self._db.execute("DELETE FROM tombstones WHERE fingerprint = ? AND (tier = ? OR ? = 'private')",
                                 (fp, m.tier, m.tier))
            # Back to what it was: a forgotten pending memory comes back pending, never active.
            back = PENDING if m.forgotten_from == PENDING else ACTIVE
            self._db.execute("UPDATE memories SET status = ?, forgotten_from = '', updated_at = ?, order_key = ? WHERE id = ?",
                             (back, stamp, change_order, m.id))
            if back == PENDING and m.audience_consent_required:
                self._db.execute("INSERT INTO events (memory_id, action, actor, at) VALUES (?,?,?,?)",
                                 (m.id, "share_held", "sync", now_iso()))
            if back == ACTIVE:
                self._db.execute("DELETE FROM withdrawals WHERE uid = ?", (m.uid,))
            return self.get(m.id)

    def withdraw_approval(self, memory_id, reason="You undid the approval.", *, require_sharing_choice=False):
        """Keep a Shared approval pending and publish a UID-only withdrawal atomically."""
        private_number = self.claim_display_number(PRIVATE) if require_sharing_choice else None
        with self._lock, self._db:
            m = self.get(memory_id)
            if m is None or m.tier != SHARED or m.status != ACTIVE or m.audience_consent_required:
                raise StoreError(f"No active Shared memory #{memory_id} to withdraw.")
            stamp, order_key = self._change(m, terminal_tie=True)
            self._db.execute("UPDATE memories SET status = 'pending', pending_reason = ?, "
                             "tier = ?, display_number = ?, audience_consent_required = 0, "
                             "sharing_approval_needed = ?, duplicate_of = CASE WHEN ? THEN 0 ELSE duplicate_of END, "
                             "updated_at = ?, order_key = ? WHERE id = ?",
                             (reason, PRIVATE if require_sharing_choice else SHARED,
                              private_number if require_sharing_choice else m.display_number,
                              int(require_sharing_choice), int(require_sharing_choice), stamp, order_key, m.id))
            self._db.execute("INSERT OR REPLACE INTO withdrawals "
                             "(uid, fingerprint, updated_at, created_at, origin, order_key) VALUES (?,?,?,?,?,?)",
                             (m.uid, fingerprint(m.text), stamp, m.created_at, "", order_key))
            if require_sharing_choice:
                self._db.execute("INSERT INTO events (memory_id, action, actor, at) VALUES (?,?,?,?)",
                                 (m.id, "share_held", "user", stamp))
            return self.get(m.id)

    @staticmethod
    def _withdrawal(row):
        return Withdrawal(row["uid"], row["fingerprint"], row["updated_at"], row["created_at"],
                          row["origin"], row["order_key"])

    def withdrawal(self, uid):
        with self._lock:
            row = self._db.execute("SELECT * FROM withdrawals WHERE uid = ?", (str(uid),)).fetchone()
        return self._withdrawal(row) if row else None

    def withdrawals_for_sync(self):
        with self._lock:
            rows = self._db.execute("SELECT * FROM withdrawals ORDER BY uid").fetchall()
        return [self._withdrawal(row) for row in rows]

    def apply_withdrawal(self, uid, fp, at, created_at, origin, order_key):
        """Record a UID-only withdrawal; keep a local pending row pending and never hash-forget peers."""
        order_key = sync_order.key(sync_order.parse(order_key))
        with self._lock, self._db:
            old = self.withdrawal(uid)
            if old and sync_order.parse(old.order_key) > sync_order.parse(order_key):
                return
            self._db.execute("INSERT OR REPLACE INTO withdrawals "
                             "(uid, fingerprint, updated_at, created_at, origin, order_key) VALUES (?,?,?,?,?,?)",
                             (str(uid), str(fp), str(at), str(created_at), str(origin), order_key))
            m = self.by_uid(uid)
            if m is not None and m.tier == SHARED and m.status == ACTIVE and sync_order.parse(order_key) >= sync_order.parse(m.order_key):
                self._db.execute("UPDATE memories SET status = 'forgotten', forgotten_from = 'active', "
                                 "forget_reason = '', updated_at = ?, order_key = ? WHERE id = ?",
                                 (str(at), order_key, m.id))
                self._db.execute("INSERT INTO events (memory_id, action, actor, at) VALUES (?,?,?,?)",
                                 (m.id, "synced_withdraw", "sync", now_iso()))

    def clear_withdrawal_if_older(self, uid, order_key):
        value = sync_order.parse(order_key)
        with self._lock, self._db:
            old = self.withdrawal(uid)
            if old and sync_order.parse(old.order_key) < value:
                self._db.execute("DELETE FROM withdrawals WHERE uid = ?", (str(uid),))

    def set_duplicate_of(self, memory_id, other_id):
        with self._lock, self._write():
            m = self.get(memory_id)
            if m is None:
                raise StoreError(f"No memory #{memory_id}.")
            if m.audience_consent_required:
                raise StoreError("That old sharing row is unavailable for this change.")
            self._db.execute("UPDATE memories SET duplicate_of = ? WHERE id = ?", (int(other_id or 0), int(memory_id)))
            return self.get(memory_id)

    def flagged(self):
        with self._lock:
            rows = self._db.execute("SELECT * FROM memories WHERE duplicate_of != 0 "
                                    "AND audience_consent_required = 0 AND status IN ('active', 'pending') ORDER BY id").fetchall()
        return [self._row(r) for r in rows]

    def purge(self, memory_id):
        with self._lock, self._db:
            m = self.get(memory_id)
            if m is None:
                raise StoreError(f"No memory #{memory_id}.")
            if m.audience_consent_required and m.status != FORGOTTEN:
                raise StoreError("That old sharing row must be forgotten before purging it.")
            if m.status != FORGOTTEN:
                raise StoreError(f"Forget memory #{m.id} before purging it.")
            self._db.execute("UPDATE tombstones SET memory_id = NULL WHERE memory_id = ?", (m.id,))
            self._db.execute("UPDATE events SET undone = 1 WHERE memory_id = ?", (m.id,))   # nothing to undo
            self._db.execute("DELETE FROM memories WHERE id = ?", (m.id,))

    def is_forgotten_text(self, text):
        with self._lock:
            return self._db.execute("SELECT 1 FROM tombstones WHERE fingerprint = ?",
                                    (fingerprint(text),)).fetchone() is not None

    def request_forget(self, memory_id, reason):
        with self._lock, self._db:
            m = self.get(memory_id)
            if m is None or m.status == FORGOTTEN:
                raise StoreError(f"No memory #{memory_id}.")
            if m.audience_consent_required:
                raise StoreError(f"No memory #{memory_id}.")
            stamp, order_key = self._change(m)
            self._db.execute("UPDATE memories SET forget_reason = ?, updated_at = ?, order_key = ? WHERE id = ?",
                             (str(reason or "Waiting for you.").strip()[:300], stamp, order_key, m.id))
            return self.get(m.id)

    def clear_forget_request(self, memory_id):
        with self._lock, self._db:
            m = self.get(memory_id)
            if m is None:
                raise StoreError(f"No memory #{memory_id}.")
            if m.audience_consent_required:
                raise StoreError(f"No memory #{memory_id}.")
            stamp, order_key = self._change(m)
            self._db.execute("UPDATE memories SET forget_reason = '', updated_at = ?, order_key = ? WHERE id = ?",
                             (stamp, order_key, m.id))
            return self.get(m.id)

    def forget_requests(self):
        with self._lock:
            rows = self._db.execute("SELECT * FROM memories WHERE forget_reason != '' AND status != 'forgotten' "
                                    "AND audience_consent_required = 0 ORDER BY updated_at DESC, id DESC").fetchall()
        return [self._row(r) for r in rows]

    def log_event(self, memory_id, action, actor):
        if action not in EVENT_ACTIONS:
            raise StoreError(f"Unknown event {action!r}.")
        stamp = now_iso()
        with self._lock, self._write():
            cur = self._db.execute("INSERT INTO events (memory_id, action, actor, at) VALUES (?,?,?,?)",
                                   (int(memory_id), action, str(actor or "user"), stamp))
            return Event(cur.lastrowid, int(memory_id), action, str(actor or "user"), stamp)

    def events(self, *, memory_id=None, limit=20):
        sql, args = "SELECT * FROM events", []
        if memory_id is not None:
            if isinstance(memory_id, str) and re.fullmatch(r"[PpSs][1-9][0-9]*", memory_id):
                m = self.resolve_reference(memory_id, allow_legacy=False)
                memory_id = m.id if m else 0
            sql, args = sql + " WHERE memory_id = ?", [int(memory_id)]
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, int(limit))).fetchall()
        return [Event(r["id"], r["memory_id"], r["action"], r["actor"], r["at"], bool(r["undone"])) for r in rows]

    def mark_undone(self, event_id):
        with self._lock, self._db:
            self._db.execute("UPDATE events SET undone = 1 WHERE id = ?", (int(event_id),))

    def last_event_id(self, memory_id):
        with self._lock:
            row = self._db.execute("SELECT MAX(id) FROM events WHERE memory_id = ?", (int(memory_id),)).fetchone()
        return int(row[0] or 0)

    def apply_review(self, memory_id, since_event, expected_order_key, save, reason):
        with self._lock:
            old_isolation = self._db.isolation_level
            self._db.isolation_level = None
            try:
                # IMMEDIATE: no other connection (a /memories command, the CLI) can change the
                # memory between our check and our write.
                self._db.execute("BEGIN IMMEDIATE")
                try:
                    row = self._db.execute("SELECT * "
                                           "FROM memories WHERE id = ?",
                                           (int(memory_id),)).fetchone()
                    latest = self._db.execute("SELECT MAX(id) FROM events WHERE memory_id = ?",
                                              (int(memory_id),)).fetchone()[0] or 0
                    if (row is None or row["status"] != PENDING or row["forget_reason"] or row["audience_consent_required"]
                            or row["sharing_approval_needed"]
                            or row["order_key"] != expected_order_key or latest != since_event):
                        self._db.execute("ROLLBACK")
                        return False
                    stamp, order_key = self._change(self._row(row))
                    if save:
                        self._db.execute("UPDATE memories SET status = 'active', pending_reason = '', "
                                         "updated_at = ?, order_key = ? WHERE id = ?", (stamp, order_key, int(memory_id)))
                        self._db.execute("DELETE FROM withdrawals WHERE uid = ?", (row["uid"],))
                        checked_sharing = row["tier"] == SHARED and self._db.execute(
                            "SELECT 1 FROM events WHERE memory_id = ? AND action = 'share_held' LIMIT 1",
                            (int(memory_id),)).fetchone() is not None
                        self._db.execute("INSERT INTO events (memory_id, action, actor, at) VALUES (?,?,?,?)",
                                         (int(memory_id), "share_approved" if checked_sharing else "approved",
                                          "auto-review", now_iso()))
                    else:
                        self._db.execute("UPDATE memories SET pending_reason = ?, updated_at = ?, order_key = ? WHERE id = ?",
                                         ((row["pending_reason"] + " Auto-review: " + reason).strip()[:600],
                                          stamp, order_key, int(memory_id)))
                    self._db.execute("COMMIT")
                    return True
                except BaseException:
                    self._db.execute("ROLLBACK")
                    raise
            finally:
                self._db.isolation_level = old_isolation

    def mark_outside(self, session_id, tool):
        if not session_id:
            return
        with self._lock, self._db:
            self._db.execute("INSERT OR IGNORE INTO outside_content (session_id, tool, at) VALUES (?,?,?)",
                             (str(session_id), str(tool or "a tool"), now_iso()))

    def outside_tool(self, session_id):
        if not session_id:
            return ""
        with self._lock:
            row = self._db.execute("SELECT tool FROM outside_content WHERE session_id = ?",
                                   (str(session_id),)).fetchone()
        return row["tool"] if row else ""

    # -- sync ----------------------------------------------------------------

    def by_uid(self, uid):
        with self._lock:
            row = self._db.execute("SELECT * FROM memories WHERE uid = ?", (str(uid),)).fetchone()
        return self._row(row) if row else None

    def shared_for_sync(self):
        with self._lock:
            rows = self._db.execute("SELECT * FROM memories WHERE tier = 'shared' "
                                    "AND audience_consent_required = 0 AND status IN ('active', 'forgotten') ORDER BY id").fetchall()
        return [self._row(r) for r in rows]

    def import_synced(self, uid, text, provenance, *, created_at, updated_at, status=ACTIVE, pending_reason="",
                      duplicate_of=0, order_key=None):
        text = clean_text(text)
        if status not in (ACTIVE, PENDING):
            raise StoreError("A memory arriving by sync is either active or pending.")
        memory_id = self.claim_memory_id()
        display_number = self.claim_display_number(SHARED)
        order_key = sync_order.key(sync_order.parse(order_key)) if order_key is not None else sync_order.from_iso(updated_at)
        p = provenance
        with self._lock, self._db:
            if self._db.execute("SELECT 1 FROM memories WHERE uid = ?", (str(uid),)).fetchone():
                raise StoreError("That memory is already here.")
            self._db.execute("DELETE FROM tombstones WHERE fingerprint = ? AND tier = 'shared'",
                             (fingerprint(text),))
            cur = self._db.execute(
                """INSERT INTO memories (id, text, tier, status, pending_reason, created_at, updated_at, order_key,
                       written_by, via, route, route_tier, session_id, turn, author, uid, duplicate_of, display_number)
                   VALUES (?,?,'shared',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (memory_id, text, status, pending_reason, created_at, updated_at, order_key, p.written_by, p.via, p.route,
                 p.route_tier, p.session_id, str(p.turn), p.author, str(uid), int(duplicate_of or 0), display_number),
            )
            return self.get(cur.lastrowid)

    def shared_with_fingerprint(self, fp, statuses=(ACTIVE,)):
        s_sql, s_vals = self._in(statuses, STATUSES)
        with self._lock:
            rows = self._db.execute(f"SELECT * FROM memories WHERE tier = 'shared' AND status IN {s_sql} "
                                    "AND audience_consent_required = 0 ORDER BY id", s_vals).fetchall()
        return [self._row(r) for r in rows if fingerprint(r["text"]) == fp]

    def add_tombstone(self, fp, tier, at, order_key=None):
        with self._lock, self._db:
            self._tombstone_fp(str(fp), None, check_tier(tier), at, travels=True, order_key=order_key)

    def tombstone(self, fp):
        with self._lock:
            row = self._db.execute("SELECT created_at, tier, travels FROM tombstones WHERE fingerprint = ?",
                                   (str(fp),)).fetchone()
        return (row["created_at"], row["tier"], bool(row["travels"])) if row else None

    def tombstone_order(self, fp):
        with self._lock:
            row = self._db.execute("SELECT order_key FROM tombstones WHERE fingerprint = ?",
                                   (str(fp),)).fetchone()
        return row["order_key"] if row else None

    def meta_get(self, key, default=""):
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key = ?", (str(key),)).fetchone()
        return row["value"] if row else default

    def meta_set(self, key, value):
        with self._lock, self._write():
            self._db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (str(key), str(value)))

    def close(self):
        with self._lock:
            self._db.close()
