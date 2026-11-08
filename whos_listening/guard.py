"""The request guard: the last check before a request leaves for a model.

Hermes runs ``llm_request`` middleware on every main chat request, after the request is built
and before it is sent. The guard gets the real route of that request (provider, model,
``base_url``). On a Shared route it removes every tagged Private span from every string in the
request: the system prompt, every replayed turn, tool results, everything. See docs/spike.md.

Hermes fails open: if middleware raises, the request is sent unchanged. So nothing here may
raise. If the normal clean-up fails, the guard removes whole memory blocks; if that fails too,
it replaces the conversation with a short notice so the request carries no history at all.

Nothing here imports Hermes.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from .recall import BLOCK_OPEN, strip_blocks, strip_private
from .store import PRIVATE

logger = logging.getLogger(__name__)

PRIVATE_MARK = "<wl-private"
BLOCKED_NOTICE = ("[whos-listening blocked this request: it could not check it for private "
                  "memories. Please try again.]")
# Keys that carry the conversation in the request shapes Hermes builds
# (chat completions, Anthropic messages, Responses API).
CONVERSATION_KEYS = ("messages", "input", "system", "instructions", "prompt")


def _walk(obj: Any, fn: Callable[[str], str]) -> Any:
    """A copy of ``obj`` with ``fn`` applied to every string value. Keys are left alone."""
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, dict):
        return {k: _walk(v, fn) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk(v, fn) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_walk(v, fn) for v in obj)
    return obj  # numbers, None, client objects: nothing to clean


def _strip_private_fast(text: str) -> str:
    return strip_private(text) if PRIVATE_MARK in text else text


def _strip_blocks_fast(text: str) -> str:
    return strip_blocks(text) if (PRIVATE_MARK in text or BLOCK_OPEN in text) else text


def blocked_request(request: dict) -> dict:
    """The last resort: the same request settings with no conversation in it."""
    out = {k: v for k, v in request.items() if k not in CONVERSATION_KEYS}
    if "input" in request and "messages" not in request:
        out["input"] = [{"role": "user", "content": BLOCKED_NOTICE}]
    else:
        out["messages"] = [{"role": "user", "content": BLOCKED_NOTICE}]
    return out


def guard_request(request: Any, tier: Optional[str]) -> Optional[dict]:
    """What to send instead of ``request``, or ``None`` to send it as it is.

    ``tier`` is the tier of the request's real route; anything but ``"private"`` (including
    ``None`` when it could not be worked out) is treated as Shared. Never raises.
    """
    if not isinstance(request, dict):
        return None
    if tier == PRIVATE:
        return None
    changed = False

    def strip(text: str) -> str:
        nonlocal changed
        out = _strip_private_fast(text)
        changed = changed or out != text
        return out

    try:
        cleaned = _walk(request, strip)
        return cleaned if changed else None
    except Exception as exc:
        logger.warning("whos-listening guard: clean-up failed, removing all memories: %s",
                       type(exc).__name__)
    try:
        return _walk(request, _strip_blocks_fast)
    except Exception as exc:
        logger.warning("whos-listening guard: block removal failed, blocking the request: %s",
                       type(exc).__name__)
    try:
        return blocked_request(request)
    except Exception:  # pragma: no cover - a dict comprehension over a dict
        return {"messages": [{"role": "user", "content": BLOCKED_NOTICE}]}
