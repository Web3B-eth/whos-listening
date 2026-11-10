"""Venice's model privacy list (venice.py) and how routes use it. All data is made up."""

import io
import json
import time

from whos_listening import settings as ws
from whos_listening import venice
from whos_listening.routes import Route, RouteSettings, classify
from whos_listening.store import PRIVATE, SHARED

LIST = json.dumps({"data": [
    {"id": "grok-4-5", "type": "text", "model_spec": {"privacy": "private"}},
    {"id": "claude-opus-5-5", "type": "text", "model_spec": {"privacy": "anonymized"}},
    {"id": "jev-latest", "type": "decision", "model_spec": {"privacy": "anonymized"}},
    {"id": "odd-model", "type": "text", "model_spec": {"privacy": "something else"}},
    {"type": "text", "model_spec": {"privacy": "private"}},
]}).encode()


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_parse_keeps_only_known_labels():
    assert venice.parse(LIST) == {"grok-4-5": "private", "claude-opus-5-5": "anonymized", "jev-latest": "anonymized"}


def test_privacy_matches_with_or_without_a_provider_prefix():
    assert venice.privacy("venice/GROK-4-5") == "private" and venice.privacy("claude-opus-5-5") == "anonymized"
    assert venice.privacy("not-a-model") is None


def test_refresh_keeps_the_list_on_disk_and_loads_it_again(tmp_path):
    venice._reset_for_tests(None)
    venice.configure(tmp_path)
    assert venice.privacy("grok-4-5") is None                      # no list yet
    seen = []
    assert venice.refresh(opener=lambda req, timeout: seen.append(req.full_url) or Resp(LIST)) is True
    assert seen == [venice.MODELS_URL] and venice.privacy("grok-4-5") == "private"
    venice._reset_for_tests(None)
    venice.configure(tmp_path)                                     # a restart reads the copy
    assert venice.privacy("claude-opus-5-5") == "anonymized"


def test_a_failed_refresh_keeps_what_was_there(tmp_path):
    venice._reset_for_tests({"grok-4-5": "private"})
    def boom(req, timeout):
        raise OSError("offline")
    assert venice.refresh(opener=boom) is False and venice.privacy("grok-4-5") == "private"
    assert venice.refresh(opener=lambda r, timeout: Resp(b"{not json")) is False
    assert venice.refresh(opener=lambda r, timeout: Resp(b'{"data": []}')) is False


def test_refresh_if_stale_runs_at_most_once_and_waits_after_a_failure(tmp_path):
    started = []
    venice._reset_for_tests({"grok-4-5": "private"})                  # fresh: nothing to do
    assert venice.refresh_if_stale(start=started.append) is False and started == []
    venice._reset_for_tests(None)
    venice.configure(tmp_path)
    assert venice.refresh_if_stale(start=started.append) is True and len(started) == 1
    assert venice.refresh_if_stale(start=started.append) is False   # one is already running
    venice._state["running"] = False
    venice._state["tried_at"] = time.time()                         # tried a moment ago: wait
    assert venice.refresh_if_stale(start=started.append) is False


def verdict(model, settings=None):
    return classify(Route("custom", model, "https://api.venice.ai/api/v1"), settings)


def test_routes_follow_venices_own_label():
    assert verdict("grok-4-5").tier == PRIVATE
    v = verdict("claude-opus-5-5")
    assert v.tier == SHARED and "anonymized" in v.reason
    assert verdict("brand-new-model").tier == SHARED                 # not on the list: Shared
    trusting = RouteSettings(venice_anonymized=PRIVATE)
    assert verdict("claude-opus-5-5", trusting).tier == PRIVATE
    assert verdict("brand-new-model", trusting).tier == PRIVATE
    assert verdict("grok-4-5", RouteSettings(shared_hosts=("api.venice.ai",))).tier == SHARED


def test_no_list_yet_means_shared_unless_trusted():
    venice._reset_for_tests(None)
    assert verdict("grok-4-5").tier == SHARED
    assert verdict("grok-4-5", RouteSettings(venice_anonymized=PRIVATE)).tier == PRIVATE


def test_the_setting(tmp_path):
    f = tmp_path / "settings.json"
    s = ws.load(ws.Sources(f, {}, False))
    assert ws.route_settings(s).venice_anonymized == SHARED
    assert "venice anonymized models: shared  (default)" in ws.describe(s)
    s = ws.load(ws.Sources(f, {"venice_anonymized": "private"}, False))
    assert ws.route_settings(s).venice_anonymized == PRIVATE
    f.write_text('{"venice_anonymized": "sure"}')
    assert ws.route_settings(ws.load(ws.Sources(f, {}, False))).venice_anonymized == SHARED   # a typo: Shared
