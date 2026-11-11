"""Turning memories into the text a request carries, with Private lines tagged.

Every memory block is wrapped in ``<whos-listening-memory>`` tags, and every Private memory
inside it in ``<wl-private id="N">`` tags. The guard (a later step) uses the tags to find and
remove Private text anywhere in a request, including turns Hermes replays from history, and
to remove whole blocks if anything goes wrong. See docs/spike.md, "What changes in the plan".

Nothing here imports Hermes.
"""

from __future__ import annotations

import re
from typing import Iterable

from .store import PRIVATE, SHARED, Memory, Store, display_ref

BLOCK_OPEN = "<whos-listening-memory>"
BLOCK_CLOSE = "</whos-listening-memory>"
PRIVATE_OPEN = '<wl-private id="{id}">'
PRIVATE_CLOSE = "</wl-private>"
PRIVATE_OPEN_HEAD = "<wl-private"
PRIVATE_CLOSE_HEAD = "</wl-private"
PRIVATE_WITHHELD = "[private memory withheld]"
# A marker in an unusually large request string may hide text anywhere in it. Withhold the
# whole string so a single malicious input cannot make the synchronous guard scan without bound.
MAX_PRIVATE_SCAN_CHARS = 1_000_000
BLOCK_RE = re.compile(r"<whos-listening-memory>.*?(?:</whos-listening-memory>|\Z)", re.DOTALL)

SMALL_STORE = 30       # up to this many memories, prefetch carries all of them
SEARCH_LIMIT = 10
RECENT_LIMIT = 5
MAX_BLOCK_CHARS = 4000


def _escape(text: str) -> str:
    # A memory must not be able to close or fake our tags.
    return text.replace("<", "‹").replace(">", "›")


def private_span(memory_id: int, text: str) -> str:
    """Tag Private-derived text so the request guard can remove replayed copies."""
    return PRIVATE_OPEN.format(id=memory_id) + _escape(text) + PRIVATE_CLOSE


def format_line(m: Memory) -> str:
    if m.tier == PRIVATE:
        return f"- {display_ref(m)} " + private_span(m.id, m.text)
    return f"- {display_ref(m)} {_escape(m.text)}"


def format_block(memories: Iterable[Memory], *, max_chars: int = MAX_BLOCK_CHARS) -> str:
    lines, used = [], 0
    for m in memories:
        line = format_line(m)
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return ""
    return "\n".join([
        BLOCK_OPEN,
        "Things the user has asked you to remember (Who's Listening). "
        "Use them when they help; do not repeat them back unprompted.",
        *lines,
        BLOCK_CLOSE,
    ])


def allowed_tiers(route_tier: str) -> tuple[str, ...]:
    return (PRIVATE, SHARED) if route_tier == PRIVATE else (SHARED,)


def select_for_prefetch(store: Store, query: str, route_tier: str) -> list[Memory]:
    """The memories a turn should carry, given the tier its route is believed to have."""
    tiers = allowed_tiers(route_tier)
    if store.count(tiers=tiers) <= SMALL_STORE:
        return store.list(tiers=tiers, limit=SMALL_STORE)
    picked: dict[int, Memory] = {}
    for m in store.search(query, tiers=tiers, limit=SEARCH_LIMIT):
        picked.setdefault(m.id, m)
    for m in store.list(tiers=tiers, limit=RECENT_LIMIT):
        picked.setdefault(m.id, m)
    return list(picked.values())


def strip_private(text: str) -> str:
    """Remove tagged, cut-off and orphan Private markers in linear time."""
    if "wl-private" not in text:
        return text
    if len(text) > MAX_PRIVATE_SCAN_CHARS:
        return PRIVATE_WITHHELD

    parts, pos = [], 0
    while True:
        start = text.find(PRIVATE_OPEN_HEAD, pos)
        if start < 0:
            parts.append(text[pos:])
            break
        after_head = start + len(PRIVATE_OPEN_HEAD)
        if after_head < len(text) and (text[after_head].isalnum() or text[after_head] == "_"):
            # Match the old tag boundary: <wl-private-other> is not our opening tag.
            parts.append(text[pos:after_head])
            pos = after_head
            continue
        parts.append(text[pos:start])
        tag_end = text.find(">", after_head)
        if tag_end < 0:
            parts.append(PRIVATE_WITHHELD)
            break
        tag_body = text[after_head:tag_end]
        if "<" in tag_body or "\n" in tag_body:
            # A broken opening tag could swallow later markers. We cannot locate its safe
            # boundary, so withhold this request string instead of keeping uncertain text.
            return PRIVATE_WITHHELD
        close = text.find(PRIVATE_CLOSE, tag_end + 1)
        parts.append(PRIVATE_WITHHELD)
        if close < 0:
            break
        pos = close + len(PRIVATE_CLOSE)

    # A closing tag without its opening tag means the line may contain Private text. The
    # old expression removed through the last closing tag on that line; keep that behavior.
    lines = []
    for line in "".join(parts).split("\n"):
        close = line.rfind(PRIVATE_CLOSE)
        partial = line.rfind(PRIVATE_CLOSE_HEAD)
        if partial > close:
            lines.append(PRIVATE_WITHHELD)  # a cut-off closing tag
        elif close >= 0:
            lines.append(PRIVATE_WITHHELD + line[close + len(PRIVATE_CLOSE):])
        else:
            lines.append(line)
    return "\n".join(lines)


def strip_blocks(text: str) -> str:
    """Remove whole memory blocks. The fallback when anything goes wrong."""
    return strip_private(BLOCK_RE.sub("", text))
