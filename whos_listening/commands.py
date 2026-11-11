"""The ``/memories`` commands, as plain functions over a `Store`. The slash command and the CLI
both use them. Nothing here imports Hermes.

Private text is only shown when ``show_private`` is true: on the terminal and platforms
configured to see it, not on Shared platforms such as Telegram by default.
"""

from __future__ import annotations

import shlex
from typing import Optional

from . import duplicates, resolve, review
from .routes import RouteSettings, platform_verdict
from .store import (
    ACTIVE, FORGOTTEN, PENDING, PRIVATE, SHARED, SYNC_ACTIONS, Event, ForgottenError, Memory, Provenance, Store,
    StoreError, display_ref,
)

HELP = """\
/memories                 list what I remember
/memories review          what waits for you: possible duplicates, saves and forgets to OK
/memories keep Pn|Sn      keep a possible duplicate and forget the other
/memories keep both Pn|Sn keep it and the memory it may repeat
/memories review scan     look for repeats saved earlier (they wait for you to choose)
/memories export          write all your memories to a file
/memories pending         saves and forgets waiting for you
/memories approve Pn|Sn   let a waiting save or forget apply
/memories reject Pn|Sn    turn it down (a turned-down save can't be saved again from chat)
/memories undo            undo the last change
/memories forget Pn|Sn    forget a memory (it can't be saved again from chat)
/memories restore Pn|Sn   undo a forget
/memories show Pn|Sn      a memory's details: tier, who wrote it, on which route
/memories add [shared] TEXT   save a memory yourself (Private unless you say shared, or on a Shared gateway)
/memories sync            sync Shared memories with your other gateways now (if sync is set up)
/memories sync status     how sync is set up and how the last one went
/memories settings        the settings in effect and where each one comes from"""

HIDDEN = "(private: shown only on a Private-capable platform)"



def shows_private(platform: str, settings: Optional[RouteSettings] = None) -> bool:
    """Whether replies on ``platform`` may show Private text: the same platform rule recall uses,
    except that an empty platform does not."""
    return bool(str(platform or "").strip()) and platform_verdict(platform, settings) is None


def _text(m: Memory, show_private: bool) -> str:
    return m.text if (m.tier == SHARED or show_private) else HIDDEN


def _line(m: Memory, show_private: bool) -> str:
    return f"{display_ref(m)} [{m.tier}] {_text(m, show_private)}"


def _id(store: Store, arg: str) -> int:
    m = store.resolve_reference(arg)
    if m is None:
        raise StoreError(f"No memory {arg}.")
    return m.id


def _get(store: Store, arg: str, show_private: bool = True) -> Memory:
    m = store.resolve_reference(arg)
    if m is None or m.audience_consent_required or (m.tier == PRIVATE and not show_private):
        raise StoreError(f"No memory {arg}.")
    return m


def cmd_list(store: Store, show_private: bool) -> str:
    tiers = (PRIVATE, SHARED) if show_private else (SHARED,)
    memories = store.list(tiers=tiers, limit=200)
    waiting = sum(1 for it in resolve.items(store) if show_private or
                  all(m.tier == SHARED for m in (it.memory, *it.others)))
    lines = [_line(m, show_private) for m in memories] or ["Nothing remembered yet."]
    if waiting:
        lines.append(f"\n{waiting} waiting for you: /memories review")
    return "\n".join(lines)


def _from(m: Memory) -> str:
    route = m.provenance.route or ""
    return f"  ({route.replace('sync from', 'arrived from')})" if route.startswith("sync from") else ""


