"""The ``/memories`` commands, as plain functions over a `Store`. The slash command and the CLI
both use them. Nothing here imports Hermes.

Private text is only shown when ``show_private`` is true: in the terminal and the local apps,
not in a chat app such as Telegram, whose servers would see the reply.
"""

from __future__ import annotations

import shlex
from typing import Optional

from . import duplicates, resolve, review
from .recall import private_span
from .routes import RouteSettings, platform_verdict
from .store import (
    ACTIVE, FORGOTTEN, PENDING, PRIVATE, SHARED, SYNC_ACTIONS, Event, ForgottenError, Memory, Provenance, Store,
    SQLiteStore, StoreError,
)

HELP = """\
/memories                 list what I remember
/memories review          what waits for you: possible duplicates, saves and forgets to OK
/memories keep N          keep #N of a possible duplicate and forget the other
/memories keep both N     keep #N and the memory it may repeat
/memories review scan     look for repeats saved earlier (they wait for you to choose)
/memories export          write all your memories to a file
/memories pending         saves and forgets waiting for you
/memories approve N       let a waiting save or forget apply
/memories reject N        turn it down (a turned-down save can't be saved again from chat)
/memories undo            undo the last change
/memories forget N        forget a memory (it can't be saved again from chat)
/memories restore N       undo a forget
/memories show N          a memory's details: tier, who wrote it, on which route
/memories add [shared] TEXT   save a memory yourself (Private unless you say shared, or on a Shared gateway)
/memories sync            sync Shared memories with your other gateways now (if sync is set up)
/memories sync status     how sync is set up and how the last one went
/memories settings        the settings in effect and where each one comes from"""

HIDDEN = "(private: shown only in the terminal or the local app)"



def shows_private(platform: str, settings: Optional[RouteSettings] = None) -> bool:
    """Whether replies on ``platform`` may show Private text: the same platform rule recall uses,
    except that an empty platform does not."""
    return bool(str(platform or "").strip()) and platform_verdict(platform, settings) is None


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
    if m is None or m.audience_consent_required:
        raise StoreError(f"No memory #{arg.lstrip('#')}.")
    return m


def cmd_share(store: SQLiteStore, raw_args: str) -> str:
    """Direct local slash-command path for a Shared-audience proposal (never a model tool)."""
    parts = raw_args.split()
    if not parts:
        proposals = store.consent_proposals()
        if not proposals:
            return "No sharing proposals are waiting."
        return private_span(0, "\n\n".join(_share_preview(store, m) for m in proposals))
    action = parts[0].lower()
    if action not in ("approve", "keep-both", "reject") or len(parts) != 2:
        return "Use /memories share to review proposals, then its exact approve, keep-both or reject command."
    token = parts[1]
    if not token.startswith("P") or "@" not in token:
        return "Use the full P-number and version shown by /memories share."
    number, version = token[1:].split("@", 1)
    if not number.isdecimal() or not version:
        return "Use the full P-number and version shown by /memories share."
    try:
        m, duplicate_id = store.decide_shared_proposal(int(number), version,
                                                       approve=action != "reject", keep_both=action == "keep-both")
    except StoreError as exc:
        return private_span(0, str(exc) + " Use /memories share to review it again.")
    if action == "reject":
        return private_span(0, "Declined sharing proposal P" + number + ". No tombstone was created.")
    pair = f" Kept Private #{duplicate_id} too." if duplicate_id else ""
    return private_span(0, "Approved Shared: any model may see it. " + _line(m, True) + pair)


def _share_preview(store: SQLiteStore, m: Memory) -> str:
    token = f"P{m.id}@{m.updated_at}"
    match = duplicates.find(store, m.text, SHARED, tiers=(PRIVATE,))
    note = f"\nPossible Private match: {_line(match, True)}" if match else ""
    choice = "keep-both" if match else "approve"
    return (f"Proposal {token}: {m.text}\nShared: any model may see it.{note}"
            f"\n/memories share {choice} {token}  ·  /memories share reject {token}")


def cmd_list(store: Store, show_private: bool) -> str:
    memories = store.list(limit=200)
    waiting = store.count(statuses=(PENDING,)) + len(store.forget_requests())
    lines = [_line(m, show_private) for m in memories] or ["Nothing remembered yet."]
    if waiting or store.flagged():
        lines.append(f"\n{len(resolve.items(store))} waiting for you: /memories review")
    return "\n".join(lines)


def _from(m: Memory) -> str:
    route = m.provenance.route or ""
    return f"  ({route.replace('sync from', 'arrived from')})" if route.startswith("sync from") else ""


