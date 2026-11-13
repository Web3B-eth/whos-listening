"""Hub mode's client (whos_listening/hub.py) against the shared cases in tests/vectors/hub/, and
the fake hub (e2e/fake_hub.py) against the hub's own cases. No Hermes needed. All data is made up."""

import importlib.util
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from whos_listening import guard, hub
from whos_listening.recall import strip_private
from whos_listening.routes import Route, RouteSettings, default_route_from_config

ROOT = Path(__file__).resolve().parent.parent
HUB = ROOT / "tests" / "vectors" / "hub"
TOKEN = "hub-token-for-tests-only"


def load(name):
    return json.loads((HUB / name).read_text(encoding="utf-8"))


def _fake_hub_module():
    spec = importlib.util.spec_from_file_location("wl_fake_hub", ROOT / "e2e" / "fake_hub.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fake_hub = _fake_hub_module()


# -- a hub that gives one scripted answer ------------------------------------------------

class Scripted:
    """A local HTTP server that answers every POST with ``answer`` (an answers.json answer)."""

    def __init__(self, answer):
        self.answer, self.seen = answer, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.seen.append((self.path, dict(self.headers), json.loads(body)))
                a = outer.answer
                if a.get("timeout"):
                    time.sleep(1.0)
                    return
                if "headers" in a:
                    data = b""
                elif "size_bytes" in a:
                    data = b'{"protocol": 1, "ok": true, "pad": "' + b"x" * a["size_bytes"] + b'"}'
                elif "raw" in a:
                    data = a["raw"].encode()
                else:
                    data = json.dumps(a["body"]).encode()
                self.send_response(a["status"])
                for k, v in a.get("headers", {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/whos-listening/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


SESSION = {"id": "20261111_101500_a1b2c3", "platform": "desktop", "agent_context": "primary", "outside_tool": ""}
ROUTE = {"provider": "custom", "model": "grok-4-5", "base_url": "https://api.venice.ai/api/v1"}
ANSWERS = load("answers.json")


@pytest.mark.parametrize("case", ANSWERS["cases"], ids=lambda c: c["name"])
def test_answer_cases(case):
    server = Scripted(case["answer"])
    clock = Clock()
    try:
        client = hub.HubClient(server.url, case.get("mcp_server", "hub"), clock=clock,
                               timeouts={k: 0.3 for k in hub.TIMEOUTS})
        expect, call = case["expect"], case["call"]
        if call == "hello":
            client.run_hello({"routes": []}, TOKEN)
            assert client.ready is expect["ready"]
            if client.ready:
                h = client.hello
                assert [t.wire for t in h.tools] == expect["tools"]
                if "roles" in expect:
                    assert [t.role for t in h.tools] == expect["roles"]
                if "save_tool" in expect:
                    save = h.tool_for("save")
                    assert (save.wire if save else None) == expect["save_tool"]
                for key in ("server_outside_content", "verdict_ttl_seconds", "hello_interval_seconds",
                            "wants_user_messages"):
                    if key in expect:
                        assert getattr(h, key) == expect[key], key
        elif call == "context":
            body = {"session": SESSION, "turn": {"number": "3", "id": ""}, "route": ROUTE,
                    "route_source": "last_request", "query": "Dinner?", "max_chars": 4000}
            block = client.context(body, TOKEN, keep_private=case["local_layers"] == "private")
            assert block == expect["block"]
        elif call == "verdict":
            tier, source, _ = client.verdict(SESSION, ROUTE, TOKEN)
            assert tier == expect["tier"]
            [(_, kept, left)] = client.kept_verdicts()
            assert left == expect["keep_seconds"]
            again = client.verdict(SESSION, ROUTE, TOKEN)        # kept: no second call
            assert again[0] == tier and len(server.seen) == 1
            clock.now += expect["keep_seconds"]
            assert client.kept_verdicts() == []
        elif call == "turn-start":
            client.begin_turn("s1", "3")
            assert client.turn_start("s1", {"session": SESSION}, TOKEN) == expect["turn_token"]
            assert client.current_token("s1", wait=False) == (expect["turn_token"] or "")
        elif call == "turn-report":
            assert client.turn_report({"event": "turn_end"}, TOKEN) is expect["failed"]
    finally:
        server.close()


def test_calls_send_the_token_protocol_and_json():
    server = Scripted({"status": 200, "body": {"protocol": 1, "ok": True}})
    try:
        hub.HubClient(server.url, "hub").turn_report({"event": "turn_end"}, TOKEN)
        [(path, headers, body)] = server.seen
        assert path == "/whos-listening/v1/turn-report"
        assert headers["Authorization"] == f"Bearer {TOKEN}" and headers["Content-Type"] == "application/json"
        assert body == {"protocol": 1, "event": "turn_end"}
    finally:
        server.close()


def test_no_token_means_no_call():
    server = Scripted({"status": 200, "body": {"protocol": 1, "ok": True}})
    try:
        client = hub.HubClient(server.url, "hub")
        assert client.turn_report({"event": "turn_end"}, "") is True
        assert not client.run_hello({}, "") and client.hello_error == "no_token"
        assert server.seen == []
    finally:
        server.close()


def test_unauthorized_asks_for_a_new_hello():
    server = Scripted({"status": 200, "body": {"protocol": 1, "ok": True, "hub": {"name": "H"}, "tools": []}})
    try:
        client = hub.HubClient(server.url, "hub")
        assert client.run_hello({}, TOKEN) and not client.hello_needed()
        server.answer = {"status": 401, "body": {"protocol": 1, "ok": False, "error": "unauthorized"}}
        client.turn_report({"event": "turn_end"}, TOKEN)
        assert client.hello_needed()
        assert not client.run_hello({}, TOKEN) and not client.ready
    finally:
        server.close()


def test_a_newer_turn_keeps_its_own_token():
    server = Scripted({"status": 200, "body": {"protocol": 1, "ok": True, "turn_token": "tt_oldTurn123"}})
    try:
        client = hub.HubClient(server.url, "hub")
        first = client.begin_turn("s1", "1")
        client.begin_turn("s1", "2")                      # the next turn started before the answer came
        client.turn_start("s1", {}, TOKEN, first)
        assert client.current_token("s1", wait=False) == ""
    finally:
        server.close()


# -- the plugin's tier in hub mode ---------------------------------------------------------

@pytest.mark.parametrize("case", load("tiers.json")["cases"], ids=lambda c: c["name"])
def test_tier_cases(case):
    settings = None if case["settings"] == "unreadable" else RouteSettings.from_dict(case["settings"])
    asked = []

    def ask():
        asked.append(1)
        return ("shared", "failed") if case["hub"] == "failed" else (case["hub"], "hub")

    tier, source = hub.decide_tier(settings, case["platform"], Route(**case["route"]), ask)
    assert tier == case["tier"]
    assert (source == "local") == (not asked)


def test_a_hosted_gateway_by_default_is_shared_locally():
    settings = RouteSettings.from_dict({"gateway": "shared"})
    assert hub.local_shared(settings, "desktop", Route("custom", "grok-4-5", "https://api.venice.ai/api/v1"))


def test_no_route_is_shared_locally():
    assert hub.local_shared(RouteSettings(), "desktop", None) is not None


# -- the guard in hub mode ------------------------------------------------------------------

GUARD = load("guard.json")
HELLO = hub.parse_hello({"tools": GUARD["tools"], "server_outside_content": GUARD["server_outside_content"]},
                        GUARD["mcp_server"])


def _parsed_args(obj):
    """obj with every tool call's arguments parsed, so JSON text compares as JSON."""
    if isinstance(obj, dict):
        out = {k: _parsed_args(v) for k, v in obj.items()}
        if "name" in obj and isinstance(obj.get("arguments"), str):
            out["arguments"] = json.loads(obj["arguments"])
        return out
    if isinstance(obj, list):
        return [_parsed_args(v) for v in obj]
    return obj


@pytest.mark.parametrize("case", GUARD["cases"], ids=lambda c: c["name"])
def test_guard_cases(case):
    out = guard.guard_request(case["request"], case["tier"], HELLO.redactions(), "", HELLO.search_tools())
    if case["sent"] is None:
        assert out is None
    else:
        assert _parsed_args(out) == _parsed_args(case["sent"])


def test_guard_before_hello_blanks_every_hub_server_argument():
    nested = json.dumps({"facts": [{"text": "Walked to the physio"}], "minutes": 40, "wl_turn": "tt_9QmV2c"})
    req = {"model": "m", "messages": [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "mcp__hub__log_walk",
                                                      "arguments": '{"note": "Walked to the physio", "minutes": 40, "wl_turn": "tt_9QmV2c"}'}},
        {"id": "c2", "type": "function", "function": {"name": "web_search", "arguments": '{"query": "x"}'}},
        {"id": "c3", "type": "function", "function": {"name": "mcp__hub__log_walk", "arguments": nested}}]}]}
    out = guard.guard_request(req, "shared", None, hub.mcp_server_prefix("hub"))
    calls = out["messages"][0]["tool_calls"]
    W = guard.WITHHELD
    assert json.loads(calls[0]["function"]["arguments"]) == {"note": W, "minutes": W}   # every argument
    assert calls[1] == req["messages"][0]["tool_calls"][1]
    assert json.loads(calls[2]["function"]["arguments"]) == {"facts": W, "minutes": W}   # nested ones too
    assert "physio" not in json.dumps(out)