def cmd_review(store: Store, show_private: bool, scan_note: str = "") -> str:
    found = [it for it in resolve.items(store) if show_private or
             all(m.tier == SHARED for m in (it.memory, *it.others))]
    if not show_private:
        scan_note = ""     # a global scan count can include Private pairs
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
                        lines.append(f"      {display_ref(x)} also waits because: {review.shown_reason(x, x.pending_reason, show_private)}")
                keeps = "  ·  ".join(f"/memories keep {display_ref(y)}" for y in (m, *news))
                both = f"/memories keep both {display_ref(news[0])}" if len(news) == 1 else f"/memories keep both {display_ref(m)}"
                lines.append(f"   keep one: {keeps}  ·  keep all: {both}")
            elif it.kind == "save":
                lines += [f"\n{n}. A save waiting for your OK:", f"   {_line(m, show_private)}{_from(m)}",
                          f"   why: {review.shown_reason(m, m.pending_reason, show_private)}",
                          f"   /memories approve {display_ref(m)}  ·  /memories reject {display_ref(m)}"]
                if m.forget_reason:
                    lines += [f"   Forget was also requested: {review.shown_reason(m, m.forget_reason, show_private)}",
                              f"   /memories forget {display_ref(m)} forgets it instead."]
            elif it.kind == "share":
                lines += [f"\n{n}. Private; sharing approval needed:", f"   {_line(m, show_private)}{_from(m)}",
                          f"   /memories approve {display_ref(m)} shares it  ·  /memories reject {display_ref(m)} keeps it Private"]
            else:
                lines += [f"\n{n}. A forget waiting for your OK:", f"   {_line(m, show_private)}",
                          f"   why: {review.shown_reason(m, m.forget_reason, show_private)}",
                          f"   /memories approve {display_ref(m)}  ·  /memories reject {display_ref(m)}"]
        lines.append('\nOr tell the assistant, such as "keep S3" or "approve P2" (with the full number).')
    lines.append("Look for repeats saved earlier: /memories review scan" + (f"  ({scan_note})" if scan_note else ""))
    lines.append("See everything: /memories  ·  Write it all to a file: /memories export")
    return "\n".join(lines)


def cmd_keep(store: Store, args: list[str], show_private: bool) -> str:
    both = bool(args) and args[0].lower() == "both"
    rest = args[1:] if both else args
    if len(rest) != 1:
        raise StoreError("Say which one: /memories keep N, or /memories keep both N.")
    try:
        if both:
            kept = resolve.keep_both(store, _id(store, rest[0]), show_private=show_private)
            return "Kept both:\n" + "\n".join(_line(m, show_private) for m in kept)
        kept, dropped = resolve.keep(store, _id(store, rest[0]), show_private=show_private)
    except StoreError:
        if not show_private:
            raise StoreError(f"{rest[0]} isn't an available duplicate here.") from None
        raise
    gone = ", ".join(display_ref(m) for m in dropped)
    return f"Kept {_line(kept, show_private)}\nForgot {gone}. It can't be saved again from chat."


def cmd_pending(store: Store, show_private: bool) -> str:
    lines = []
    tiers = (PRIVATE, SHARED) if show_private else (SHARED,)
    for m in store.list(statuses=(PENDING,), tiers=tiers, limit=200):
        if not show_private and any(other.tier == PRIVATE for other in resolve.partners(store, m)):
            continue
        why = m.pending_reason or (f"it may say what another memory says: /memories review" if m.duplicate_of
                                   else "needs your OK")
        line = f"save {_line(m, show_private)}\n    why it waits: {review.shown_reason(m, why, show_private)}"
        if m.forget_reason:
            line += (f"\n    Forget was also requested: {review.shown_reason(m, m.forget_reason, show_private)}"
                     f"\n    /memories forget {display_ref(m)} forgets it instead.")
        lines.append(line)
    for m in store.forget_requests():
        if m.status != PENDING and (show_private or m.tier == SHARED):
            lines.append(f"forget {_line(m, show_private)}\n    why it waits: {review.shown_reason(m, m.forget_reason, show_private)}")
    if show_private:
        for m in store.list(statuses=(ACTIVE,), tiers=(PRIVATE,), limit=200):
            if m.sharing_approval_needed:
                lines.append(f"share {_line(m, True)}\n    why it waits: Private; sharing approval needed.")
    if not lines:
        return "Nothing is waiting for you."
    return "\n".join(lines) + "\n\n/memories approve Pn/Sn or /memories reject Pn/Sn"


