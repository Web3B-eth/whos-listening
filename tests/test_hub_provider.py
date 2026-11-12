"""Hub mode through the Hermes glue, against the fake hub (e2e/fake_hub.py) on 127.0.0.1. Needs
Hermes importable (run with the Hermes venv); skipped otherwise. All data is made up."""

import json
import sys
import threading
import time

import pytest

from conftest import FAKE_VENICE
from test_hub import fake_hub
from wl_helpers import load_plugin

TOKEN = "hub-token-for-tests-only"
PRIVATE_URL, SHARED_URL = "http://127.0.0.1:9/v1", "https://llm.example.com/v1"
MEMORIES = [{"ref": "P2", "id": "m_2", "tier": "private", "text": "Sister Dana is vegetarian"},
            {"ref": "S1", "id": "m_9", "tier": "shared", "text": "Likes spicy food"}]


@pytest.fixture(autouse=True)
def scratch_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("WHOS_LISTENING_HUB_TOKEN", TOKEN)


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    mod = sys.modules[load_plugin().__name__ + ".whos_listening.provider"]
    mod.venice._reset_for_tests(dict(FAKE_VENICE))
    monkeypatch.setattr(mod, "ROUTES", mod.RouteBook())
    monkeypatch.setattr(mod, "OUTSIDE", mod.review.OutsideBook())
    monkeypatch.setattr(mod, "HUBS", {})
    monkeypatch.setattr(mod, "_HUB_TOKENS", {})
    monkeypatch.setattr(mod, "_active_home", lambda: tmp_path)
    monkeypatch.setattr(mod, "_config_default_route", lambda: None)
    return mod