def test_guard_blanks_redact_args_of_any_type():
    W = guard.WITHHELD
    for value in (["Walked to the physio"], {"text": "Walked to the physio"}, 40, True):
        args = json.dumps({"text": value, "tier": "private", "reason": None, "wl_turn": "tt_9QmV2c"})
        req = {"model": "m", "messages": [{"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "mcp__hub__memory_save", "arguments": args}}]}]}
        out = guard.guard_request(req, "shared", HELLO.redactions())
        assert json.loads(out["messages"][0]["tool_calls"][0]["function"]["arguments"]) == {
            "text": W, "tier": "private", "reason": None}


def test_guard_withholds_bridged_calls_it_cant_read():
    """A bridged call whose arguments don't parse is still the model's own words."""
    broken = [
        '{"calls": [{"name": "mcp__hub__memory_save", "arguments": {"text": "Walked to the physio"}}',   # cut off
        '{"name": "whos-listening_remember", "arguments": {"text": "Walked to the physio"',
        'Walked to the physio',
    ]
    for raw in broken:
        for shape in ("function", "responses", "anthropic"):
            if shape == "function":
                msg = {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "tool_call", "arguments": raw}}]}
            elif shape == "responses":
                msg = {"type": "function_call", "call_id": "c1", "name": "tool_call", "arguments": raw}
            else:
                msg = {"role": "assistant", "content": [{"type": "tool_use", "id": "c1", "name": "tool_call",
                                                         "input": {"calls": raw}}]}
            req = {"model": "m", "messages": [msg]}
            for tools, prefix in ((HELLO.redactions(), ""), (None, hub.mcp_server_prefix("hub")), (None, "")):
                out = guard.guard_request(req, "shared", tools, prefix)
                assert "physio" not in json.dumps(out), (raw, shape, prefix)
    # Readable bridged calls to other tools keep their arguments.
    ok = json.dumps({"calls": [{"name": "web_search", "arguments": {"query": "weather"}}]})
    req = {"model": "m", "messages": [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "tool_call", "arguments": ok}}]}]}
    assert guard.guard_request(req, "shared", HELLO.redactions()) is None            # nothing to change


