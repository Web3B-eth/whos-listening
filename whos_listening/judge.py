"""A decision model as a second judge of near-duplicates (optional, ``duplicate_judge`` setting).

The word check in duplicates.py is local and strict. When it finds no repeat, this asks one of
Venice's decision models whether the new memory repeats one of the existing memories that share
a word with it, in different words ("Allergic to peanuts" after "Has a peanut allergy"):

- **Clef** (``clef-api``): Cloudflare's decision model. Venice runs it privately, with no data kept.
- **Jev** (``jev-latest``): TypeSafe's decision model. Venice passes it on to TypeSafe
  (anonymized), so it counts as Shared unless the user trusts Venice's anonymized models.

One request per save (``POST https://api.venice.ai/api/v1/decisions``), a Choice over the
candidates plus "none". The answer counts only at a probability of at least THRESHOLD, and never
when one memory is about another person (a brother, a friend…) and the other isn't: in tests both
models sometimes took "Brother Sam lives in Denver" for "Lives in Denver". Any error, timeout or
doubt saves normally. Nothing here imports Hermes; the provider passes in the key and the tiers.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Iterable, Optional, Sequence

from .duplicates import content_words, people_words, similarity
from .store import ACTIVE, PENDING, Memory, Store

URL = "https://api.venice.ai/api/v1/decisions"
MODELS = {"clef": "clef-api", "jev": "jev-latest"}
THRESHOLD = 0.8          # in tests (42 made-up cases) neither model wrongly skipped a fact at 0.8 but
                         # Jev's people mix-ups, which the person rule below catches
CANDIDATES = 20          # existing memories sent with the new one: most words in common first, then
                         # the newest (a rewording may share no word: "Has a dog" / "Has a Golden Retriever")
TIMEOUT = 6.0

INSTRUCTIONS = (
    "Would saving `new_memory` about the user add nothing to `existing_memories`? It repeats a memory "
    "when that memory already says the same thing about the user, in any words, or says more. It does "
    "not repeat it when it adds a detail, changes a detail, contradicts it, or is about a different "
    "thing or person.")



class JudgeError(Exception):
    pass


def candidates(store: Store, text: str, tiers: Sequence[str], limit: int = CANDIDATES) -> list[Memory]:
    """Up to ``limit`` existing memories in ``tiers``: those sharing content words with ``text`` first
    (most in common first), then the newest of the rest."""
    if not content_words(text) or not tiers:
        return []
    recent = store.list(statuses=(ACTIVE, PENDING), tiers=tuple(tiers), limit=500)      # newest first
    scored = sorted(((similarity(text, m.text), i, m) for i, m in enumerate(recent)),
                    key=lambda s: (-s[0][1], -s[0][0], s[1]))
    sharing = [m for (score, shared), _, m in scored if shared]
    rest = [m for m in recent if m not in sharing]
    return (sharing + rest)[:limit]


def payload(model: str, text: str, cands: Iterable[Memory]) -> dict:
    cands = list(cands)
    criteria = {f"m{m.id}": f'It repeats existing memory {m.id}: "{m.text}"' for m in cands}
    criteria["none"] = "It repeats none of them"
    return {"model": model,
            "state": {"new_memory": text, "existing_memories": [{"id": str(m.id), "text": m.text} for m in cands]},
            "questions": {"match": {"type": "choice", "instructions": INSTRUCTIONS, "criteria": criteria}}}


def decide(answer: Any, text: str, cands: Iterable[Memory], threshold: float = THRESHOLD) -> Optional[Memory]:
    """The memory the judge says ``text`` repeats, if it's sure enough and about the same person."""
    if not isinstance(answer, dict):
        return None
    choice = str(answer.get("choice") or "")
    probs = answer.get("probabilities") if isinstance(answer.get("probabilities"), dict) else {}
    try:
        p = float(probs.get(choice, 0))
    except (TypeError, ValueError):
        return None
    if not choice.startswith("m") or p < threshold:
        return None
    by_id = {f"m{m.id}": m for m in cands}
    m = by_id.get(choice)
    if m is None or people_words(text) != people_words(m.text):
        return None
    return m


def ask(api_key: str, body: dict, *, opener: Callable[..., Any] = urllib.request.urlopen,
        timeout: float = TIMEOUT) -> dict:
    if not api_key:
        raise JudgeError("no Venice key")
    req = urllib.request.Request(URL, data=json.dumps(body).encode("utf-8"), method="POST", headers={
        "Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "User-Agent": "whos-listening"})
    try:
        with opener(req, timeout=timeout) as resp:
            out = json.loads(resp.read(200_000) or b"null")
    except urllib.error.HTTPError as exc:
        raise JudgeError(f"Venice answered {exc.code}") from None
    except Exception as exc:
        raise JudgeError(type(exc).__name__) from None
    answer = (out or {}).get("answers", {}).get("match") if isinstance(out, dict) else None
    if not isinstance(answer, dict):
        raise JudgeError("no answer")
    return answer


def find(store: Store, text: str, tiers: Sequence[str], model: str, api_key: str, *,
         opener: Callable[..., Any] = urllib.request.urlopen) -> Optional[Memory]:
    """The existing memory ``text`` repeats according to ``model``, or None (also on any error)."""
    cands = candidates(store, text, tiers)
    if not cands:
        return None
    try:
        answer = ask(api_key, payload(model, text, cands), opener=opener)
    except JudgeError:
        return None
    return decide(answer, text, cands)