@pytest.fixture
def the_hub():
    fh = fake_hub.FakeHub(TOKEN, MEMORIES, trusted_routes=[{"provider": "custom", "model": "m", "base_url": PRIVATE_URL}])
    server = fake_hub.serve(fh, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    fh.url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    yield fh
    server.shutdown()
    server.server_close()


def hub_settings(tmp_path, url, **extra):
    d = tmp_path / "whos-listening"
    d.mkdir(exist_ok=True)
    (d / "settings.json").write_text(json.dumps({"hub": {"url": url, "mcp_server": "hub"}, **extra}))


@pytest.fixture
def provider(plugin, the_hub, tmp_path):
    hub_settings(tmp_path, the_hub.url)
    p = plugin.WhosListeningProvider()
    p.initialize("sess-1", hermes_home=str(tmp_path), platform="cli")
    yield p
    p.shutdown()


def calls(fh, name):
    return [e for e in _log(fh) if e.get("call") == name]


def _log(fh):
    return fh.log


@pytest.fixture(autouse=True)
def keep_log(monkeypatch):
    def log(self, entry):
        self.__dict__.setdefault("log", []).append(entry)
    monkeypatch.setattr(fake_hub.FakeHub, "_log", log)


def guard(plugin, request, base_url, session="sess-1", model="m", platform=""):
    return plugin._on_llm_request(request=request, session_id=session, provider="custom", model=model,
                                  base_url=base_url, platform=platform, turn_id="turn_1")


def wait_for(fn, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.02)
    return False


def turn(plugin, provider, n, message="hi", model="m"):
    provider.on_turn_start(n, message)
    plugin._on_pre_llm_call(session_id="sess-1", model=model)


def test_hub_mode_offers_no_tools_of_its_own(plugin, provider, the_hub):
    assert provider.get_tool_schemas() == []
    note = provider.system_prompt_block()
    assert "Fake hub" in note and "mcp__hub__memory_search" in note and "whos-listening_remember" not in note
    assert "hub mode" in json.loads(provider.handle_tool_call("whos-listening_recall", {"query": "x"}))["error"]
    hello = calls(the_hub, "hello")[0]["body"]
    assert hello["plugin"]["name"] == "whos-listening" and hello["gateway"]["setting"] is None
    assert {r["role"] for r in hello["routes"]} >= {"compression"}


def test_recall_gets_private_only_on_a_trusted_route(plugin, provider, the_hub):
    turn(plugin, provider, 1)
    block = provider.prefetch("dinner", session_id="sess-1")
    assert "Likes spicy food" in block and "Dana" not in block       # route not known yet
    assert "(Private memories are not used in this chat:" in block
    guard(plugin, {"messages": []}, PRIVATE_URL)                      # the guard records the real route
    turn(plugin, provider, 2)
    block = provider.prefetch("dinner", session_id="sess-1")
    assert '- P2 <wl-private id="m_2">Sister Dana is vegetarian</wl-private>' in block
    ctx = calls(the_hub, "context")[-1]["body"]
    assert ctx["route_source"] == "last_request" and ctx["session"]["platform"] == "cli"


def test_local_shared_settings_drop_the_hubs_private_memories(plugin, provider, the_hub, tmp_path):
    hub_settings(tmp_path, the_hub.url, shared_hosts=["127.0.0.1"])
    guard(plugin, {"messages": []}, PRIVATE_URL)
    turn(plugin, provider, 1)
    block = provider.prefetch("dinner", session_id="sess-1")
    assert "Likes spicy food" in block and "Dana" not in block
    n = len(calls(the_hub, "verdict"))
    req = {"messages": [{"role": "user", "content": block + '<wl-private id="m_2">Dana</wl-private>'}]}
    assert "Dana" not in json.dumps(guard(plugin, req, PRIVATE_URL)["request"])
    assert len(calls(the_hub, "verdict")) == n                         # Shared locally: no need to ask


def test_guard_asks_the_hub_and_keeps_the_answer(plugin, provider, the_hub):
    tagged = {"messages": [{"role": "user", "content": '- P2 <wl-private id="m_2">Sister Dana is vegetarian</wl-private>'}]}
    assert guard(plugin, tagged, PRIVATE_URL) is None                  # Private: sent as it is
    assert guard(plugin, tagged, PRIVATE_URL) is None
    assert len(calls(the_hub, "verdict")) == 1                         # kept
    out = guard(plugin, tagged, SHARED_URL)
    assert "Dana" not in json.dumps(out["request"]) and "[private memory withheld]" in json.dumps(out["request"])
    assert calls(the_hub, "verdict")[-1]["body"]["session"]["platform"] == "cli"   # the provider's platform


def test_guard_blanks_replayed_hub_tool_calls(plugin, provider, the_hub):
    req = {"messages": [{"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {
        "name": "mcp__hub__memory_save", "arguments": json.dumps({"text": "Allergic to penicillin", "wl_turn": "tt_9QmV2c"})}}]}]}
    out = guard(plugin, req, SHARED_URL)
    assert json.loads(out["request"]["messages"][0]["tool_calls"][0]["function"]["arguments"]) == {
        "text": "[private memory withheld]"}


def test_turn_token_goes_into_hub_tool_calls(plugin, provider, the_hub):
    guard(plugin, {"messages": []}, PRIVATE_URL)
    turn(plugin, provider, 1)
    out = plugin._on_tool_request(tool_name="mcp__hub__memory_search", args={"query": "sister", "wl_turn": "made_up_token"},
                                  session_id="sess-1")
    token = out["args"]["wl_turn"]
    assert token in the_hub.turn_tokens and out["args"]["query"] == "sister"
    result, handled = the_hub.tool("memory_search", out["args"])
    assert "Dana" in result and handled == {"route": "private", "outside_content": False}
    # Another MCP server's tool keeps its arguments (and, not being the hub's, counts as outside content).
    assert plugin._on_tool_request(tool_name="mcp__notes__memory_save", args={"text": "x"}, session_id="sess-1") is None
    turn(plugin, provider, 2)                                          # a new turn, a new token
    assert plugin._on_tool_request(tool_name="mcp__hub__memory_search", args={"query": "x"},
                                   session_id="sess-1")["args"]["wl_turn"] != token


