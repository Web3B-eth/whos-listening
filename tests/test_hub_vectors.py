"""Check the hub protocol's test cases in tests/vectors/hub/ are well formed.

These tests only keep the case files consistent with the doc (docs/hub-protocol.md), with
each other and with the local code they share (the memory block's header and the strip in
recall.py). tests/test_hub.py runs the cases for real, against the hub client and the fake
hub. All data is made up.
"""

import json
from pathlib import Path

import pytest

from whos_listening.recall import BLOCK_CLOSE, BLOCK_OPEN, format_block, strip_private
from whos_listening.routes import RouteSettings
from whos_listening.store import Memory

HUB = Path(__file__).parent / "vectors" / "hub"
WITHHELD = "[private memory withheld]"


def load(name):
    return json.loads((HUB / name).read_text(encoding="utf-8"))


FIELDS = load("fields.json")
CALLS = set(FIELDS["calls"])
VALUES = FIELDS["values"]
BASIC = {"string": str, "boolean": bool, "object": dict, "array": list, "null": type(None)}


def _is(value, kind):
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind.startswith("[$") and kind.endswith("]"):
        return isinstance(value, list) and all(_is(v, kind[1:-1]) for v in value)
    if kind.startswith("$"):
        return isinstance(value, dict) and not _problems(value, FIELDS["objects"][kind[1:]])
    return isinstance(value, BASIC[kind])


def _problems(obj, spec):
    out = []
    for key, kind in spec["required"].items():
        if key not in obj:
            out.append(f"missing {key}")
    for key, kind in {**spec["required"], **spec.get("optional", {})}.items():
        if key in obj and not any(_is(obj[key], k) for k in kind.split("|")):
            out.append(f"{key} is not {kind}")
    for key, allowed in VALUES.items():
        if isinstance(obj.get(key), str) and key in spec["required"] | spec.get("optional", {}) \
                and obj[key] not in allowed:
            out.append(f"{key}={obj[key]!r} is not one of {allowed}")
    return out


def _all(obj):
    """Every dict nested in obj, obj included."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _all(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _all(v)


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def test_fields_refer_to_known_objects():
    kinds = set()
    for spec in list(FIELDS["objects"].values()) + [FIELDS["error"]] + \
            [side for call in FIELDS["calls"].values() for side in call.values()]:
        for kind in {**spec["required"], **spec.get("optional", {})}.values():
            kinds.update(kind.split("|"))
    for kind in kinds:
        name = kind.strip("[]")
        assert name.lstrip("$") in FIELDS["objects"] if name.startswith("$") else name in set(BASIC) | {"integer"}


@pytest.mark.parametrize("case", load("messages.json"), ids=lambda c: c["name"])
def test_message_examples_match_fields(case):
    spec = FIELDS["calls"][case["call"]]
    assert _problems(case["request"], spec["request"]) == []
    assert _problems(case["response"], spec["response"]) == []
    assert case["request"]["protocol"] == 1 and case["response"]["protocol"] == 1
    for obj in _all(case):          # nested objects use the listed values too
        for key in ("tier", "route_source", "event", "tier_source", "role", "isolation"):
            if isinstance(obj.get(key), str):
                assert obj[key] in VALUES[key], (key, obj[key])


def test_every_call_has_an_example():
    assert {c["call"] for c in load("messages.json")} == CALLS


ANSWERS = load("answers.json")
EXPECT_KEYS = {
    "hello": {"ready", "tools", "roles", "save_tool", "server_outside_content", "verdict_ttl_seconds", "hello_interval_seconds",
              "wants_user_messages"},
    "context": {"block"},
    "verdict": {"tier", "keep_seconds"},
    "turn-start": {"turn_token"},
    "turn-report": {"failed"},
}


def test_block_header_is_local_modes():
    block = format_block([Memory(id=1, text="Likes jazz", tier="shared", status="active",
                                 created_at="", updated_at="")])
    assert block.split("\n")[1] == ANSWERS["block_header"]


@pytest.mark.parametrize("case", ANSWERS["cases"], ids=lambda c: c["name"])
def test_answer_cases(case):
    assert case["call"] in CALLS
    answer = case["answer"]
    assert answer.get("timeout") is True or isinstance(answer.get("status"), int)
    assert set(case["expect"]) <= EXPECT_KEYS[case["call"]]
    if case["call"] == "hello":
        assert isinstance(case["mcp_server"], str)
    if case["call"] == "context":
        assert case["local_layers"] in ("private", "shared")
        block = case["expect"]["block"]
        if block:
            lines = block.split("\n")
            assert lines[0] == BLOCK_OPEN and lines[1] == ANSWERS["block_header"] and lines[-1] == BLOCK_CLOSE
            if case["local_layers"] == "shared":
                assert "wl-private" not in block
    if case["call"] == "verdict":
        assert case["expect"]["tier"] in VALUES["tier"]
        # Only a well-formed answer is kept for the hello ttl; anything else counts as failed.
        assert (case["expect"]["keep_seconds"] == 300) == (
            answer.get("status") == 200 and isinstance(answer.get("body"), dict)
            and answer["body"].get("tier") in VALUES["tier"] and answer["body"].get("ok") is True)


TIERS = load("tiers.json")


@pytest.mark.parametrize("case", TIERS["cases"], ids=lambda c: c["name"])
def test_tier_cases(case):
    assert case["hub"] in ("private", "shared", "failed")
    assert case["tier"] in VALUES["tier"]
    assert case["settings"] == "unreadable" or isinstance(RouteSettings.from_dict(case["settings"]), RouteSettings)
    assert set(case["route"]) == {"provider", "model", "base_url"}
    if case["hub"] != "private":
        assert case["tier"] == "shared"   # local settings can only make it stricter


GUARD = load("guard.json")
HUB_TOOLS = {f"mcp__{GUARD['mcp_server']}__{t['name']}" for t in GUARD["tools"]}


def _pairs(a, b):
    """Strings at the same place in two objects of the same shape."""
    if isinstance(a, str) and isinstance(b, str):
        yield a, b
    elif isinstance(a, dict) and isinstance(b, dict):
        assert set(a) == set(b)
        for k in a:
            yield from _pairs(a[k], b[k])
    elif isinstance(a, list) and isinstance(b, list):
        assert len(a) == len(b)
        for x, y in zip(a, b):
            yield from _pairs(x, y)


def _without_arguments(obj):
    """A copy of obj with every tool call's arguments left out."""
    if isinstance(obj, dict):
        call = "name" in obj and ("arguments" in obj or "input" in obj)
        return {k: _without_arguments(v) for k, v in obj.items() if not (call and k in ("arguments", "input"))}
    if isinstance(obj, list):
        return [_without_arguments(v) for v in obj]
    return obj