def cmd_review(store: Store, show_private: bool, scan_note: str = "") -> str:
    found = resolve.items(store)
    if not found:
        lines = ["Nothing is waiting for you."]
    else:
        lines = [f"Waiting for you ({len(found)}):"]
        for n, it in enumerate(found, 1):
            m = it.memory
            if it.kind == "duplicate":
                news = it.others
                lines += [f"\n{n}. These may say the same thing:", f"   {_line(m, show_private)}{_from(m)}"]
                for x in news:
                    lines.append(f"   {_line(x, show_private)}{_from(x)}")
                    if x.pending_reason:
                        lines.append(f"      #{x.id} also waits because: {review.shown_reason(x, x.pending_reason, show_private)}")
                keeps = "  ·  ".join(f"/memories keep {y.id}" for y in (m, *news))
                both = f"/memories keep both {news[0].id}" if len(news) == 1 else f"/memories keep both {m.id}"
                lines.append(f"   keep one: {keeps}  ·  keep all: {both}")
            elif it.kind == "save":
                lines += [f"\n{n}. A save waiting for your OK:", f"   {_line(m, show_private)}{_from(m)}",
                          f"   why: {review.shown_reason(m, m.pending_reason, show_private)}",
                          f"   /memories approve {m.id}  ·  /memories reject {m.id}"]
                if m.forget_reason:
                    lines += [f"   Forget was also requested: {review.shown_reason(m, m.forget_reason, show_private)}",
                              f"   /memories forget {m.id} forgets it instead."]
            else:
                lines += [f"\n{n}. A forget waiting for your OK:", f"   {_line(m, show_private)}",
                          f"   why: {review.shown_reason(m, m.forget_reason, show_private)}",
                          f"   /memories approve {m.id}  ·  /memories reject {m.id}"]
        lines.append('\nOr tell the assistant, such as "keep 41" or "approve 43" (with the number).')
    lines.append("Look for repeats saved earlier: /memories review scan" + (f"  ({scan_note})" if scan_note else ""))
    lines.append("See everything: /memories  ·  Write it all to a file: /memories export")
    return "\n".join(lines)


def cmd_keep(store: Store, args: list[str], show_private: bool) -> str:
    both = bool(args) and args[0].lower() == "both"
    rest = args[1:] if both else args
    if len(rest) != 1:
        raise StoreError("Say which one: /memories keep N, or /memories keep both N.")
    if both:
        kept = resolve.keep_both(store, _id(rest[0]), show_private=show_private)
        return "Kept both:\n" + "\n".join(_line(m, show_private) for m in kept)
    kept, dropped = resolve.keep(store, _id(rest[0]), show_private=show_private)
    gone = ", ".join(f"#{m.id}" for m in dropped)
    return f"Kept {_line(kept, show_private)}\nForgot {gone}. It can't be saved again from chat."


def cmd_pending(store: Store, show_private: bool) -> str:
    lines = []
    for m in store.list(statuses=(PENDING,), limit=200):
        why = m.pending_reason or (f"it may say what #{m.duplicate_of} says: /memories review" if m.duplicate_of
                                   else "needs your OK")
        line = f"save {_line(m, show_private)}\n    why it waits: {review.shown_reason(m, why, show_private)}"
        if m.forget_reason:
            line += (f"\n    Forget was also requested: {review.shown_reason(m, m.forget_reason, show_private)}"
                     f"\n    /memories forget {m.id} forgets it instead.")
        lines.append(line)
    for m in store.forget_requests():
        if m.status != PENDING:
            lines.append(f"forget {_line(m, show_private)}\n    why it waits: {review.shown_reason(m, m.forget_reason, show_private)}")
    if not lines:
        return "Nothing is waiting for you."
    return "\n".join(lines) + "\n\n/memories approve N or /memories reject N"


def cmd_approve(store: Store, arg: str, show_private: bool, actor: str = "user") -> str:
    m = _get(store, arg)
    if m.tier == PRIVATE and not show_private and (m.status == PENDING or m.forget_reason):
        # Don't let anyone approve a Private memory they can't see here.
        return (f"#{m.id} is private, so its text isn't shown here. Approve it in the terminal or the "
                f"local app, where you can read it first.")
    if m.status == PENDING:
        if m.forget_reason:
            store.clear_forget_request(m.id)
        store.set_status(m.id, ACTIVE)
        if m.duplicate_of:
            store.set_duplicate_of(m.id, 0)         # approving a possible duplicate keeps both
        store.log_event(m.id, "approved", actor)
        return f"Saved {_line(store.get(m.id), show_private)}"
    if m.forget_reason and m.status != FORGOTTEN:
        store.forget(m.id)
        store.log_event(m.id, "approved_forget", actor)
        return f"Forgot #{m.id}. It can't be saved again from chat."
    raise StoreError(f"#{m.id} isn't waiting for you.")


def cmd_reject(store: Store, arg: str, show_private: bool, actor: str = "user") -> str:
    m = _get(store, arg)
    if m.status == PENDING:
        store.forget(m.id)
        store.log_event(m.id, "rejected", actor)
        return f"Turned down #{m.id}. It can't be saved again from chat."
    if m.forget_reason and m.status != FORGOTTEN:
        store.clear_forget_request(m.id)
        store.log_event(m.id, "rejected_forget", actor)
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


