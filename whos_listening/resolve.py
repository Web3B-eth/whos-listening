"""Reviewing memories: possible duplicates to choose between, saves and forgets waiting for the
user, a scan for repeats saved earlier, and an export of everything to a file.

A possible duplicate is a pair: the newer memory is flagged (``duplicate_of``) with the older
one it may repeat. That happens when a Shared memory says what a Private one says (a save, or an
arrival from another gateway, under the ``tier_duplicates`` setting "ask") and when a scan finds
two memories saying the same thing. A flag doesn't hold a memory back: it stays as it was
(active, or waiting for another reason) until the user keeps one (the others are forgotten) or all.

- **Keeping one forgets the other.** A Shared memory that was active is forgotten on every
  gateway (as any forget of it is); one that arrived by sync and waits here is too, so the other
  gateway doesn't keep a copy the user chose against. A Private one stays a local decision.
- **Keeping both** clears the flag, and the pair is remembered so a scan doesn't flag it again.

The slash command and the CLI show plain text (commands.py); the assistant's review tool gets
the same choices (tools.py), but only for memory numbers named in the user's own message.
Nothing here imports Hermes.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from . import duplicates as dupes
from .store import ACTIVE, PENDING, PRIVATE, SHARED, TIERS, Memory, Store, StoreError

KEPT_BOTH_KEY = "review.kept_both"     # pairs of uids the user kept both of: a scan skips them
SCAN_KEY = "review.scan"               # how the last scan went (JSON)
EXPORT_DIR = "exports"


@dataclass(frozen=True)
class Item:
    kind: str                  # "duplicate", "save" (waiting for an OK) or "forget" (waiting for an OK)
    memory: Memory             # the older memory of a duplicate group, or the one waiting
    others: tuple[Memory, ...] = ()    # the newer memories flagged as saying what ``memory`` says


# -- pairs -----------------------------------------------------------------------------------

def _live(m: Optional[Memory]) -> bool:
    return m is not None and m.status in (ACTIVE, PENDING)


def partners(store: Store, m: Memory) -> list[Memory]:
    """The other memories in ``m``'s duplicate pairs: the one it's flagged against, or the newer
    ones flagged against it."""
    if m.duplicate_of:
        other = store.get(m.duplicate_of)
        return [other] if _live(other) else []
    return [x for x in store.flagged() if x.duplicate_of == m.id]


def items(store: Store) -> list[Item]:
    """Everything waiting for the user, oldest first: possible duplicates (grouped by the memory
    they may repeat), then saves and forgets to approve. A flag whose other memory is gone is
    cleared on the way."""
    groups: dict[int, list[Memory]] = {}
    for m in store.flagged():
        other = store.get(m.duplicate_of)
        if _live(other):
            groups.setdefault(other.id, []).append(m)
        else:
            store.set_duplicate_of(m.id, 0)
    out = [Item("duplicate", store.get(old), tuple(news)) for old, news in sorted(groups.items())]
    flagged = {m.id for news in groups.values() for m in news}
    for m in sorted(store.list(statuses=(PENDING,), limit=500), key=lambda x: x.id):
        if m.id not in flagged:
            out.append(Item("save", m))
    for m in store.forget_requests():
        out.append(Item("forget", m))
    return out


def _check_visible(ms: list[Memory], show_private: bool) -> None:
    if not show_private and any(m.tier == PRIVATE for m in ms):
        raise StoreError("That pair has a private memory, so it isn't shown here. Choose in the terminal or "
                         "the local app, where you can read it first.")


def _drop(store: Store, m: Memory, actor: str) -> None:
    if m.status == PENDING:
        # A Shared arrival waiting here is forgotten on the gateway it came from too.
        store.forget(m.id, everywhere=m.tier == SHARED and m.provenance.via == "sync")
        store.log_event(m.id, "rejected", actor)
    else:
        store.forget(m.id)
        store.log_event(m.id, "forgot", actor)


def _activate(store: Store, m: Memory, actor: str) -> None:
    if m.status == PENDING:
        store.set_status(m.id, ACTIVE)
        store.log_event(m.id, "approved", actor)
    if m.duplicate_of:
        store.set_duplicate_of(m.id, 0)


def not_flagged(memory_id: int) -> str:
    return f"#{memory_id} isn't marked as a possible duplicate."


def keep(store: Store, memory_id: int, *, show_private: bool, actor: str = "user") -> tuple[Memory, list[Memory]]:
    """Keep ``memory_id`` and forget what it may repeat: for an older memory, every newer one
    flagged against it; for a newer one, the older one (the other newer ones are then flagged
    against the one kept). Returns (kept, forgotten)."""
    m = store.get(int(memory_id))
    if not _live(m):
        raise StoreError(f"No memory #{memory_id} to keep.")
    others = partners(store, m)
    if not others:
        raise StoreError(not_flagged(m.id) + (f" To approve it: /memories approve {m.id}." if m.status == PENDING else ""))
    siblings = [x for x in store.flagged() if m.duplicate_of and x.duplicate_of == m.duplicate_of and x.id != m.id]
    _check_visible([m, *others, *siblings], show_private)
    for other in others:
        _drop(store, other, actor)
    _activate(store, m, actor)
    for x in siblings:
        store.set_duplicate_of(x.id, m.id)          # they may repeat the one kept just as well
    return store.get(m.id), [store.get(o.id) for o in others]


def keep_both(store: Store, memory_id: int, *, show_private: bool, actor: str = "user") -> list[Memory]:
    """Keep ``memory_id`` and the memories flagged with it; a scan won't flag the pair again."""
    m = store.get(int(memory_id))
    if not _live(m):
        raise StoreError(f"No memory #{memory_id}.")
    others = partners(store, m)
    if not others:
        raise StoreError(not_flagged(m.id))
    _check_visible([m, *others], show_private)
    kept = _kept_both(store)
    for other in others:
        kept.add(_pair_key(m, other))
        _activate(store, other, actor)
    _activate(store, m, actor)
    store.meta_set(KEPT_BOTH_KEY, json.dumps(sorted(kept)))
    return [store.get(x.id) for x in (m, *others)]


