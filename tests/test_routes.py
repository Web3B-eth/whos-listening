import pytest

from whos_listening.routes import (
    Route, RouteBook, RouteSettings, classify, default_route_from_config, host_of,
)
from whos_listening.store import PRIVATE, SHARED


def tier(base_url, settings=None, provider="custom", model="m"):
    return classify(Route(provider, model, base_url), settings).tier


@pytest.mark.parametrize("url,host", [
    ("https://api.venice.ai/api/v1", "api.venice.ai"),
    ("http://127.0.0.1:8765/v1", "127.0.0.1"),
    ("http://[::1]:11434", "::1"),
    ("LOCALHOST:11434/v1", "localhost"),
    ("", ""),
])
def test_host_of(url, host):
    assert host_of(url) == host


@pytest.mark.parametrize("url", [
    "https://api.venice.ai/api/v1", "https://venice.ai", "http://localhost:11434/v1",
    "http://127.0.0.1:8080", "http://[::1]:8080", "http://192.168.1.20:1234",
    "http://10.0.0.5", "http://172.20.1.1", "http://box.local:8000", "http://ollama.localhost",
    "http://169.254.10.10", "http://[fd12:3456::1]/v1",
])
def test_default_private(url):
    assert tier(url) == PRIVATE


@pytest.mark.parametrize("url", [
    "https://openrouter.ai/api/v1", "https://inference-api.nousresearch.com/v1", "http://8.8.8.8",
    "http://172.32.0.1", "https://notvenice.ai", "https://venice.ai.example.com", "http://ollama",
    "https://local.example.com",
])
def test_default_shared(url):
    assert tier(url) == SHARED


def test_unknown_route_is_shared():
    assert classify(None).tier == SHARED
    assert tier("") == SHARED


def test_settings_precedence():
    s = RouteSettings.from_dict({
        "routes": {"nous/gpt-6-luna": "private", "*/glm-5.3-flash": "private", "nous": "shared",
                   "openrouter": "nonsense"},
        "private_hosts": ["models.example.org"],
        "shared_hosts": ["api.venice.ai"],
    })
    assert "openrouter" not in s.routes
    assert classify(Route("nous", "gpt-6-luna", "https://x.example"), s).tier == PRIVATE
    assert classify(Route("nous", "other", "https://x.example"), s).tier == SHARED
    assert classify(Route("custom", "glm-5.3-flash", "https://x.example"), s).tier == PRIVATE
    assert tier("https://eu.models.example.org/v1", s) == PRIVATE
    assert tier("https://api.venice.ai/api/v1", s) == SHARED  # the user's shared_hosts beats the default


def test_settings_round_trip(tmp_path):
    p = tmp_path / "settings.json"
    assert RouteSettings.load(p) == RouteSettings()
    s = RouteSettings.from_dict({"routes": {"Nous": "Private"}, "private_hosts": ["A.example"]})
    s.save(p)
    assert RouteSettings.load(p) == s
    p.write_text("{broken")
    with pytest.raises(ValueError):
        RouteSettings.load(p)


def test_default_route_from_config():
    cfg = {"model": {"provider": "my-venice", "default": "glm-5.3-flash"},
           "providers": {"my-venice": {"base_url": "https://api.venice.ai/api/v1", "key_env": "X"}}}
    r = default_route_from_config(cfg)
    assert r == Route("custom", "glm-5.3-flash", "https://api.venice.ai/api/v1")
    assert default_route_from_config({"model": {"provider": "nous", "default": "gpt-6-luna"}}) == \
        Route("nous", "gpt-6-luna", "")
    assert default_route_from_config({}) is None


def test_route_book_beliefs():
    cfg = Route("custom", "cfg-model", "http://127.0.0.1:1/v1")
    book = RouteBook(max_sessions=2)
    # first turn: believe the config default only when the turn's model matches it
    book.saw_turn_model("s1", "cfg-model")
    assert book.believed("s1", cfg)[0] == cfg
    book.saw_turn_model("s2", "other-model")
    assert book.believed("s2", cfg)[0] is None
    # once a request was seen, use it while the model matches
    real = Route("custom", "cfg-model", "https://openrouter.ai/api/v1")
    book.saw_request("s1", real)
    assert book.believed("s1", cfg)[0] == real
    book.saw_turn_model("s1", "switched")
    assert book.believed("s1", cfg)[0] is None
    # bounded
    book.saw_request("s3", real)
    book.saw_request("s4", real)
    assert book.last_seen("s1") is None
