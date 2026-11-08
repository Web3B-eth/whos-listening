"""The request guard on its own (no Hermes needed). All data is made up."""

import json

from whos_listening import guard

PRIVATE_LINE = '- #1 <wl-private id="1">Keeps a vegetarian diet on weekdays</wl-private>'
BLOCK = "<whos-listening-memory>\nThings...\n" + PRIVATE_LINE + "\n- #2 Likes spicy food\n</whos-listening-memory>"


def chat(*contents):
    return {"model": "m", "temperature": 0.2, "messages": [{"role": "user", "content": c} for c in contents]}


def test_private_route_sends_as_is():
    assert guard.guard_request(chat(BLOCK), "private") is None


def test_shared_route_strips_every_message():
    out = guard.guard_request(chat(BLOCK, "hi", BLOCK), "shared")
    sent = json.dumps(out)
    assert "vegetarian" not in sent and sent.count("spicy") == 2
    assert out["temperature"] == 0.2


def test_unknown_tier_counts_as_shared():
    assert "vegetarian" not in json.dumps(guard.guard_request(chat(BLOCK), None))


def test_nothing_to_strip_means_no_change():
    assert guard.guard_request(chat("hello"), "shared") is None


def test_other_request_shapes():
    responses = {"model": "m", "instructions": BLOCK,
                 "input": [{"role": "user", "content": [{"type": "input_text", "text": BLOCK}]}]}
    anthropic = {"model": "m", "system": [{"type": "text", "text": BLOCK}],
                 "messages": [{"role": "user", "content": "hi"}]}
    for req in (responses, anthropic):
        assert "vegetarian" not in json.dumps(guard.guard_request(req, "shared"))


def test_original_request_is_not_changed():
    req = chat(BLOCK)
    guard.guard_request(req, "shared")
    assert "vegetarian" in req["messages"][0]["content"]


def test_non_dict_request_is_left_alone():
    assert guard.guard_request(None, "shared") is None
    assert guard.guard_request(["x"], "shared") is None


def test_falls_back_to_removing_blocks(monkeypatch):
    def boom(_):
        raise RuntimeError("clean-up broke")
    monkeypatch.setattr(guard, "_strip_private_fast", boom)
    out = guard.guard_request(chat(BLOCK + " tail"), "shared")
    sent = json.dumps(out)
    assert "vegetarian" not in sent and "spicy" not in sent and "tail" in sent


def test_last_resort_drops_the_conversation(monkeypatch):
    def boom(*_):
        raise RecursionError
    monkeypatch.setattr(guard, "_walk", boom)
    out = guard.guard_request({"model": "m", "messages": [{"role": "user", "content": BLOCK}], "tools": []}, "shared")
    assert out["model"] == "m" and out["tools"] == []
    assert out["messages"] == [{"role": "user", "content": guard.BLOCKED_NOTICE}]
    out = guard.guard_request({"model": "m", "input": [BLOCK], "instructions": BLOCK}, "shared")
    assert "instructions" not in out and out["input"][0]["content"] == guard.BLOCKED_NOTICE


def test_client_objects_pass_through():
    marker = object()
    out = guard.guard_request({"messages": [{"role": "user", "content": BLOCK}], "extra": marker}, "shared")
    assert out["extra"] is marker
