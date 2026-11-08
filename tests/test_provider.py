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
    plugin._on_pre_llm_call(session_id=session, model=model)
    plugin._on_pre_api_request(session_id=session, provider="custom", model=model, base_url=base_url)


def tool(p, name, args):
    return json.loads(p.handle_tool_call(name, args))


def test_register_wires_provider_and_hooks(entry):
    calls = []

    class Ctx:
        def register_memory_provider(self, prov):
            calls.append(("provider", prov.name))

        def register_hook(self, name, fn):
            calls.append(("hook", name))

    entry.register(Ctx())
    assert calls == [("provider", "whos-listening"), ("hook", "pre_llm_call"), ("hook", "pre_api_request")]


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