def _arguments(d):
    raw = d.get("arguments", d.get("input"))
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except ValueError:
        return UNREADABLE


UNREADABLE = object()


def _check_call(name, args_in, args_out):
    """One tool call's arguments before and after the guard (docs/hub-protocol.md, The guard)."""
    if name == "tool_call":         # Hermes' Tool Search bridge: the calls inside, the same way
        if args_in is UNREADABLE:
            assert args_out == {"calls": []}
            return
        inner_in = args_in.get("calls", [args_in])
        inner_out = args_out.get("calls", [args_out])
        assert len(inner_in) == len(inner_out)
        for a, b in zip(inner_in, inner_out):
            _check_call(a.get("name"), a.get("arguments"), b.get("arguments"))
        return
    tool = next((t for t in GUARD["tools"] if f"mcp__{GUARD['mcp_server']}__{t['name']}" == name), None)
    if tool is None:            # not a hub memory tool: left alone
        assert args_out == args_in
        return
    assert "wl_turn" not in args_out
    for key, value in args_in.items():
        if key == "wl_turn":
            continue
        blank = value is not None and key in tool["redact_args"]     # whatever its type
        # A search query (or any argument of a search tool) goes to "", so a model doesn't copy
        # the placeholder into a later save; the rest to the placeholder.
        search = key == "query" or tool.get("role") in ("search", "context", "list")
        assert args_out[key] == (("" if search else WITHHELD) if blank else value), key


@pytest.mark.parametrize("case", GUARD["cases"], ids=lambda c: c["name"])
def test_guard_cases(case):
    assert case["tier"] in VALUES["tier"]
    if case["tier"] == "private":
        assert case["sent"] is None
        return
    if case["sent"] is None:
        assert not any("wl-private" in s or "wl_turn" in s for s in _strings(case["request"]))
        return
    sent = case["sent"]
    calls_in = [d for d in _all(case["request"]) if "name" in d and ("arguments" in d or "input" in d)]
    calls_out = [d for d in _all(sent) if "name" in d and ("arguments" in d or "input" in d)]
    for before, after in zip(calls_in, calls_out):
        _check_call(before["name"], _arguments(before), _arguments(after))
    # Tagged text everywhere else follows the strip in strip.json.
    for a, b in _pairs(_without_arguments(case["request"]), _without_arguments(sent)):
        assert b == strip_private(a)


def test_guard_tool_names_exist():
    named = {d["name"] for c in GUARD["cases"] for d in _all(c["request"])
             if isinstance(d.get("name"), str) and d["name"].startswith(f"mcp__{GUARD['mcp_server']}__")}
    assert HUB_TOOLS & named  # the listed tools are exercised


HUB_CASES = load("hub.json")
MUST_KEYS = {"status", "error", "no_private", "held_back_set", "tier", "private_text_after_strip",
             "private_text", "handled_as"}


@pytest.mark.parametrize("case", HUB_CASES["cases"], ids=lambda c: c["name"])
def test_hub_cases(case):
    assert case.get("level", "required") in ("required", "recommended")
    kept = {"turn_token"}
    for step in case["steps"]:
        assert step["call"] in CALLS | {"tool"}
        assert set(step["must"]) <= MUST_KEYS
        if step["call"] == "tool":
            assert isinstance(step["tool"], str) and isinstance(step["args"], dict)
            for value in _strings(step["args"]):
                if value.startswith("{") and value.endswith("}"):
                    assert value[1:-1] in kept, value
        else:
            assert "protocol" in step["request"]
            if step["request"]["protocol"] == 1 and step["must"].get("status") == 200:
                assert _problems(step["request"], FIELDS["calls"][step["call"]]["request"]) == []
        if "keep_token_as" in step:
            kept.add(step["keep_token_as"])
        if "handled_as" in step["must"]:
            assert set(step["must"]["handled_as"]) == {"route", "outside_content"}