def test_guard_hub_arguments_that_cant_be_read():
    req = {"model": "m", "messages": [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "mcp__hub__memory_save", "arguments": "{broken"}}]}]}
    out = guard.guard_request(req, "shared", HELLO.redactions())
    assert json.loads(out["messages"][0]["tool_calls"][0]["function"]["arguments"]) == {
        "text": guard.WITHHELD, "reason": guard.WITHHELD}


# -- the fake hub passes the hub's cases ------------------------------------------------------

HUB_CASES = load("hub.json")


@pytest.mark.parametrize("case", HUB_CASES["cases"], ids=lambda c: c["name"])
def test_fake_hub_passes_hub_cases(case):
    given = HUB_CASES["given"]
    fh = fake_hub.FakeHub(HUB_CASES["token"], given["memories"], trusted_platforms=given["trusted"]["platforms"],
                          trusted_routes=given["trusted"]["routes"])
    private_texts = [m["text"] for m in given["memories"] if m["tier"] == "private"]
    tokens = {}
    for step in case["steps"]:
        must = step["must"]
        if step["call"] == "tool":
            args = {k: (tokens.get(v[1:-1], v) if isinstance(v, str) and v.startswith("{") else v)
                    for k, v in step["args"].items()}
            result, handled = fh.tool(step["tool"], args)
            escaped = [t.replace("<", "‹").replace(">", "›") for t in private_texts]
            if must.get("private_text") is False:
                assert not any(t in result for t in private_texts + escaped)
            if must.get("private_text_after_strip") is False:
                assert not any(t in strip_private(result) for t in private_texts + escaped)
                assert "wl-private" in result         # it had some to tag
            if "handled_as" in must:
                assert handled == must["handled_as"]
            continue
        headers = step.get("headers", {"Authorization": f"Bearer {HUB_CASES['token']}"})
        status, answer = fh.handle(step["call"], headers, step["request"])
        assert status == must.get("status", 200)
        if "error" in must:
            assert answer["error"] == must["error"]
        if must.get("no_private"):
            assert all(m["tier"] != "private" for m in answer["memories"])
        if must.get("held_back_set"):
            assert answer["held_back"]
        if "tier" in must:
            assert answer["tier"] == must["tier"]
        if "turn_token" in answer:
            tokens["turn_token"] = answer["turn_token"]
            if "keep_token_as" in step:
                tokens[step["keep_token_as"]] = answer["turn_token"]


