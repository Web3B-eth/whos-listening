"""Deciding whether a memory the assistant wants to save may apply now, or must wait for the user.

Two checks, both cheap and local:

- **The rule screen** turns away text that a person rarely means as a lasting fact about
  themselves, and that a planted instruction often contains: links, email addresses, long
  numbers, instructions to assistants, and passwords or keys. It runs on every save the
  assistant makes (the user's own ``/memories add`` skips it).
- **Outside content.** Once a chat has read anything from outside (a web page, a file, a
  terminal, any tool but ours), text in it could be steering the assistant. From then on, the
  assistant's saves and forgets in that chat wait for the user. Auto-review (``review:
  "auto"`` in settings) can let a save through after a second check; a forget always waits.

Ported from N of One's memory safety check (``memoryRuleConcern``). Nothing here imports Hermes.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict
from typing import Iterable, Optional

from .recall import private_span
from .store import PRIVATE, Memory

RULES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"https?://|\bwww\.|\b[a-z0-9-]+\.(?:com|net|org|io|ai|app|dev|co)\b", re.I),
     "It has a link or web address."),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
     "It has an email address."),
    (re.compile(r"\+?\d[\d\s().-]{7,}\d"),
     "It has a long number, such as a phone or account number."),
    (re.compile(
        r"\b(?:ignore|disregard)\b.{0,40}\b(?:instruction|previous|above|rule)"
        r"|\b(?:assistant|ai|you)\s+(?:must|should|always|never)\b"
        r"|\b(?:always|never)\s+(?:recommend|suggest|send|share|tell|reply|use|visit|trust|click|buy)\b"
        r"|\bsystem prompt\b", re.I),
     "It reads like an instruction to assistants."),
    (re.compile(r"\b(?:password|passcode|api[ _-]?key|token|secret|pin code)\b", re.I),
     "It mentions a password or key."),
)

# Tools whose results are not outside content: ours, and Hermes' own bookkeeping tools.
# Hermes' skill tools read skills installed on this gateway (by the user, or bundled with Hermes),
# not content from outside, so loading a skill doesn't make a chat's saves wait.
TRUSTED_TOOLS = frozenset({"whos-listening_remember", "whos-listening_recall", "whos-listening_forget",
                           "whos-listening_review", "todo", "todo_list", "clarify", "skills_list", "skill_view"})

REVIEW_MODES = ("ask", "auto")      # "auto" is Auto-review (see autoreview.py)

SAFE_REASON = "Needs your review."


def shown_reason(memory: Memory, reason: str, show_private: bool, *, replayable: bool = False) -> str:
    """Keep model-written reasons out of Shared views, including reasons saved by old versions.

    An Auto-review reason can echo the recent user messages even when the memory is Shared.
    A Private-route tool result is replayable, so tag a sensitive reason there for the guard.
    """
    reason = reason or SAFE_REASON
    sensitive = memory.tier == PRIVATE or "Auto-review:" in reason
    if not show_private and sensitive:
        return SAFE_REASON
    if replayable and sensitive:
        return private_span(memory.id, reason)
    return reason


def rule_concern(text: str) -> Optional[str]:
    """The rule screen: a reason to ask the user, or None when nothing matched."""
    for pattern, reason in RULES:
        if pattern.search(text or ""):
            return reason
    return None


def is_outside_tool(tool_name: str, trusted: Iterable[str] = ()) -> bool:
    name = str(tool_name or "").strip()
    return bool(name) and name not in TRUSTED_TOOLS and name not in set(trusted)


def outside_reason(tool: str) -> str:
    return f"This chat read outside content ({tool}), so it waits for you."


def save_hold_reason(text: str, outside_tool: str) -> Optional[str]:
    """Why an assistant's save must wait for the user, or None if it can apply now.
    The rule screen comes first, so its more specific reason is the one shown."""
    return rule_concern(text) or (outside_reason(outside_tool) if outside_tool else None)


def forget_hold_reason(outside_tool: str) -> Optional[str]:
    """A forget after outside content always waits for the user."""
    if outside_tool:
        return f"This chat read outside content ({outside_tool}), so a forget waits for you."
    return None


class OutsideBook:
    """Which chats have read outside content in this process, and through which tool.

    Keyed by session id; holds tool names only. Bounded so a long-lived gateway does not grow
    it forever. The provider also keeps the flag in the store, so it survives a restart.
    """

    def __init__(self, max_sessions: int = 1024):
        self._lock = threading.Lock()
        self._seen: OrderedDict[str, str] = OrderedDict()
        self._max = max_sessions

    def saw(self, session_id: str, tool: str) -> None:
        if not session_id:
            return
        with self._lock:
            self._seen.setdefault(session_id, tool)
            self._seen.move_to_end(session_id)
            while len(self._seen) > self._max:
                self._seen.popitem(last=False)

    def get(self, session_id: str) -> str:
        with self._lock:
            return self._seen.get(session_id, "")
