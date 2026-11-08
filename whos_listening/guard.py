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

import json
import logging
from typing import Any, Callable, Optional

from .recall import BLOCK_OPEN, strip_blocks, strip_private
from .store import PRIVATE, SHARED

logger = logging.getLogger(__name__)

PRIVATE_MARK = "wl-private"   # in both the opening and the closing tag
WITHHELD = "[private memory withheld]"
# Our tools whose arguments the model writes in its own words. When Hermes replays a call,
# those words are untagged, so on a Shared route they're redacted (see _redact_call).
REMEMBER, RECALL = "wl_remember", "wl_recall"
BLOCKED_NOTICE = ("[whos-listening blocked this request: it could not check it for private "
                  "memories. Please try again.]")
# Keys that carry the conversation in the request shapes Hermes builds
# (chat completions, Anthropic messages, Responses API).
CONVERSATION_KEYS = ("messages", "input", "system", "instructions", "prompt")


def _walk(obj: Any, fn: Callable[[str], str], on_dict: Optional[Callable[[dict], dict]] = None) -> Any:
    """A copy of ``obj`` with ``fn`` applied to every string value (keys are left alone), and
    ``on_dict`` to every dict before its values are walked."""
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, dict):
        if on_dict is not None:
            obj = on_dict(obj)
        return {k: _walk(v, fn, on_dict) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk(v, fn, on_dict) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_walk(v, fn, on_dict) for v in obj)
    return obj  # numbers, None, client objects: nothing to clean


def _redact_args(name: str, args: Any) -> Any:
    """``args`` with the model's own words taken out: a Private ``wl_remember`` text, and any
    ``wl_recall`` query (it may name a Private fact). Unreadable arguments are dropped."""
    if isinstance(args, str):
        try:
            parsed = json.loads(args) if args.strip() else {}
        except ValueError:
            return json.dumps({"text": WITHHELD} if name == REMEMBER else {"query": ""})
        redacted = _redact_args(name, parsed)
        return args if redacted == parsed else json.dumps(redacted, ensure_ascii=False)
    if not isinstance(args, dict):
        return {"text": WITHHELD} if name == REMEMBER else {"query": ""}
    out = dict(args)
    if name == REMEMBER and str(out.get("tier") or PRIVATE).strip().lower() != SHARED and "text" in out:
        out["text"] = WITHHELD
    if name == RECALL and out.get("query"):
        out["query"] = ""
    return out


def _redact_call(d: dict) -> dict:
    """A replayed call to one of our tools, in any of the shapes Hermes sends:
    ``{"function": {"name", "arguments"}}`` (chat completions), ``{"type": "function_call",
    "name", "arguments"}`` (Responses API) and ``{"type": "tool_use", "name", "input"}``
    (Anthropic). The ``function`` dict of the first shape is reached on its own by the walk."""
    name = d.get("name")
    if name not in (REMEMBER, RECALL):
        return d
    for key in ("arguments", "input"):
        if key in d:
            new = _redact_args(name, d[key])
            if new != d[key]:
                d = {**d, key: new}
    return d


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

    def redact(d: dict) -> dict:
        nonlocal changed
        out = _redact_call(d)
        changed = changed or out is not d
        return out

    try:
        cleaned = _walk(request, strip, redact)
        return cleaned if changed else None
    except Exception as exc:
        logger.warning("whos-listening guard: clean-up failed, removing all memories: %s",
                       type(exc).__name__)
    try:
        return _walk(request, _strip_blocks_fast, _redact_call)
    except Exception as exc:
        logger.warning("whos-listening guard: block removal failed, blocking the request: %s",
                       type(exc).__name__)
    try:
        return blocked_request(request)
    except Exception:  # pragma: no cover - a dict comprehension over a dict
        return {"messages": [{"role": "user", "content": BLOCKED_NOTICE}]}
