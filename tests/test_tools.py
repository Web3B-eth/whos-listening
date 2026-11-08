import json

from whos_listening import tools
from whos_listening.store import FORGOTTEN, PRIVATE, SHARED, Provenance


def call(store, name, args, route_tier):
    return json.loads(tools.handle(store, name, args, route_tier=route_tier,
                                   provenance=lambda: Provenance(written_by="assistant", via=name)))


def test_schemas_are_named():
    assert [s["name"] for s in tools.SCHEMAS] == list(tools.TOOL_NAMES)


def test_remember_defaults_to_private(store):
    r = call(store, tools.REMEMBER, {"text": "Is training for a 10k"}, PRIVATE)
    assert r["ok"] and r["saved"]["tier"] == PRIVATE
    assert "<wl-private" in r["saved"]["line"]
    assert store.get(r["saved"]["id"]).provenance.via == tools.REMEMBER


def test_remember_private_on_shared_route_does_not_echo(store):
    r = call(store, tools.REMEMBER, {"text": "Is training for a 10k"}, SHARED)
    assert r["ok"] and "line" not in r["saved"] and r["note"]


def test_recall_withholds_private_on_shared_route(store):
    call(store, tools.REMEMBER, {"text": "Has a nut allergy"}, PRIVATE)
    call(store, tools.REMEMBER, {"text": "Likes nut butter cookies", "tier": "shared"}, PRIVATE)
    shared = call(store, tools.RECALL, {"query": "nut"}, SHARED)
    assert [m["tier"] for m in shared["memories"]] == [SHARED]
    assert shared["private_withheld"] is True
    private = call(store, tools.RECALL, {"query": ""}, PRIVATE)
    assert {m["tier"] for m in private["memories"]} == {PRIVATE, SHARED}


def test_forget(store):
    mid = call(store, tools.REMEMBER, {"text": "Drives a blue van"}, PRIVATE)["saved"]["id"]
    r = call(store, tools.FORGET, {"id": mid}, SHARED)
    assert r["ok"] and r["forgotten"] == {"id": mid, "tier": PRIVATE}
    assert store.get(mid).status == FORGOTTEN
    assert call(store, tools.FORGET, {"id": mid}, PRIVATE)["ok"] is False
    assert call(store, tools.FORGET, {"id": "abc"}, PRIVATE)["ok"] is False


def test_bad_input_returns_error_json(store):
    assert call(store, tools.REMEMBER, {"text": ""}, PRIVATE)["ok"] is False
    assert call(store, tools.REMEMBER, {"text": "x", "tier": "top-secret"}, PRIVATE)["ok"] is False
    assert call(store, "nope", {}, PRIVATE)["ok"] is False


def test_forget_then_resave_is_blocked(store):
    saved = call(store, "wl_remember", {"text": "Likes jazz", "tier": "shared"}, "private")["saved"]
    out = call(store, "wl_forget", {"id": saved["id"]}, "private")
    assert out["ok"] is True and "won't be saved again" in out["note"]
    again = call(store, "wl_remember", {"text": "likes jazz", "tier": "shared"}, "private")
    assert again["ok"] is False and "forgotten" in again["error"]
