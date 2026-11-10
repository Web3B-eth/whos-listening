"""Where settings come from (settings.py): Hermes' config (Desktop's plugin form) and settings.json.
All values are made up."""

import json

import pytest

from whos_listening import settings as ws
from whos_listening.store import PRIVATE, SHARED


def src(tmp_path, file=None, hermes=None, hosted=False, known=True):
    f = tmp_path / "settings.json"
    if file is not None:
        f.write_text(file if isinstance(file, str) else json.dumps(file))
    return ws.Sources(f, {} if hermes is None and known else hermes, hosted, hermes_known=known)


def test_nothing_set_means_the_defaults(tmp_path):
    s = ws.load(src(tmp_path))
    r = ws.route_settings(s)
    assert r.gateway == PRIVATE and r.review == "ask" and not r.gateway_hosted
    assert s.origin == {"gateway": ws.FROM_DEFAULT}
    assert not ws.sync_settings(s).enabled


def test_a_hosted_hermes_defaults_to_a_shared_gateway(tmp_path):
    s = ws.load(src(tmp_path, hosted=True))
    r = ws.route_settings(s)
    assert r.gateway == SHARED and r.gateway_hosted and s.origin["gateway"] == ws.FROM_HOSTED
    # ...unless you say it's Private, in Hermes or in the file.
    assert ws.route_settings(ws.load(src(tmp_path, hermes={"gateway": "private"}, hosted=True))).gateway == PRIVATE
    assert ws.route_settings(ws.load(src(tmp_path, file={"gateway": "private"}, hosted=True))).gateway == PRIVATE


def test_hermes_settings_win_over_the_file(tmp_path):
    file = {"gateway": "private", "review": "auto", "sync": {"repo": "me/notes", "name": "laptop", "path": "x/y.json"},
            "private_platforms": ["signal"]}
    hermes = {"gateway": "shared", "sync_name": "cloud", "sync_interval": 120, "shared_hosts": ["models.example.org"]}
    s = ws.load(src(tmp_path, file=file, hermes=hermes))
    r, y = ws.route_settings(s), ws.sync_settings(s)
    assert r.gateway == SHARED and not r.gateway_hosted                 # set, not the hosted default
    assert r.review == "auto" and r.private_platforms == ("signal",) and r.shared_hosts == ("models.example.org",)
    assert (y.repo, y.name, y.interval, y.path) == ("me/notes", "cloud", 120, "x/y.json")
    assert s.origin["gateway"] == ws.FROM_HERMES and s.origin["review"] == ws.FROM_FILE
    assert s.origin["sync.name"] == ws.FROM_HERMES and s.origin["sync.repo"] == ws.FROM_FILE


def test_default_or_empty_in_hermes_means_not_set_there(tmp_path):
    file = {"gateway": "shared", "review": "auto", "sync": {"repo": "me/notes"}, "private_hosts": ["a.example.org"]}
    hermes = {"gateway": "default", "review": "default", "sync_repo": "", "private_hosts": [], "routes": {}}
    r = ws.route_settings(ws.load(src(tmp_path, file=file, hermes=hermes)))
    assert r.gateway == SHARED and r.review == "auto" and r.private_hosts == ("a.example.org",)
    assert ws.sync_settings(ws.load(src(tmp_path, file=file, hermes=hermes))).repo == "me/notes"
    # "default" in the file is the built-in default too.
    assert ws.route_settings(ws.load(src(tmp_path, file={"gateway": "default"}, hosted=True))).gateway == SHARED


def test_sync_can_be_set_up_from_hermes_alone(tmp_path):
    s = ws.load(src(tmp_path, hermes={"sync_repo": "me/notes", "sync_name": "cloud"}))
    y = ws.sync_settings(s)
    assert y.enabled and y.repo == "me/notes" and y.name == "cloud" and y.path == "whos-listening/shared.json"
    assert ws.sync_settings(ws.load(src(tmp_path, hermes={"sync_repo": "not a repo"}))).error


