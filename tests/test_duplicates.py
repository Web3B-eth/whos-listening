"""Near-duplicates (duplicates.py) and how the remember tool handles them. All data is made up."""

import json

import pytest

from whos_listening import duplicates as d
from whos_listening import settings as ws
from whos_listening import tools
from whos_listening.store import PENDING, PRIVATE, SHARED, Provenance

SAME = [
    ("Owns a Santa Cruz Hightower mountain bike.",
     "Bought a Santa Cruz Hightower mountain bike and is really happy with it."),
    ("Sister Dana is vegetarian.", "My sister Dana is vegetarian"),
    ("Allergic to shellfish.", "I am allergic to shellfish"),
    ("I ride mostly on rocky trails.", "Usually rides on rocky trails"),
    ("I try to eat paleo when I can.", "Eats paleo when possible"),
]
DIFFERENT = [
    ("Prefers window seats on long flights.", "Prefers aisle seats on short flights"),
    ("Keeps a vegetarian diet on weekdays", "Keeps a vegetarian diet on weekends"),
    ("I prefer 29-inch wheels.", "Prefers 27.5-inch wheels"),
    ("Likes spicy food", "Likes spicy Thai food"),            # more specific: kept
    ("Likes jazz", "Likes tea"),
    ("Keeps a vegetarian diet on weekdays", "Sister Dana is vegetarian"),
]


@pytest.mark.parametrize("a,b", SAME)
def test_rewordings_are_near_duplicates(a, b):
    assert d.is_near_duplicate(a, b) and d.is_near_duplicate(b, a)


@pytest.mark.parametrize("a,b", DIFFERENT)
def test_different_facts_are_not(a, b):
    assert not d.is_near_duplicate(a, b)


def test_a_shorter_restatement_adds_nothing_so_it_repeats():
    longer = "I just bought an Ibis Ripmo mountain bike and I love it."
    assert d.repeats("I own an Ibis Ripmo now.", longer)          # live test, 2026-11-10
    assert not d.repeats(longer, "I own an Ibis Ripmo now.")       # the fuller one adds detail: kept
    assert d.repeats("Likes spicy food", "Likes spicy Thai food")  # adds nothing
    assert not d.repeats("Likes spicy Thai food", "Likes spicy food")   # adds "Thai": kept
    assert not d.repeats("Prefers 27.5-inch wheels", "I prefer 29-inch wheels")
    assert not d.repeats("Likes jazz", "Likes jazz and tea")       # one shared word is too few


def test_find_respects_tiers(store):
    private = store.add("Owns a Santa Cruz Hightower mountain bike", PRIVATE, Provenance())
    text = "Bought a Santa Cruz Hightower mountain bike"
    assert d.find(store, text, PRIVATE).id == private.id
    assert d.find(store, text, SHARED) is None          # a Shared save isn't swallowed by a Private one
    shared = store.add("Sister Dana is vegetarian", SHARED, Provenance())
    assert d.find(store, "My sister Dana is vegetarian", PRIVATE).id == shared.id
    held = store.add("Allergic to shellfish", PRIVATE, Provenance(), status=PENDING, pending_reason="r")
    assert d.find(store, "I am allergic to shellfish", PRIVATE).id == held.id      # waiting ones count


def save(store, text, *, tier=PRIVATE, route=PRIVATE, mode="skip"):
    return json.loads(tools.handle(store, tools.REMEMBER, {"text": text, "tier": tier}, route_tier=route,
                                   provenance=lambda: Provenance(), duplicates=mode))


def test_the_remember_tool_skips_a_near_duplicate(store):
    first = save(store, "Owns a Santa Cruz Hightower mountain bike")["saved"]
    out = save(store, "Bought a Santa Cruz Hightower mountain bike and loves it")
    assert out["ok"] and out["duplicate"]["id"] == first["id"] and "nothing new was saved" in out["note"]
    assert store.count() == 1


def test_a_shared_route_never_learns_of_a_private_near_duplicate(store):
    save(store, "Owns a Santa Cruz Hightower mountain bike")                       # Private
    out = save(store, "Bought a Santa Cruz Hightower mountain bike", route=SHARED)
    assert out == json.loads(tools._shared_route_saved(PRIVATE, None))           # looks like any save
    assert "Hightower" not in json.dumps(out) and store.count() == 1


def test_keep_mode_saves_anyway(store):
    save(store, "Owns a Santa Cruz Hightower mountain bike")
    assert save(store, "Bought a Santa Cruz Hightower mountain bike", mode="keep")["saved"]
    assert store.count() == 2


def test_the_setting(tmp_path):
    f = tmp_path / "settings.json"
    s = ws.load(ws.Sources(f, {}, False))
    assert ws.route_settings(s).duplicates == "skip" and "near-duplicates: skip  (default)" in ws.describe(s)
    s = ws.load(ws.Sources(f, {"duplicates": "keep"}, False))
    assert ws.route_settings(s).duplicates == "keep" and "near-duplicates: keep  (Hermes settings)" in ws.describe(s)
    f.write_text('{"duplicates": "sometimes"}')
    assert ws.route_settings(ws.load(ws.Sources(f, {}, False))).duplicates == "skip"