def test_outside_content_reaches_the_hub_before_the_tool_runs(plugin, provider, the_hub):
    guard(plugin, {"messages": []}, PRIVATE_URL)
    turn(plugin, provider, 1)
    assert plugin._on_tool_request(tool_name="web_extract", args={"url": "https://example.com"},
                                   session_id="sess-1") is None
    [report] = calls(the_hub, "turn-report")                           # sent before tool_request returned
    assert report["body"]["event"] == "outside" and report["body"]["outside_tools"] == ["web_extract"]
    args = plugin._on_tool_request(tool_name="mcp__hub__memory_save", args={"text": "Likes long walks"},
                                   session_id="sess-1")["args"]
    _, handled = the_hub.tool("memory_save", args)
    assert handled == {"route": "private", "outside_content": True}
    plugin._on_post_tool_call(tool_name="mcp__hub__memory_search", session_id="sess-1")
    plugin._on_tool_request(tool_name="read_file", args={}, session_id="sess-1")
    assert len(calls(the_hub, "turn-report")) == 1                     # one outside report per chat
    provider.sync_turn("u", "a", session_id="sess-1")
    assert wait_for(lambda: len(calls(the_hub, "turn-report")) == 2)
    end = calls(the_hub, "turn-report")[-1]["body"]
    assert end["event"] == "turn_end" and end["outside_tools"] == ["web_extract", "read_file"]
    assert end["session"]["outside_tool"] == "web_extract"


def test_turn_end_reports_each_request_and_its_tier(plugin, provider, the_hub):
    turn(plugin, provider, 1)
    guard(plugin, {"messages": []}, PRIVATE_URL)
    guard(plugin, {"messages": []}, SHARED_URL)
    provider.sync_turn("u", "a", session_id="sess-1")
    assert wait_for(lambda: calls(the_hub, "turn-report"))
    body = calls(the_hub, "turn-report")[-1]["body"]
    assert [(r["tier"], r["tier_source"]) for r in body["requests"]] == [("private", "hub"), ("shared", "hub")]
    assert body["turn"] == {"number": "1", "id": "turn_1"}


def test_an_unreachable_hub_means_no_memories_and_shared(plugin, tmp_path):
    hub_settings(tmp_path, "http://127.0.0.1:9/v1")                    # nothing listens there
    p = plugin.WhosListeningProvider()
    p.initialize("sess-1", hermes_home=str(tmp_path), platform="cli")
    try:
        guard(plugin, {"messages": []}, PRIVATE_URL)
        turn(plugin, p, 1)
        assert p.prefetch("dinner", session_id="sess-1") == ""
        req = {"messages": [{"role": "user", "content": '<wl-private id="m_2">Sister Dana is vegetarian</wl-private>'}]}
        assert "Dana" not in json.dumps(guard(plugin, req, PRIVATE_URL)["request"])
        assert "can't be reached" in p.system_prompt_block()
        assert "not ready" in plugin._memories_command("settings")
    finally:
        p.shutdown()


def test_a_bad_hub_address_is_not_ready(plugin, tmp_path):
    hub_settings(tmp_path, "http://hub.example.com/v1")
    out = plugin._memories_command("settings")
    assert "hub mode: not ready: hub.url must start with https://" in out


def test_memories_command_in_hub_mode(plugin, provider, the_hub):
    assert "hub mode" in plugin._memories_command("")
    guard(plugin, {"messages": []}, PRIVATE_URL)
    out = plugin._memories_command("settings")
    assert "hub mode: ready, connected to Fake hub" in out and "hub: " in out
    assert "verdict kept: cli · custom/m @ 127.0.0.1 = private" in out


def test_the_mcp_server_bearer_is_the_fallback_token(plugin, monkeypatch, the_hub):
    monkeypatch.delenv("WHOS_LISTENING_HUB_TOKEN")
    monkeypatch.setenv("FAKE_HUB_BEARER", TOKEN)
    monkeypatch.setattr(plugin, "_config", lambda: {"mcp_servers": {
        "hub": {"url": the_hub.url, "headers": {"Authorization": "Bearer ${FAKE_HUB_BEARER}"}}}})
    client = plugin.hub.HubClient(the_hub.url, "hub")
    assert plugin._hub_token(client) == TOKEN                         # the ${VAR} looked up through Hermes
    monkeypatch.delenv("FAKE_HUB_BEARER")
    plugin._HUB_TOKENS.clear()
    assert plugin._hub_token(client) == ""                            # not set: no token, never the placeholder
