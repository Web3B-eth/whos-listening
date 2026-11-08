"""The ``/memories`` commands, as plain functions over a `Store`. The slash command and the CLI
both use them. Nothing here imports Hermes.

Private text is only shown when ``show_private`` is true: in the terminal and the local apps,
not in a chat app such as Telegram, whose servers would see the reply.
"""

from __future__ import annotations

import shlex

from .store import (
    ACTIVE, FORGOTTEN, PENDING, PRIVATE, SHARED, Event, ForgottenError, Memory, Provenance, Store,
    StoreError,
)

HELP = """\
/memories                 list what I remember
/memories pending         saves and forgets waiting for you
/memories approve N       let a waiting save or forget apply
/memories reject N        turn it down (a turned-down save can't be saved again from chat)
/memories undo            undo the last change
/memories forget N        forget a memory (it can't be saved again from chat)
/memories restore N       undo a forget
/memories show N          a memory's details: tier, who wrote it, on which route
/memories add [shared] TEXT   save a memory yourself (Private unless you say shared)"""

HIDDEN = "(private: shown only in the terminal or the local app)"

# Hermes platforms where the reply stays on this computer.
LOCAL_PLATFORMS = frozenset({"", "cli", "local", "tui", "desktop"})


def shows_private(platform: str) -> bool:
    return str(platform or "").strip().lower() in LOCAL_PLATFORMS


def _text(m: Memory, show_private: bool) -> str:
    return m.text if (m.tier == SHARED or show_private) else HIDDEN


def _line(m: Memory, show_private: bool) -> str:
    return f"#{m.id} [{m.tier}] {_text(m, show_private)}"


def _id(arg: str) -> int:
    try:
        return int(str(arg).lstrip("#"))
    except ValueError:
        raise StoreError("Give a memory number, like 3 or #3.") from None


def _get(store: Store, arg: str) -> Memory:
    m = store.get(_id(arg))
    if m is None:
        raise StoreError(f"No memory #{arg.lstrip('#')}.")
    return m


def cmd_list(store: Store, show_private: bool) -> str:
    memories = store.list(limit=200)
    waiting = store.count(statuses=(PENDING,)) + len(store.forget_requests())
    lines = [_line(m, show_private) for m in memories] or ["Nothing remembered yet."]
    if waiting:
        lines.append(f"\n{waiting} waiting for you: /memories pending")
    return "\n".join(lines)


def cmd_pending(store: Store, show_private: bool) -> str:
    lines = []
    for m in store.list(statuses=(PENDING,), limit=200):
        lines.append(f"save {_line(m, show_private)}\n    why it waits: {m.pending_reason or 'needs your OK'}")
    for m in store.forget_requests():
        lines.append(f"forget {_line(m, show_private)}\n    why it waits: {m.forget_reason}")
    if not lines:
        return "Nothing is waiting for you."
    return "\n".join(lines) + "\n\n/memories approve N or /memories reject N"


def cmd_approve(store: Store, arg: str, show_private: bool) -> str:
    m = _get(store, arg)
    if m.status == PENDING:
        store.set_status(m.id, ACTIVE)
        store.log_event(m.id, "approved", "user")
        return f"Saved {_line(store.get(m.id), show_private)}"
    if m.forget_reason and m.status != FORGOTTEN:
        store.forget(m.id)
        store.log_event(m.id, "approved_forget", "user")
        return f"Forgot #{m.id}. It can't be saved again from chat."
    raise StoreError(f"#{m.id} isn't waiting for you.")


def cmd_reject(store: Store, arg: str, show_private: bool) -> str:
    m = _get(store, arg)
    if m.status == PENDING:
        store.forget(m.id)
        store.log_event(m.id, "rejected", "user")
        return f"Turned down #{m.id}. It can't be saved again from chat."
    if m.forget_reason and m.status != FORGOTTEN:
        store.clear_forget_request(m.id)
        store.log_event(m.id, "rejected_forget", "user")
        return f"Kept {_line(m, show_private)}"
    raise StoreError(f"#{m.id} isn't waiting for you.")


def cmd_forget(store: Store, arg: str) -> str:
    m = _get(store, arg)
    if m.status == FORGOTTEN:
        raise StoreError(f"#{m.id} is already forgotten.")
    store.forget(m.id)
    store.log_event(m.id, "forgot", "user")
    return f"Forgot #{m.id}. It can't be saved again from chat. /memories restore {m.id} undoes this."


def cmd_restore(store: Store, arg: str, show_private: bool) -> str:
    m = _get(store, arg)
    store.restore(m.id)
    store.log_event(m.id, "restored", "user")
    return f"Restored {_line(store.get(m.id), show_private)}"


