"""Near-duplicates: a new save that says what an existing memory already says, in other words.

Exact repeats are already caught by the store (same words, any case or punctuation). This catches
rewordings such as "Owns a Santa Cruz Hightower mountain bike" and "Bought a Santa Cruz Hightower
mountain bike and is really happy with it", which a model on a Shared route saves again because it
can't see the Private one.

The check is local and deterministic: no model, no network. It compares the content words of two
memories (filler words like "likes", "my", "really" and word endings set aside). A new save is a
near-duplicate of a memory when at least two content words are shared and either most of them are
(Jaccard >= THRESHOLD), or the new save adds no content word the memory doesn't have ("I own an Ibis
Ripmo now" after "I just bought an Ibis Ripmo mountain bike"). It is deliberately strict: "prefers window seats on long flights" and "prefers aisle seats on short
flights" share half their content words and are not duplicates.

Nothing here imports Hermes.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable, Optional

from .store import ACTIVE, PENDING, PRIVATE, SHARED, Memory, Store

THRESHOLD = 0.7          # share of content words in common (Jaccard) to count as the same fact
MIN_SHARED = 2           # ...and at least this many of them, so "likes jazz" ≠ "likes tea"
CANDIDATES = 500         # how many of the newest memories to compare against

_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")

# Words that say little about which fact this is: pronouns, articles, linking and attitude
# verbs, intensifiers and time fillers. Kept short and plain so the behaviour is predictable.
FILLER = frozenset("""
a an the and or but of to in on at for with from by as about into over after before than then so
i me my mine myself we our us you your he she they them their his her its it it's i'm i've
is am are was were be been being has have had having do does did doing will would can could should
may might must just really very quite pretty so too also still now currently recently lately
usually often always sometimes generally mostly mainly
user user's
like likes liked liking love loves loved enjoy enjoys enjoyed prefer prefers preferred
own owns owned bought buy buys got get gets has have having picked pick
happy glad stoked excited
when can if this that these those there here some any one try tries tried trying possible
""".split())


# Words that make a memory about someone other than the user. Two memories where one names such a
# person and the other doesn't (or names another) are never repeats: "Brother Sam lives in Denver"
# isn't "Lives in Denver".
# Words for the same kind of person map to one name, so "daughter" and "kid" match.
_PEOPLE_GROUPS = {
    "child": "son sons daughter daughters kid kids child children baby",
    "parent": "mom mum mother dad father parent parents",
    "partner": "wife husband partner spouse girlfriend boyfriend fiance fiancee",
    "brother": "brother brothers", "sister": "sister sisters", "friend": "friend friends",
    "coworker": "boss manager coworker colleague", "neighbor": "neighbor neighbour", "roommate": "roommate",
    "relative": "aunt uncle cousin niece nephew in-law family",
    "grandparent": "grandma grandpa grandmother grandfather grandparent",
}
PEOPLE = {w: group for group, words in _PEOPLE_GROUPS.items() for w in words.split()}
_PEOPLE_WORD = re.compile(r"[a-z]+(?:-[a-z]+)?")
# "children's hospital" or "kids' table" describe a thing, not a person the memory is about.
_NOT_A_PERSON = re.compile(r"\b(?:children|kids)['’]s?(?=\s)")


def people_words(text: str) -> frozenset[str]:
    text = _NOT_A_PERSON.sub(" ", str(text or "").lower())
    return frozenset(PEOPLE[w] for w in _PEOPLE_WORD.findall(text) if w in PEOPLE)


def _stem(word: str) -> str:
    """A light, predictable ending trim, so ride, rides, riding and rided all become "rid", and
    bike and bikes "bik". Only used to compare two memories, never shown."""
    if word.isdigit():
        return word
    for end in ("ing", "ed", "es", "s"):
        if len(word) > len(end) + 2 and word.endswith(end) and not word.endswith("ss"):
            word = word[: -len(end)]
            break
    return word[:-1] if len(word) > 3 and word.endswith("e") else word


def content_words(text: str) -> frozenset[str]:
    norm = unicodedata.normalize("NFKC", str(text or "")).casefold().replace("’", "'")
    words = (w.split("'")[0] if w.endswith("'s") else w for w in _WORD.findall(norm))
    return frozenset(_stem(w) for w in words if w and w not in FILLER and len(w) > 1)


def similarity(a: str, b: str) -> tuple[float, int]:
    """(share of content words in common, how many in common)."""
    wa, wb = content_words(a), content_words(b)
    if not wa or not wb:
        return 0.0, 0
    shared = len(wa & wb)
    return shared / len(wa | wb), shared


def is_near_duplicate(a: str, b: str) -> bool:
    score, shared = similarity(a, b)
    return shared >= MIN_SHARED and score >= THRESHOLD


def repeats(new: str, existing: str) -> bool:
    """Whether a new save says nothing an existing memory doesn't: most words shared, or every
    content word of the new one already in the existing one (at least MIN_SHARED either way)."""
    wn, we = content_words(new), content_words(existing)
    shared = len(wn & we)
    if shared < MIN_SHARED or people_words(new) != people_words(existing):
        return False
    return wn <= we or shared / len(wn | we) >= THRESHOLD


def find(store: Store, text: str, tier: str, *, statuses: Iterable[str] = (ACTIVE, PENDING)) -> Optional[Memory]:
    """An existing memory that already says what ``text`` says, or None.

    Only a memory at least as widely visible as the new one counts: a Shared save is not
    swallowed by a Private memory (the user asked for any model to see it), but a Private save
    is covered by a Shared one."""
    tiers = (PRIVATE, SHARED) if tier == PRIVATE else (SHARED,)
    best, best_score = None, 0.0
    for m in store.list(statuses=tuple(statuses), tiers=tiers, limit=CANDIDATES):
        if repeats(text, m.text):
            score, _ = similarity(text, m.text)
            if best is None or score > best_score:
                best, best_score = m, score
    return best
