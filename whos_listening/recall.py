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

from .store import PRIVATE, SHARED, Memory, Store

BLOCK_OPEN = "<whos-listening-memory>"
BLOCK_CLOSE = "</whos-listening-memory>"
PRIVATE_OPEN = '<wl-private id="{id}">'
PRIVATE_CLOSE = "</wl-private>"

PRIVATE_SPAN_RE = re.compile(r"<wl-private\b[^>]*>.*?</wl-private>", re.DOTALL)
# An opening tag with no closing tag (a cut-off reply, say): remove to the end.
PRIVATE_UNCLOSED_RE = re.compile(r"<wl-private\b[^>]*>.*\Z", re.DOTALL)
BLOCK_RE = re.compile(r"<whos-listening-memory>.*?(?:</whos-listening-memory>|\Z)", re.DOTALL)

SMALL_STORE = 30       # up to this many memories, prefetch carries all of them
SEARCH_LIMIT = 10
RECENT_LIMIT = 5
MAX_BLOCK_CHARS = 4000


def _escape(text: str) -> str:
    # A memory must not be able to close or fake our tags.
    return text.replace("<", "‹").replace(">", "›")


def format_line(m: Memory) -> str:
    text = _escape(m.text)
    if m.tier == PRIVATE:
        return f"- #{m.id} " + PRIVATE_OPEN.format(id=m.id) + text + PRIVATE_CLOSE
    return f"- #{m.id} {text}"


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
        "Things the user has asked you to remember (whos-listening). "
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
    """Remove every tagged Private span (and any cut-off one) from ``text``."""
    text = PRIVATE_SPAN_RE.sub("[private memory withheld]", text)
    return PRIVATE_UNCLOSED_RE.sub("[private memory withheld]", text)


def strip_blocks(text: str) -> str:
    """Remove whole memory blocks. The fallback when anything goes wrong."""
    return strip_private(BLOCK_RE.sub("", text))