def _pair_key(a: Memory, b: Memory) -> str:
    return "|".join(sorted((a.uid or str(a.id), b.uid or str(b.id))))


def _kept_both(store: Store) -> set[str]:
    try:
        data = json.loads(store.meta_get(KEPT_BOTH_KEY) or "[]")
        return {str(x) for x in data} if isinstance(data, list) else set()
    except (ValueError, TypeError):
        return set()


# -- scan --------------------------------------------------------------------------------------

def scan(store: Store, judge: Optional[Callable[..., Optional[Memory]]] = None, *,
         progress: Optional[Callable[[int, int, int], None]] = None) -> int:
    """Look for memories that repeat an older one (the word check, then the judge if given) and
    flag them for the user. Never forgets anything. Returns how many it flagged."""
    memories = sorted(store.list(statuses=(ACTIVE, PENDING), limit=5000), key=lambda m: m.id)
    paired = {m.id for m in memories if m.duplicate_of} | {m.duplicate_of for m in memories if m.duplicate_of}
    found = 0
    for i, m in enumerate(memories, 1):
        kept = _kept_both(store)        # read each time: the user may choose while a slow scan runs
        if m.id not in paired and _live(store.get(m.id)):
            def ok(o: Memory, m: Memory = m) -> bool:
                return o.id < m.id and o.id not in paired and _pair_key(m, o) not in kept

            hit, best = None, -1.0
            for o in memories:
                if ok(o) and (dupes.repeats(m.text, o.text) or dupes.repeats(o.text, m.text)):
                    score, _ = dupes.similarity(m.text, o.text)
                    if score > best:
                        hit, best = o, score
            if hit is None and judge is not None:
                try:
                    hit = judge(m.text, TIERS, only=ok)
                except Exception:
                    hit = None
            if hit is not None and ok(hit) and _live(store.get(hit.id)):
                store.set_duplicate_of(m.id, hit.id)
                paired |= {m.id, hit.id}
                found += 1
        if progress is not None:
            progress(i, len(memories), found)
    return found


def scan_status(store: Store) -> str:
    try:
        s = json.loads(store.meta_get(SCAN_KEY) or "{}")
    except ValueError:
        return ""
    if not isinstance(s, dict) or not s:
        return ""
    if s.get("state") == "failed":
        return f"The last scan ({s.get('at', '?')}) stopped with an error."
    if s.get("state") == "running":
        return f"A scan is running: {s.get('done', 0)} of {s.get('total', '?')} checked, {s.get('found', 0)} found so far."
    return f"Last scan: {s.get('at', '?')}, {s.get('found', 0)} found."


def set_scan_status(store: Store, state: str, done: int = 0, total: int = 0, found: int = 0) -> None:
    store.meta_set(SCAN_KEY, json.dumps({"state": state, "done": done, "total": total, "found": found,
                                         "at": datetime.now().strftime("%Y-%m-%d %H:%M")}))


# -- export ------------------------------------------------------------------------------------

def export_markdown(store: Store, data_dir: Path, *, show_private: bool, now: Optional[datetime] = None) -> Path:
    """Write every memory (Private ones only if ``show_private``) to a Markdown file in the
    data folder's ``exports``, readable only by this user. Returns its path."""
    now = now or datetime.now()
    folder = Path(data_dir) / EXPORT_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"memories-{now.strftime('%Y%m%d-%H%M%S')}.md"
    lines = ["# Who's Listening memories", "", f"Exported {now.strftime('%Y-%m-%d %H:%M')}.", ""]
    tiers = (PRIVATE, SHARED) if show_private else (SHARED,)
    for tier in tiers:
        ms = sorted(store.list(tiers=(tier,), limit=5000), key=lambda m: m.id)
        lines += [f"## {tier.capitalize()} ({len(ms)})", ""]
        lines += [f"- #{m.id} {m.text}  _(saved {m.created_at[:10]})_" for m in ms] or ["Nothing yet."]
        lines.append("")
    waiting = [m for m in store.list(statuses=(PENDING,), tiers=tiers, limit=5000)]
    if waiting:
        lines += [f"## Waiting for you ({len(waiting)})", ""]
        lines += [f"- #{m.id} [{m.tier}] {m.text}: {m.pending_reason or 'needs your OK'}" for m in waiting]
        lines.append("")
    if not show_private:
        hidden = store.count(tiers=(PRIVATE,))
        if hidden:
            lines += [f"{hidden} private memories are left out: export them in the terminal or the local app.", ""]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path