def test_fake_hub_answers_the_message_examples():
    """Each example request in messages.json gets an answer with the response's required fields."""
    fields = load("fields.json")["calls"]
    fh = fake_hub.FakeHub(TOKEN, [], trusted_routes=[ROUTE])
    for case in load("messages.json"):
        status, answer = fh.handle(case["call"], {"Authorization": f"Bearer {TOKEN}"}, case["request"])
        assert status == 200, case["call"]
        assert set(fields[case["call"]]["response"]["required"]) <= set(answer)


# -- names, addresses and the token ---------------------------------------------------------------

def test_url_rules():
    assert hub.url_problem("https://hub.example.com/whos-listening/v1") == ""
    for ok in ("http://127.0.0.1:8080/v1", "http://localhost/v1", "http://[::1]:9/v1"):
        assert hub.url_problem(ok) == "", ok
    for bad in ("http://hub.example.com/v1", "ftp://x/y", "https://user:pw@hub.example.com/v1", "", "hub"):
        assert hub.url_problem(bad), bad


def test_finds_the_mcp_server_by_host():
    servers = {"notes": {"url": "https://notes.example.com/mcp"}, "home-hub": {"url": "https://hub.example.com/mcp"},
               "local": {"command": "python"}}
    assert hub.find_mcp_server("https://hub.example.com/whos-listening/v1", servers) == ("home-hub", "")
    assert hub.find_mcp_server("https://other.example.com/v1", servers)[0] == ""
    two = {**servers, "again": {"url": "https://hub.example.com/mcp2"}}
    assert hub.find_mcp_server("https://hub.example.com/v1", two)[0] == ""