def cmd_approve(store: Store, arg: str, show_private: bool, actor: str = "user",
                tier_duplicates: str = "ask", shared_only: bool = False) -> str:
    m = _get(store, arg, show_private)
    if m.sharing_approval_needed:
        if shared_only:
            raise StoreError("Sharing review is available only on a Private-capable gateway.")
        if tier_duplicates == PRIVATE and any(
                other.id != m.id and duplicates.repeats(m.text, other.text)
                for other in store.list(statuses=(ACTIVE, PENDING), tiers=(PRIVATE,), limit=5000)):
            kept = store.approve_private_only(m.id, m.order_key)
            store.log_event(m.id, "approved", actor)
            return f"Kept {display_ref(kept)} Private because the duplicate setting keeps Private copies."
        shared = store.promote_shared(m.id, m.order_key, actor=actor)
        return f"Approved sharing: {_line(shared, show_private)}"
    if m.status == PENDING:
        checked_sharing = m.tier == SHARED and any(
            e.action == "share_held" for e in store.events(memory_id=m.id, limit=5000))
        store.set_status(m.id, ACTIVE)
        if m.duplicate_of:
            store.set_duplicate_of(m.id, 0)         # approving a possible duplicate keeps both
        store.log_event(m.id, "share_approved" if checked_sharing else "approved", actor)
        return f"Saved {_line(store.get(m.id), show_private)}"
    if m.forget_reason and m.status != FORGOTTEN:
        store.forget(m.id)
        store.log_event(m.id, "approved_forget", actor)
        return f"Forgot {display_ref(m)}. It can't be saved again from chat."
    raise StoreError(f"{display_ref(m)} isn't waiting for you.")


def cmd_reject(store: Store, arg: str, show_private: bool, actor: str = "user") -> str:
    m = _get(store, arg, show_private)
    if m.sharing_approval_needed:
        private = store.decline_sharing(m.id, m.order_key)
        return f"Kept {display_ref(private)} Private; sharing was declined."
    if m.status == PENDING:
        store.forget(m.id)
        store.log_event(m.id, "rejected", actor)
        return f"Turned down {display_ref(m)}. It can't be saved again from chat."
    if m.forget_reason and m.status != FORGOTTEN:
        store.clear_forget_request(m.id)
        store.log_event(m.id, "rejected_forget", actor)
        return f"Kept {_line(m, show_private)}"
    raise StoreError(f"{display_ref(m)} isn't waiting for you.")


def cmd_forget(store: Store, arg: str, show_private: bool = True) -> str:
    if not show_private:
        target = store.resolve_reference(arg)
        memory_id = target.id if target is not None else 0
        if target is None or target.tier == PRIVATE or target.audience_consent_required:
            # A direct owner command may forget a Private memory from any platform. Give the
            # same answer for a missing number, so the reply does not confirm Private state.
            if target is not None and target.tier == PRIVATE and not target.audience_consent_required and target.status != FORGOTTEN:
                store.forget(memory_id)
                store.log_event(memory_id, "forgot", "user")
            return "Forget command processed. Check it on a Private-capable platform."
    m = _get(store, arg, show_private)
    if m.status == FORGOTTEN:
        raise StoreError(f"{display_ref(m)} is already forgotten.")
    store.forget(m.id)
    store.log_event(m.id, "forgot", "user")
    return f"Forgot {display_ref(m)}. It can't be saved again from chat. /memories restore {display_ref(m)} undoes this."


def cmd_restore(store: Store, arg: str, show_private: bool) -> str:
    m = _get(store, arg, show_private)
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
    if tier == PRIVATE and not show_private:
        return "Add Private memories on a Private-capable platform where you can read them."
    if tier == PRIVATE and private_refused:
        return f"{private_refused} Use `add shared TEXT` for a memory any model may see."
    m = store.add(text, tier, Provenance(written_by="user", via=via), allow_forgotten=True)
    store.log_event(m.id, "saved", "user")
    return f"Saved {_line(m, show_private)}" + (" (this gateway keeps shared memories only)" if shared_only else "")


def cmd_show(store: Store, arg: str, show_private: bool) -> str:
    m = _get(store, arg, show_private)
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
    if not show_private and m.tier == SHARED:
        # A Private row may have been promoted. Its older event names and actors are
        # owner history, not Shared-facing metadata about the now-Shared fact.
        for i, event in enumerate(history):
            if event.action == "share_approved":
                history = history[:i]
                break
    if history:
        out.append("history: " + "; ".join(
            f"{e.action} by {e.actor}" + (" (undone)" if e.undone else "") for e in reversed(history)))
    return "\n".join(out)