def cmd_add(store: Store, text: str, show_private: bool, via: str, private_refused: str = "",
            shared_only: bool = False) -> str:
    """``text`` exactly as typed after ``add`` (no shell-style quoting, so "don't" stays).
    ``private_refused`` says why this gateway may not keep a Private memory ("" if it may).
    ``shared_only``: this is a Shared gateway, so ``add TEXT`` saves a Shared memory."""
    tier, words = (SHARED if shared_only else PRIVATE), text.split(None, 1)
    if words and words[0].lower() in (PRIVATE, SHARED):
        tier, text = words[0].lower(), (words[1] if len(words) > 1 else "")
    if tier == PRIVATE and private_refused:
        return f"{private_refused} Use `add shared TEXT` for a memory any model may see."
    m = store.add(text, tier, Provenance(written_by="user", via=via), allow_forgotten=True)
    store.log_event(m.id, "saved", "user")
    return f"Saved {_line(m, show_private)}" + (" (this gateway keeps shared memories only)" if shared_only else "")


def cmd_show(store: Store, arg: str, show_private: bool) -> str:
    m = _get(store, arg)
    p = m.provenance
    out = [
        _line(m, show_private),
        f"status: {m.status}" + (f" ({review.shown_reason(m, m.pending_reason, show_private)})" if m.status == PENDING else "")
        + (f" (forget requested: {review.shown_reason(m, m.forget_reason, show_private)})" if m.forget_reason else ""),
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
        if e.undone or e.action == "undo" or e.action in SYNC_ACTIONS or e.action.startswith("share_"):
            continue        # a change that arrived by sync isn't yours to undo here
        current = store.get(e.memory_id)
        if current is not None and current.audience_consent_required:
            continue        # a hidden proposal is not an ordinary undo choice
        if current is None:          # purged: nothing left to undo
            store.mark_undone(e.id)
            continue
        if any(x.id > e.id and x.action in SYNC_ACTIONS for x in store.events(memory_id=e.memory_id, limit=50)):
            store.mark_undone(e.id)
            return (f"Your last change to #{e.memory_id} was overtaken by a change from another gateway "
                    "(sync), so it can't be undone here.")
        try:
            msg = _undo(store, e, show_private)
        except StoreError as exc:
            store.mark_undone(e.id)                 # never leave undo stuck on this change
            return f"That change can't be undone any more: {exc}"
        store.mark_undone(e.id)
        store.log_event(e.memory_id, "undo", "user")
        return msg
    return "Nothing to undo."


def run(store: Store, raw_args: str, *, show_private: bool, via: str = "/memories",
        private_refused: str = "", shared_only: bool = False) -> str:
    """Run one ``/memories`` command and return the reply. Never raises."""
    head = (raw_args or "").split(None, 1)
    cmd = head[0].lower() if head else "list"
    tail = head[1] if len(head) > 1 else ""
    try:
        rest = shlex.split(tail) if cmd != "add" else []
    except ValueError:
        rest = tail.split()

    def one() -> str:
        if len(rest) != 1:
            raise StoreError(f"Usage: /memories {cmd} N")
        return rest[0]

    try:
        if cmd in ("list", "ls"):
            return cmd_list(store, show_private)
        if cmd == "pending":
            return cmd_pending(store, show_private)
        if cmd == "review":
            if rest and rest[0].lower() == "scan":
                return "Scans run inside Hermes: type /memories review scan in a chat."
            return cmd_review(store, show_private, resolve.scan_status(store))
        if cmd == "keep":
            return cmd_keep(store, rest, show_private)
        if cmd == "export":
            return "Exports run inside Hermes: type /memories export in a chat."
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
            return cmd_add(store, tail, show_private, via, private_refused, shared_only)
        if cmd == "sync":
            return "Sync runs inside Hermes: type /memories sync in a chat."
        if cmd == "settings":
            return "Settings are shown inside Hermes: type /memories settings in a chat."
        if cmd in ("help", "-h", "--help"):
            return HELP
        return f"Unknown command {cmd!r}.\n\n{HELP}"
    except ForgottenError as exc:
        return str(exc)
    except StoreError as exc:
        return str(exc)
    except Exception as exc:  # a command must answer, not crash the chat
        return f"Who's Listening hit an error: {type(exc).__name__}"


def open_and_run(store_factory, raw_args: str, *, show_private: bool, via: str = "/memories",
                 private_refused: str = "", shared_only: bool = False) -> str:
    """Open the store, run one command, close it."""
    try:
        store = store_factory()
    except Exception as exc:
        return f"Who's Listening could not open its memories: {type(exc).__name__}"
    try:
        return run(store, raw_args, show_private=show_private, via=via, private_refused=private_refused,
                   shared_only=shared_only)
    finally:
        store.close()