def test_values_of_the_wrong_shape_fail_safe(tmp_path):
    for hermes in ({"private_platforms": "signal"}, {"routes": ["nous"]}, {"gateway": 3},
                   {"sync_interval": "often"}, {"private_hosts": [1, 2]}):
        with pytest.raises(ws.SettingsError):
            ws.load(src(tmp_path, hermes=hermes))
    with pytest.raises(ws.SettingsError):           # a list as text would be read letter by letter
        ws.load(src(tmp_path, file={"private_platforms": "signal"}))
    with pytest.raises(ws.SettingsError):
        ws.load(src(tmp_path, file="{broken"))
    with pytest.raises(ws.SettingsError):
        ws.load(src(tmp_path, file="[1, 2]"))
    # A "sync" that isn't an object turns only sync off.
    s = ws.load(src(tmp_path, file={"sync": "me/notes"}))
    assert ws.sync_settings(s).error and ws.route_settings(s).gateway == PRIVATE
    # Unknown keys in Hermes are ignored, not refused.
    assert ws.load(src(tmp_path, file={}, hermes={"something_else": [1]})).doc["gateway"] == PRIVATE


def test_unreadable_hermes_settings_fail_safe(tmp_path):
    with pytest.raises(ws.SettingsError):
        ws.load(ws.Sources(tmp_path / "settings.json", None, False))
    # No Hermes to ask at all (the command line, before any copy) is not an error.
    assert ws.load(ws.Sources(tmp_path / "settings.json", None, False, hermes_known=False)).doc["gateway"] == PRIVATE


def test_hermes_part_reads_only_this_plugins_settings():
    cfg = {"plugins": {"entries": {"whos-listening": {"settings": {"gateway": "shared"}},
                                   "other": {"settings": {"gateway": "private"}}}}}
    assert ws.hermes_part(cfg) == {"gateway": "shared"}
    for cfg in (None, {}, {"plugins": []}, {"plugins": {"entries": {"whos-listening": {"settings": "x"}}}}):
        assert ws.hermes_part(cfg) == {}


def test_snapshot_for_the_command_line(tmp_path):
    assert ws.read_snapshot(tmp_path) is None
    ws.write_snapshot(tmp_path, {"gateway": "shared", "not_ours": 1}, True)
    assert ws.read_snapshot(tmp_path) == ({"gateway": "shared"}, True)
    before = ws.snapshot_path(tmp_path).stat().st_mtime_ns
    ws.write_snapshot(tmp_path, {"gateway": "shared"}, True)            # unchanged: not rewritten
    assert ws.snapshot_path(tmp_path).stat().st_mtime_ns == before
    ws.snapshot_path(tmp_path).write_text("{broken")
    assert ws.read_snapshot(tmp_path) is None


def test_describe_says_where_each_setting_comes_from(tmp_path):
    s = ws.load(src(tmp_path, file={"review": "auto"}, hermes={"sync_repo": "me/notes"}, hosted=True))
    text = ws.describe(s, token_set=False)
    assert "gateway: shared  (default on a hosted Hermes)" in text
    assert "review: auto  (settings.json)" in text
    assert "sync: me/notes  (Hermes settings)" in text
    assert "sync token: missing" in text
    assert "sync: off" in ws.describe(ws.load(src(tmp_path, file={})))


def test_the_form_declares_every_hermes_setting():
    """plugin.yaml's config_schema and FIELDS must list the same keys (plus the token)."""
    from pathlib import Path
    import re
    text = (Path(__file__).resolve().parent.parent / "plugin.yaml").read_text()
    schema = text.split("config_schema:", 1)[1]
    keys = set(re.findall(r"^  ([a-z_]+):\s*$", schema, re.M))
    assert keys == set(ws.FIELDS) | {"sync_token"}
    assert "env: WHOS_LISTENING_SYNC_TOKEN" in schema
