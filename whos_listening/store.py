"""Where memories live: a small `Store` interface and its SQLite (FTS5) version.

Nothing here imports Hermes, so it can be tested with plain pytest.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

PRIVATE = "private"
SHARED = "shared"
TIERS = (PRIVATE, SHARED)

ACTIVE = "active"
PENDING = "pending"
FORGOTTEN = "forgotten"
STATUSES = (ACTIVE, PENDING, FORGOTTEN)

MAX_TEXT = 500  # a memory is a short fact, not a document

SCHEMA_VERSION = 1


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class Provenance:
    """Who wrote a memory, on which route, and from which turn."""

    written_by: str = "unknown"      # "assistant" (a tool call), "user" (a command or the CLI)
    via: str = ""                    # the tool or command, e.g. "wl_remember", "cli"
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
    pending_reason: str = ""


class StoreError(ValueError):
    """A request the store refuses (bad tier, empty text, unknown id...)."""


def clean_text(text: str) -> str:
    text = " ".join(str(text or "").split())
    if not text:
        raise StoreError("A memory needs some text.")
    if len(text) > MAX_TEXT:
        raise StoreError(f"A memory must be {MAX_TEXT} characters or fewer.")
    return text


def check_tier(tier: str) -> str:
    tier = str(tier or "").strip().lower()
    if tier not in TIERS:
        raise StoreError("Tier must be 'private' or 'shared'.")
    return tier


class Store(ABC):
    """What the rest of the plugin needs from storage."""

    @abstractmethod
    def add(self, text: str, tier: str, provenance: Provenance, *,
            status: str = ACTIVE, pending_reason: str = "") -> Memory: ...

    @abstractmethod
    def get(self, memory_id: int) -> Optional[Memory]: ...

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
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._db.execute("PRAGMA journal_mode = WAL")
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

    def _migrate(self) -> None:
        with self._lock, self._db:
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise StoreError(f"{self.path} was made by a newer whos-listening (schema {version}).")
            if version < 1:
                self._db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS memories (
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
                    );
                    CREATE INDEX IF NOT EXISTS memories_status_tier ON memories (status, tier);
                    """
                )
                if self._fts:
                    self._db.executescript(
                        """
                        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                            USING fts5(text, content='memories', content_rowid='id');
                        CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                            INSERT INTO memories_fts(rowid, text) VALUES (new.id, new.text);
                        END;
                        CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                            INSERT INTO memories_fts(memories_fts, rowid, text) VALUES ('delete', old.id, old.text);
                        END;
                        CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF text ON memories BEGIN
                            INSERT INTO memories_fts(memories_fts, rowid, text) VALUES ('delete', old.id, old.text);
                            INSERT INTO memories_fts(rowid, text) VALUES (new.id, new.text);
                        END;
                        """
                    )
                self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _row(row: sqlite3.Row) -> Memory:
        return Memory(
            id=row["id"], text=row["text"], tier=row["tier"], status=row["status"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            pending_reason=row["pending_reason"],
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

    # -- Store -------------------------------------------------------------

    def add(self, text, tier, provenance, *, status=ACTIVE, pending_reason=""):
        text = clean_text(text)
        tier = check_tier(tier)
        if status not in (ACTIVE, PENDING):
            raise StoreError("A new memory is either active or pending.")
        stamp = now_iso()
        p = provenance
        with self._lock, self._db:
            cur = self._db.execute(
                """INSERT INTO memories (text, tier, status, pending_reason, created_at, updated_at,
                       written_by, via, route, route_tier, session_id, turn, author)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (text, tier, status, pending_reason, stamp, stamp, p.written_by, p.via, p.route,
                 p.route_tier, p.session_id, str(p.turn), p.author),
            )
            return self.get(cur.lastrowid)

    def get(self, memory_id):
        with self._lock:
            row = self._db.execute("SELECT * FROM memories WHERE id = ?", (int(memory_id),)).fetchone()
        return self._row(row) if row else None

    def list(self, *, statuses=(ACTIVE,), tiers=TIERS, limit=50):
        s_sql, s_vals = self._in(statuses, STATUSES)
        t_sql, t_vals = self._in(tiers, TIERS)
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM memories WHERE status IN {s_sql} AND tier IN {t_sql} "
                "ORDER BY updated_at DESC, id DESC LIMIT ?",
                (*s_vals, *t_vals, int(limit)),
            ).fetchall()
        return [self._row(r) for r in rows]

    def count(self, *, statuses=(ACTIVE,), tiers=TIERS):
        s_sql, s_vals = self._in(statuses, STATUSES)
        t_sql, t_vals = self._in(tiers, TIERS)
        with self._lock:
            return self._db.execute(
                f"SELECT COUNT(*) FROM memories WHERE status IN {s_sql} AND tier IN {t_sql}",
                (*s_vals, *t_vals),
            ).fetchone()[0]

    def search(self, query, *, tiers=TIERS, limit=10):
        t_sql, t_vals = self._in(tiers, TIERS)
        if self._fts:
            match = fts_query(query)
            if not match:
                return []
            sql = (f"SELECT m.* FROM memories_fts f JOIN memories m ON m.id = f.rowid "
                   f"WHERE memories_fts MATCH ? AND m.status = 'active' AND m.tier IN {t_sql} "
                   f"ORDER BY bm25(memories_fts) LIMIT ?")
            args = (match, *t_vals, int(limit))
        else:  # pragma: no cover - only on SQLite builds without FTS5
            words = [w for w in _WORD.findall(query or "") if len(w) > 1]
            if not words:
                return []
            like = " OR ".join("m.text LIKE ?" for _ in words)
            sql = (f"SELECT m.* FROM memories m WHERE ({like}) AND m.status = 'active' "
                   f"AND m.tier IN {t_sql} ORDER BY m.updated_at DESC LIMIT ?")
            args = (*[f"%{w}%" for w in words], *t_vals, int(limit))
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [self._row(r) for r in rows]

    def set_status(self, memory_id, status, *, pending_reason=""):
        if status not in STATUSES:
            raise StoreError(f"Unknown status {status!r}.")
        with self._lock, self._db:
            cur = self._db.execute(
                "UPDATE memories SET status = ?, pending_reason = ?, updated_at = ? WHERE id = ?",
                (status, pending_reason if status == PENDING else "", now_iso(), int(memory_id)),
            )
            if cur.rowcount == 0:
                raise StoreError(f"No memory #{memory_id}.")
            return self.get(memory_id)

    def close(self):
        with self._lock:
            self._db.close()