def test_bearer_of_an_mcp_server_entry():
    assert hub.bearer_of({"headers": {"Authorization": "Bearer abc123"}}) == "abc123"
    assert hub.bearer_of({"headers": {"authorization": "bearer abc123"}}) == "abc123"
    assert hub.bearer_of({"headers": {"Authorization": "Bearer ${HUB_TOKEN}"}}) == ""   # nothing to look it up
    look_up = {"HUB_TOKEN": "abc123"}.get
    assert hub.bearer_of({"headers": {"Authorization": "Bearer ${HUB_TOKEN}"}}, look_up) == "abc123"
    assert hub.bearer_of({"headers": {"Authorization": "Bearer ${env:HUB_TOKEN}"}}, look_up) == "abc123"
    assert hub.bearer_of({"headers": {"Authorization": "Bearer ${OTHER}"}}, look_up) == ""   # not set
    assert hub.bearer_of({"headers": {"Authorization": "Basic abc"}}) == ""
    assert hub.bearer_of({}) == "" and hub.bearer_of(None) == ""


def test_base_url_is_cleaned():
    assert hub.clean_base_url("https://u:p@api.example.com:8443/v1?key=x#frag") == "https://api.example.com:8443/v1"
    assert hub.route_obj(Route("custom", "m", "https://api.example.com/v1?k=1")) == {
        "provider": "custom", "model": "m", "base_url": "https://api.example.com/v1"}
    assert hub.route_obj(None) is None


def test_hello_routes_from_config():
    config = {"model": {"provider": "venice", "default": "grok-4-5"},
              "providers": {"venice": {"base_url": "https://api.venice.ai/api/v1", "api_key": "not-sent"}},
              "fallback_providers": [{"provider": "nous", "model": "openai/gpt-6.1-terra",
                                      "base_url": "https://inference-api.nousresearch.com/v1"}],
              "auxiliary": {"compression": {"provider": "auto", "model": "", "base_url": "", "api_key": "x"},
                            "title_generation": {"provider": "auto", "model": ""}, "transient_retries": 2}}
    routes = hub.hello_routes(config)
    assert routes == [
        {"role": "main", "provider": "custom", "model": "grok-4-5", "base_url": "https://api.venice.ai/api/v1"},
        {"role": "fallback", "provider": "nous", "model": "openai/gpt-6.1-terra",
         "base_url": "https://inference-api.nousresearch.com/v1"},
        {"role": "compression", "provider": "auto", "model": "", "base_url": ""},
        {"role": "auxiliary", "name": "title_generation", "provider": "auto", "model": "", "base_url": ""}]
    assert "not-sent" not in json.dumps(routes)


def test_named_custom_endpoints_are_provider_custom_as_hermes_reports_them():
    """hello and the config default name a custom endpoint the way llm_request does: "custom"."""
    venice = "https://api.venice.ai/api/v1"
    for config in (
        {"model": {"provider": "api-venice-ai", "default": "grok-4-5", "base_url": venice},     # base_url on model
         "providers": {"api-venice-ai": {"base_url": venice}}},
        {"model": {"provider": "Venice Private", "default": "grok-4-5"},                       # legacy list form
         "custom_providers": [{"name": "Venice Private", "base_url": venice}]},
        {"model": {"provider": "custom:api-venice-ai", "default": "grok-4-5", "base_url": venice}},
    ):
        assert hub.hello_routes(config)[0] == {"role": "main", "provider": "custom", "model": "grok-4-5",
                                               "base_url": venice}
        assert default_route_from_config(config) == Route("custom", "grok-4-5", venice)
    fallback = {"model": {"provider": "nous", "default": "x"}, "providers": {"my-venice": {"base_url": venice}},
                "fallback_providers": [{"provider": "my-venice", "model": "grok-4-5", "base_url": venice}],
                "auxiliary": {"compression": {"provider": "my-venice", "model": "grok-4-5"}}}
    routes = hub.hello_routes(fallback)
    assert routes[1]["provider"] == "custom" and routes[2] == {"role": "compression", "provider": "custom",
                                                                "model": "grok-4-5", "base_url": venice}
    # A built-in provider keeps its name.
    assert hub.hello_routes({"model": {"provider": "nous", "default": "x"}})[0]["provider"] == "nous"