def cmd_add(store: Store, args: list[str], show_private: bool, via: str) -> str:
    tier = PRIVATE
    if args and args[0].lower() in (PRIVATE, SHARED):
        tier, args = args[0].lower(), args[1:]
    m = store.add(" ".join(args), tier, Provenance(written_by="user", via=via), allow_forgotten=True)
    store.log_event(m.id, "saved", "user")
    return f"Saved {_line(m, show_private)}"


def cmd_show(store: Store, arg: str, show_private: bool) -> str:
    m = _get(store, arg)
    p = m.provenance
    out = [
        _line(m, show_private),
        f"status: {m.status}" + (f" ({m.pending_reason})" if m.status == PENDING else "")
        + (f" (forget requested: {m.forget_reason})" if m.forget_reason else ""),
        f"written by: {p.written_by}" + (f" via {p.via}" if p.via else ""),
        f"route: {p.route or 'unknown'}" + (f" ({p.route_tier})" if p.route_tier else ""),
        f"session: {p.session_id or '-'}  turn: {p.turn or '-'}" + (f"  author: {p.author}" if p.author else ""),
        f"created: {m.created_at}  updated: {m.updated_at}",
    ]
    history = store.events(memory_id=m.id, limit=10)
    if history:
        out.append("history: " + "; ".join(
            f"{e.action} by {e.actor}" + (" (undone)" if e.undone else "") for e in reversed(history)))
    return "\n".join(out)


# What undoing each event does. Returns the message for the user.
def _undo(store: Store, e: Event, show_private: bool) -> str:
    m = store.get(e.memory_id)
    if m is None:
        raise StoreError("That memory was purged, so the last change can't be undone.")
    if e.action in ("saved", "held", "restored", "approved"):
        if e.action == "approved":
            store.set_status(m.id, PENDING, pending_reason=m.pending_reason or "You undid the approval.")
            return f"#{m.id} is waiting for you again."
        store.forget(m.id)
        return f"Undid it: forgot #{m.id}."
    if e.action in ("forgot", "rejected", "approved_forget"):
        store.restore(m.id)
        if e.action == "rejected":
            store.set_status(m.id, PENDING, pending_reason="You undid turning it down.")
            return f"#{m.id} is waiting for you again."
        return f"Undid it: restored {_line(store.get(m.id), show_private)}"
    if e.action == "forget_requested":
        store.clear_forget_request(m.id)
        return f"Undid it: the request to forget #{m.id} is gone."
    if e.action == "rejected_forget":
        store.request_forget(m.id, "You undid keeping it.")
        return f"The request to forget #{m.id} is waiting for you again."
    raise StoreError("Nothing to undo.")


def cmd_undo(store: Store, show_private: bool) -> str:
    for e in store.events(limit=50):
        if e.undone or e.action == "undo":
            continue
        msg = _undo(store, e, show_private)
        store.mark_undone(e.id)
        store.log_event(e.memory_id, "undo", "user")
        return msg
    return "Nothing to undo."


def run(store: Store, raw_args: str, *, show_private: bool, via: str = "/memories") -> str:
    """Run one ``/memories`` command and return the reply. Never raises."""
    try:
        args = shlex.split(raw_args or "")
    except ValueError:
        args = (raw_args or "").split()
    cmd, rest = (args[0].lower(), args[1:]) if args else ("list", [])

    def one() -> str:
        if len(rest) != 1:
            raise StoreError(f"Usage: /memories {cmd} N")
        return rest[0]

    try:
        if cmd in ("list", "ls"):
            return cmd_list(store, show_private)
        if cmd == "pending":
            return cmd_pending(store, show_private)
        if cmd == "approve":
            return cmd_approve(store, one(), show_private)
        if cmd == "reject":
            return cmd_reject(store, one(), show_private)
        if cmd == "forget":
            return cmd_forget(store, one())
        if cmd == "restore":
            return cmd_restore(store, one(), show_private)
        if cmd == "undo":
            return cmd_undo(store, show_private)
        if cmd == "show":
            return cmd_show(store, one(), show_private)
        if cmd == "add":
            return cmd_add(store, rest, show_private, via)
        if cmd in ("help", "-h", "--help"):
            return HELP
        return f"Unknown command {cmd!r}.\n\n{HELP}"
    except ForgottenError as exc:
        return str(exc)
    except StoreError as exc:
        return str(exc)
    except Exception as exc:  # a command must answer, not crash the chat
        return f"whos-listening hit an error: {type(exc).__name__}"


def open_and_run(store_factory, raw_args: str, *, show_private: bool, via: str = "/memories") -> str:
    """Open the store, run one command, close it."""
    try:
        store = store_factory()
    except Exception as exc:
        return f"whos-listening could not open its memories: {type(exc).__name__}"
    try:
        return run(store, raw_args, show_private=show_private, via=via)
    finally:
        store.close()
