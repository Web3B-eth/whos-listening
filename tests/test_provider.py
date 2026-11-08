"""The Hermes glue. Needs Hermes importable (run with the Hermes venv); skipped otherwise."""

import json
import sys

import pytest

from wl_helpers import load_plugin


@pytest.fixture(autouse=True)
def scratch_hermes_home(tmp_path, monkeypatch):
    """Never let Hermes code fall back to the real ~/.hermes."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


@pytest.fixture
def plugin():
    """The provider module, reached through the plugin package as Hermes loads it."""
    return sys.modules[load_plugin().__name__ + ".whos_listening.provider"]


@pytest.fixture
def entry():
    return load_plugin()


@pytest.fixture
def provider(plugin, tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "_config_default_route", lambda: None)
    p = plugin.WhosListeningProvider()
    p.initialize("sess-1", hermes_home=str(tmp_path), platform="cli")
    yield p
    p.shutdown()


def see(plugin, base_url, model="m", session="sess-1"):
    """A turn starts, then the guard checks its request (which records the real route)."""
    plugin._on_pre_llm_call(session_id=session, model=model)
    plugin._on_llm_request(request={"messages": []}, session_id=session, provider="custom",
                           model=model, base_url=base_url)


@pytest.fixture(autouse=True)
def fresh_routes(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "ROUTES", plugin.RouteBook())


def tool(p, name, args):
    return json.loads(p.handle_tool_call(name, args))


def test_register_wires_provider_and_hooks(entry):
    calls = []

    class Ctx:
        def register_memory_provider(self, prov):
            calls.append(("provider", prov.name))

        def register_hook(self, name, fn):
            calls.append(("hook", name))

        def register_middleware(self, kind, fn):
            calls.append(("middleware", kind))

        def register_command(self, name, fn, **kw):
            calls.append(("command", name))

    entry.register(Ctx())
    assert calls == [("provider", "whos-listening"), ("hook", "pre_llm_call"), ("hook", "post_tool_call"),
                     ("middleware", "llm_request"), ("command", "memories")]


def test_store_lives_in_profile_home(provider, tmp_path):
    assert (tmp_path / "whos-listening" / "memories.sqlite").exists()


def test_prefetch_follows_route(plugin, provider):
    see(plugin, "http://127.0.0.1:9/v1")
    tool(provider, "wl_remember", {"text": "Is saving up for a canoe"})
    tool(provider, "wl_remember", {"text": "Likes jazz", "tier": "shared"})
    block = provider.prefetch("weekend plans", session_id="sess-1")
    assert "canoe" in block and "jazz" in block and "<wl-private" in block

    see(plugin, "https://openrouter.ai/api/v1")
    block = provider.prefetch("weekend plans", session_id="sess-1")
    assert "canoe" not in block and "jazz" in block

    # model switched since the last request: route unknown, so Shared only
    plugin._on_pre_llm_call(session_id="sess-1", model="another")
    see_block = provider.prefetch("weekend plans", session_id="sess-1")
    assert "canoe" not in see_block


def test_first_turn_uses_config_default(plugin, tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "_config_default_route",
                        lambda: plugin.Route("custom", "cfg-model", "https://api.venice.ai/api/v1"))
    p = plugin.WhosListeningProvider()
    p.initialize("sess-new", hermes_home=str(tmp_path), platform="cli")
    p._store.add("Collects stamps", "private", plugin.Provenance())
    plugin._on_pre_llm_call(session_id="sess-new", model="cfg-model")
    # until the guard has run once in this process, even the config default isn't trusted
    assert "stamps" not in p.prefetch("hobbies", session_id="sess-new")
    plugin._on_llm_request(request={"messages": []}, session_id="other", provider="custom",
                           model="x", base_url="https://api.example.com/v1")
    assert "stamps" in p.prefetch("hobbies", session_id="sess-new")
    plugin._on_pre_llm_call(session_id="sess-new", model="other-model")
    assert "stamps" not in p.prefetch("hobbies", session_id="sess-new")
    p.shutdown()


def test_broken_settings_mean_shared(plugin, provider, tmp_path):
    see(plugin, "http://127.0.0.1:9/v1")
    tool(provider, "wl_remember", {"text": "Is saving up for a canoe"})
    (tmp_path / "whos-listening" / "settings.json").write_text("{not json")
    assert "canoe" not in provider.prefetch("plans", session_id="sess-1")


def test_provenance_records_route(plugin, provider):
    see(plugin, "http://127.0.0.1:9/v1", model="local-model")
    provider.on_turn_start(4, "hi", author_name="Sam")
    mid = tool(provider, "wl_remember", {"text": "Bakes bread on Saturdays"})["saved"]["id"]
    p = provider._store.get(mid).provenance
    assert p.written_by == "assistant" and p.via == "wl_remember"
    assert p.route == "custom @ 127.0.0.1 / local-model" and p.route_tier == "private"
    assert p.session_id == "sess-1" and p.turn == "4" and p.author == "Sam"


def test_subagents_cannot_write(plugin, tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "_config_default_route", lambda: None)
    p = plugin.WhosListeningProvider()
    p.initialize("sess-sub", hermes_home=str(tmp_path), platform="cli", agent_context="subagent")
    assert tool(p, "wl_remember", {"text": "Something"})["ok"] is False
    assert tool(p, "wl_recall", {"query": ""})["ok"] is True
    p.shutdown()


def test_works_through_memory_manager(plugin, tmp_path, monkeypatch):
    from agent.memory_manager import MemoryManager

    monkeypatch.setattr(plugin, "_config_default_route", lambda: None)
    mgr = MemoryManager()
    mgr.add_provider(plugin.WhosListeningProvider())
    mgr.initialize_all(session_id="sess-mm", platform="cli", hermes_home=str(tmp_path))
    out = json.loads(mgr.handle_tool_call("wl_remember", {"text": "Waters plants on Mondays", "tier": "shared"}))
    assert out["ok"] is True
    mgr.shutdown_all()


# -- the guard ------------------------------------------------------------------

def _request(*texts):
    return {"model": "m", "stream": True,
            "messages": [{"role": "system", "content": texts[0]},
                         *({"role": "user", "content": t} for t in texts[1:])]}


def mw(plugin, request, base_url, session="sess-1", model="m"):
    return plugin._on_llm_request(request=request, original_request=request, session_id=session,
                                  provider="custom", model=model, base_url=base_url, platform="cli")


def test_guard_strips_private_on_shared_route(plugin, provider, monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "_active_home", lambda: tmp_path)
    see(plugin, "http://127.0.0.1:9/v1")
    tool(provider, "wl_remember", {"text": "Is saving up for a canoe"})
    tool(provider, "wl_remember", {"text": "Likes jazz", "tier": "shared"})
    block = provider.prefetch("weekend plans", session_id="sess-1")
    req = _request("sys", block, "What should I do this weekend?")

    assert mw(plugin, req, "http://127.0.0.1:9/v1") is None          # Private route: unchanged
    out = mw(plugin, req, "https://openrouter.ai/api/v1")            # Shared route: cleaned
    sent = json.dumps(out["request"])
    assert "canoe" not in sent and "jazz" in sent and "withheld" in sent
    assert out["request"]["stream"] is True and out["source"] == "whos-listening"
    # the guard also tells prefetch what route the session is really on
    assert plugin.ROUTES.last_seen("sess-1").host == "openrouter.ai"


def test_guard_cleans_replayed_turns_and_tool_results(plugin, provider, monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "_active_home", lambda: tmp_path)
    see(plugin, "http://127.0.0.1:9/v1")
    tool(provider, "wl_remember", {"text": "Is saving up for a canoe"})
    recall_result = provider.handle_tool_call("wl_recall", {"query": "canoe"})
    assert "canoe" in recall_result
    req = {"model": "m", "messages": [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": provider.prefetch("canoe", session_id="sess-1") + "\nhi"},
        {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "wl_recall", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": recall_result},
        {"role": "user", "content": [{"type": "text", "text": "new question"}]},
    ]}
    out = mw(plugin, req, "https://api.example.com/v1")
    assert "canoe" not in json.dumps(out["request"])
    assert out["request"]["messages"][4]["content"][0]["text"] == "new question"


def test_guard_fails_closed_when_profile_unknown(plugin, provider, monkeypatch):
    monkeypatch.setattr(plugin, "_active_home", lambda: None)
    req = _request('sys - #1 <wl-private id="1">Is saving up for a canoe</wl-private>', "hi")
    out = mw(plugin, req, "http://127.0.0.1:9/v1")   # even a Private host: profile unknown means Shared
    assert "canoe" not in json.dumps(out["request"])


def test_guard_fails_closed_on_broken_settings(plugin, provider, monkeypatch, tmp_path):
    monkeypatch.setattr(plugin, "_active_home", lambda: tmp_path)
    (tmp_path / "whos-listening" / "settings.json").write_text("{not json")
    req = _request('sys - #1 <wl-private id="1">Is saving up for a canoe</wl-private>', "hi")
    assert "canoe" not in json.dumps(mw(plugin, req, "http://127.0.0.1:9/v1")["request"])


def test_guard_never_raises(plugin, monkeypatch):
    def boom():
        raise RuntimeError("no")
    monkeypatch.setattr(plugin, "_active_home", boom)
    req = _request('<wl-private id="1">Is saving up for a canoe</wl-private>')
    out = plugin._on_llm_request(request=req)          # missing context and a failing lookup
    assert "canoe" not in json.dumps(out["request"])
    assert plugin._on_llm_request() is None            # no request at all: nothing to send


def test_guard_through_hermes_middleware_chain(plugin, provider, monkeypatch, tmp_path):
    """Run the guard the way Hermes does, twice over (registrations pile up), next to a plugin
    that raises."""
    from hermes_cli import plugins as hp
    from hermes_cli.middleware import apply_llm_request_middleware

    monkeypatch.setattr(plugin, "_active_home", lambda: tmp_path)
    mgr = hp.get_plugin_manager()

    def broken(**_):
        raise RuntimeError("another plugin failing")

    saved = list(mgr._middleware.get("llm_request", []))
    mgr._middleware["llm_request"] = [plugin._on_llm_request, broken, plugin._on_llm_request]
    monkeypatch.setattr(hp, "_delivery_manager", lambda: mgr, raising=False)
    try:
        req = _request('sys - #1 <wl-private id="1">Is saving up for a canoe</wl-private> - #2 Likes jazz', "hi")
        res = apply_llm_request_middleware(req, session_id="sess-1", model="m", provider="custom",
                                           base_url="https://api.example.com/v1", platform="cli")
        sent = json.dumps(res.payload)
        assert "canoe" not in sent and "jazz" in sent
    finally:
        mgr._middleware["llm_request"] = saved


# -- outside content and /memories -------------------------------------------------

@pytest.fixture(autouse=True)
def fresh_outside(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "OUTSIDE", plugin.review.OutsideBook())


def test_outside_content_holds_saves_and_survives_restart(plugin, provider, tmp_path, monkeypatch):
    see(plugin, "http://127.0.0.1:9/v1")
    assert tool(provider, "wl_remember", {"text": "Likes jazz"})["saved"]
    plugin._on_post_tool_call(tool_name="wl_recall", session_id="sess-1")       # ours: not outside
    assert tool(provider, "wl_remember", {"text": "Collects stamps"})["saved"]
    plugin._on_post_tool_call(tool_name="read_file", session_id="sess-1")
    held = tool(provider, "wl_remember", {"text": "Prefers window seats"})
    assert held["pending"] and "read_file" in held["note"]
    # Hermes restarts: the in-process flag is gone, the stored one isn't
    provider.sync_turn("u", "a", session_id="sess-1")
    monkeypatch.setattr(plugin, "OUTSIDE", plugin.review.OutsideBook())
    p2 = plugin.WhosListeningProvider()
    p2.initialize("sess-1", hermes_home=str(tmp_path), platform="cli")
    assert tool(p2, "wl_remember", {"text": "Bakes bread"})["pending"]
    p2.shutdown()


def test_memories_command(plugin, provider, tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "_active_home", lambda: tmp_path)
    see(plugin, "http://127.0.0.1:9/v1")
    tool(provider, "wl_remember", {"text": "Has a peanut allergy"})
    assert "peanut" in plugin._memories_command("", platform="cli")
    assert "peanut" not in plugin._memories_command("", platform="telegram")
    monkeypatch.setattr(plugin, "_active_home", lambda: None)
    assert "can't tell" in plugin._memories_command("")


def test_memories_command_through_hermes_dispatch(plugin, tmp_path, monkeypatch):
    from hermes_cli.plugin_command_dispatch import invoke_plugin_command

    monkeypatch.setattr(plugin, "_active_home", lambda: tmp_path)
    out = invoke_plugin_command(plugin._memories_command, "add shared Likes jazz",
                                source="cli", session_id="s", session_key="k", platform="cli")
    assert out.startswith("Saved")


def test_outside_flag_follows_compression_and_branch(plugin, provider):
    plugin._on_post_tool_call(tool_name="web_extract", session_id="sess-1")
    provider.on_session_switch("sess-2", parent_session_id="sess-1", reset=False)
    see(plugin, "http://127.0.0.1:9/v1", session="sess-2")
    assert tool(provider, "wl_remember", {"text": "Prefers window seats"})["pending"]
    provider.on_session_switch("sess-3", parent_session_id="sess-2", reset=True)   # /new: a clean start
    see(plugin, "http://127.0.0.1:9/v1", session="sess-3")
    assert tool(provider, "wl_remember", {"text": "Bakes bread"})["saved"]


def test_outside_flag_is_stored_by_the_hook(plugin, provider, tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "_active_home", lambda: tmp_path)
    plugin._on_post_tool_call(tool_name="read_file", session_id="sess-9")
    assert provider._store.outside_tool("sess-9") == "read_file"   # no wl_* call or turn end needed


def test_tool_call_session_id_counts(plugin, provider):
    plugin._on_post_tool_call(tool_name="read_file", session_id="other-id")
    see(plugin, "http://127.0.0.1:9/v1")
    out = json.loads(provider.handle_tool_call("wl_remember", {"text": "Likes jazz"}, session_id="other-id"))
    assert out["pending"]