# What undoing each event does. Returns the message for the user.
def _undo(store: Store, e: Event, show_private: bool) -> str:
    m = store.get(e.memory_id)
    if m is None:
        raise StoreError("That memory was purged, so the last change can't be undone.")
    if e.action == "share_approved":
        if m.status != ACTIVE or m.tier != SHARED or m.audience_consent_required:
            raise StoreError("That sharing approval changed; review the current memory before deciding.")
        private = store.withdraw_approval(m.id, "Private; sharing approval needed.",
                                          require_sharing_choice=True)
        return f"Undid sharing approval. {display_ref(private)} is Private and waits in /memories review."
    if e.action in ("saved", "held", "restored", "approved"):
        if e.action == "approved":
            if m.tier == SHARED and m.status == ACTIVE:
                store.withdraw_approval(m.id, m.pending_reason or "You undid the approval.")
            else:
                store.set_status(m.id, PENDING, pending_reason=m.pending_reason or "You undid the approval.")
            return f"{display_ref(m)} is waiting for you again."
        store.forget(m.id)
        return f"Undid it: forgot {display_ref(m)}."
    if e.action in ("forgot", "rejected", "approved_forget"):
        store.restore(m.id)
        if e.action == "rejected":
            store.set_status(m.id, PENDING, pending_reason="You undid turning it down.")
            return f"{display_ref(m)} is waiting for you again."
        return f"Undid it: restored {_line(store.get(m.id), show_private)}"
    if e.action == "forget_requested":
        store.clear_forget_request(m.id)
        return f"Undid it: the request to forget {display_ref(m)} is gone."
    if e.action == "rejected_forget":
        store.request_forget(m.id, "You undid keeping it.")
        return f"The request to forget {display_ref(m)} is waiting for you again."
    raise StoreError("Nothing to undo.")


def cmd_undo(store: Store, show_private: bool) -> str:
    for e in store.events(limit=50):
        if e.undone or e.action == "undo" or e.action in SYNC_ACTIONS:
            continue        # a change that arrived by sync isn't yours to undo here
        if e.action == "share_held":
            held = store.get(e.memory_id)
            if held is not None and held.sharing_approval_needed:
                return f"{display_ref(held)} is waiting in /memories review."
            if held is not None and held.tier == SHARED and held.status == PENDING:
                return f"{display_ref(held)} is waiting for fact review."
            store.mark_undone(e.id)
            return "That sharing review is no longer waiting. Use /memories undo again for an earlier change."
        if e.action == "share_rejected":
            store.mark_undone(e.id)
            return "That sharing review was declined. Use /memories undo again for an earlier change."
        current = store.get(e.memory_id)
        if current is None:          # purged: nothing left to undo
            store.mark_undone(e.id)
            continue
        if current.tier == PRIVATE and not show_private:
            continue
        if any(x.id > e.id and x.action in SYNC_ACTIONS for x in store.events(memory_id=e.memory_id, limit=50)):
            store.mark_undone(e.id)
            return (f"Your last change to {display_ref(current)} was overtaken by a change from another gateway "
                    "(sync), so it can't be undone here.")
        if e.action == "share_approved" and current.audience_consent_required:
            store.mark_undone(e.id)
            return "That sharing approval is already waiting in /memories review."
        if current.audience_consent_required:
            continue        # a legacy hidden row is not an ordinary undo choice
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
        private_refused: str = "", shared_only: bool = False, tier_duplicates: str = "ask") -> str:
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
            return cmd_approve(store, one(), show_private, tier_duplicates=tier_duplicates,
                               shared_only=shared_only)
        if cmd == "reject":
            return cmd_reject(store, one(), show_private)
        if cmd == "forget":
            return cmd_forget(store, one(), show_private)
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
                 private_refused: str = "", shared_only: bool = False, tier_duplicates: str = "ask") -> str:
    """Open the store, run one command, close it."""
    try:
        store = store_factory()
    except Exception as exc:
        return f"Who's Listening could not open its memories: {type(exc).__name__}"
    try:
        return run(store, raw_args, show_private=show_private, via=via, private_refused=private_refused,
                   shared_only=shared_only, tier_duplicates=tier_duplicates)
    finally:
        store.close()
