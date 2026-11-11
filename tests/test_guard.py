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


# -- replayed calls to our tools ---------------------------------------------------

def test_redacts_replayed_remember_and_recall_calls():
    req = {"messages": [
        {"role": "assistant", "tool_calls": [
            {"id": "a", "type": "function", "function": {
                "name": "whos-listening_remember", "arguments": json.dumps({"text": "Has a peanut allergy"})}},
            {"id": "b", "type": "function", "function": {
                "name": "whos-listening_remember", "arguments": json.dumps({"text": "Likes jazz", "tier": "shared"})}},
            {"id": "c", "type": "function", "function": {
                "name": "whos-listening_recall", "arguments": json.dumps({"query": "peanut"})}},
            {"id": "d", "type": "function", "function": {"name": "web_search", "arguments": '{"q": "peanut"}'}},
        ]}]}
    out = guard.guard_request(req, "shared")
    calls = out["messages"][0]["tool_calls"]
    assert "peanut" not in calls[0]["function"]["arguments"]
    assert "Likes jazz" not in calls[1]["function"]["arguments"]  # it may have fallen back to Private
    assert "peanut" not in calls[2]["function"]["arguments"]
    assert calls[3] == req["messages"][0]["tool_calls"][3]         # other tools untouched
    assert guard.guard_request(req, "private") is None


def test_redacts_calls_in_responses_and_anthropic_shapes():
    responses = {"input": [{"type": "function_call", "name": "whos-listening_remember", "call_id": "x",
                            "arguments": '{"text": "Has a peanut allergy", "tier": "private"}'}]}
    anthropic = {"messages": [{"role": "assistant", "content": [
        {"type": "tool_use", "id": "x", "name": "whos-listening_remember", "input": {"text": "Has a peanut allergy"}}]}]}
    broken = {"input": [{"type": "function_call", "name": "whos-listening_remember", "arguments": "{not json peanut"}]}
    for req in (responses, anthropic, broken):
        assert "peanut" not in json.dumps(guard.guard_request(req, "shared"))


def test_orphan_closing_tag():
    text = "- #3 Has a peanut allergy</wl-private>\n- #4 Likes jazz"
    out = guard.guard_request(chat(text), "shared")
    sent = json.dumps(out)
    assert "peanut" not in sent and "jazz" in sent


def test_legacy_review_result_replay_hides_untagged_reasons():
    note = "A duplicate group lists memories that may say the same thing: the user keeps one"
    old = json.dumps({"ok": True, "waiting": [
        {"kind": "save", "memory": {"id": 1, "tier": "private",
                                     "line": '- #1 <wl-private id="1">Keeps a copper notebook</wl-private>'},
         "why": "Outside content. Auto-review: The copper notebook is private"},
        {"kind": "save", "memory": {"id": 2, "tier": "shared", "line": "- #2 Likes tea"},
         "why": "Outside content. Auto-review: The copper notebook is private"},
        {"kind": "duplicate", "memories": [],
         "also_held": {"2": "Auto-review: The copper notebook is private"}},
    ], "note": note})
    out = guard.guard_request(chat(old), "shared")
    sent = out["messages"][0]["content"]
    assert "copper notebook" not in sent and "Likes tea" in sent
    parsed = json.loads(sent)
    assert all("copper notebook" not in item.get("why", "") for item in parsed["waiting"])
    assert "copper notebook" not in json.dumps(parsed["waiting"][2]["also_held"])
    structured = guard.guard_request(chat(json.loads(old)), "shared")
    assert structured is not None and "copper notebook" not in json.dumps(structured)


def test_legacy_shared_memory_reason_can_echo_private_context():
    old = json.dumps({"ok": True, "waiting": [{"kind": "save",
        "memory": {"id": 2, "tier": "shared", "line": "- #2 Likes tea"},
        "why": "Auto-review: The copper notebook is private"}],
        "note": "A duplicate group lists memories that may say the same thing"})
    out = guard.guard_request(chat(old), "shared")
    assert out is not None and "copper notebook" not in json.dumps(out)
    assert "Likes tea" in json.dumps(out)


def test_partial_legacy_review_result_is_withheld_on_shared_route():
    partial = '{"ok": true, "waiting": [{"kind": "save", "why": "The copper notebook'
    out = guard.guard_request(chat(partial), "shared")
    assert out is not None and "copper notebook" not in json.dumps(out)


def test_unrelated_waiting_json_is_left_alone():
    unrelated = json.dumps({"ok": True, "waiting": [{"why": "The copper notebook"}]})
    assert guard.guard_request(chat(unrelated), "shared") is None


def test_repeated_unclosed_private_tags_are_removed_from_shared_request():
    text = "safe " + '<wl-private id="1">' * 200 + "hidden detail"
    out = guard.guard_request(chat(text), "shared")
    assert out is not None and "hidden detail" not in json.dumps(out)