def test_hub_tools_and_outside_content():
    h = hub.parse_hello({"tools": [{"name": "memory_search", "outside_content": False, "redact_args": ["query"]},
                                   {"name": "web_fetch", "outside_content": True, "redact_args": []}],
                         "server_outside_content": False}, "hub")
    assert h.counts_as_outside("mcp__hub__memory_search") is False
    assert h.counts_as_outside("mcp__hub__web_fetch") is True
    assert h.counts_as_outside("mcp__hub__log_walk") is False          # server_outside_content
    assert h.counts_as_outside("web_extract") is None                   # not the hub's: the usual rule
    assert hub.parse_hello({"tools": []}, "hub").counts_as_outside("mcp__hub__x") is True   # default


def test_prompt_note_without_a_save_role_still_says_when_to_save(monkeypatch):
    from whos_listening import provider as prov

    class Client:
        hello = hub.parse_hello({"hub": {"name": "Example hub"},
                                 "tools": [{"name": "memory_save", "outside_content": False, "redact_args": ["text"]}]},
                                "hub")
    monkeypatch.setattr(prov, "_hub_hello", lambda *a, **k: None)
    monkeypatch.setattr(prov, "_hub_ready", lambda client: True)
    note = prov._hub_prompt_note(None, Client())
    assert "save it with the hub's memory tool for saving without being asked" in note
    assert "mcp__hub__memory_save" in note       # still listed among the hub's tools


def test_held_back_reason_is_one_short_line():
    block = hub.build_block([{"ref": "S1", "id": "m_9", "tier": "shared", "text": "Likes spicy food"}],
                            "this chat\nis on <Telegram>.", keep_private=True)
    assert "(Private memories are not used in this chat: this chat is on ‹Telegram›.)" in block


def test_a_route_the_hub_knows_lets_the_plugin_recheck_an_unknown_turn():
    memories = [{"ref": "P1", "id": "m_1", "tier": "private", "text": "Allergic to kiwi"},
                {"ref": "S1", "id": "m_2", "tier": "shared", "text": "Likes spicy food"}]
    body = {"session": SESSION, "turn": {"number": "1", "id": ""}, "route": None, "route_source": "unknown",
            "query": "Dinner?", "max_chars": 4000}
    seen = []

    def recheck(route):
        seen.append(route)
        return route.model == "grok-4-5"

    def block(answer, **kw):
        server = Scripted({"status": 200, "body": {"protocol": 1, "ok": True, "memories": memories,
                                                   "held_back": "", **answer}})
        try:
            client = hub.HubClient(server.url, "hub", timeouts={k: 0.3 for k in hub.TIMEOUTS})
            return client.context({**body, **kw}, TOKEN, keep_private=False, recheck=recheck)
        finally:
            server.close()

    # The hub names the route it knows: the plugin decides again with it.
    kept = block({"route": ROUTE})
    assert "Allergic to kiwi" in kept and "Likes spicy food" in kept
    assert seen == [Route(provider="custom", model="grok-4-5", base_url="https://api.venice.ai/api/v1")]
    # The plugin's own layers still win.
    assert "Allergic to kiwi" not in block({"route": {**ROUTE, "model": "other-model"}})
    # No route, a malformed one, or a turn whose route the plugin already knew: no recheck.
    for answer, extra in [({}, {}), ({"route": "custom/grok-4-5"}, {}), ({"route": {"provider": "", "model": "m"}}, {}),
                          ({"route": {**ROUTE, "model": 5}}, {}),
                          ({"route": ROUTE}, {"route": ROUTE, "route_source": "last_request"})]:
        seen.clear()
        text = block(answer, **extra)
        assert "Allergic to kiwi" not in text and "Likes spicy food" in text
        assert seen == []
