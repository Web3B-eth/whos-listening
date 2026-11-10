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
                   {"sync_interval": "often"}, {"sync_repo": ["me/notes"]}):
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
    assert ws.read_snapshot(tmp_path) == ({"gateway": "shared"}, "Hermes gets its config from a remote service")
    before = ws.snapshot_path(tmp_path).stat().st_mtime_ns
    ws.write_snapshot(tmp_path, {"gateway": "shared"}, True)            # unchanged: not rewritten
    assert ws.snapshot_path(tmp_path).stat().st_mtime_ns == before
    ws.snapshot_path(tmp_path).write_text("{broken")
    with pytest.raises(ws.SettingsError):             # a damaged copy fails safe, it isn't "no copy"
        ws.read_snapshot(tmp_path)
    ws.write_snapshot(tmp_path, None, False)          # Hermes' config couldn't be read
    with pytest.raises(ws.SettingsError):
        ws.read_snapshot(tmp_path)


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


# -- review findings ------------------------------------------------------------------------

def test_lists_from_both_places_combine(tmp_path):
    """Desktop's form doesn't show settings.json's lists: setting one there must not drop a
    Shared entry from the file."""
    s = ws.load(src(tmp_path, file={"shared_platforms": ["desktop"], "shared_hosts": ["a.example.org"]},
                    hermes={"shared_platforms": ["tui"], "shared_hosts": ["a.example.org", "b.example.org"]}))
    r = ws.route_settings(s)
    assert r.shared_platforms == ("desktop", "tui") and r.shared_hosts == ("a.example.org", "b.example.org")
    assert s.origin["shared_platforms"] == ws.FROM_BOTH


def test_routes_combine_and_shared_wins(tmp_path):
    s = ws.load(src(tmp_path, file={"routes": {"nous": "shared", "venice/x": "private"}},
                    hermes={"routes": {"Nous": "private", "custom": "private"}}))
    assert ws.route_settings(s).routes == {"nous": "shared", "venice/x": "private", "custom": "private"}
    s = ws.load(src(tmp_path, file={"routes": {"custom": "private"}}, hermes={"routes": {"custom": "shared"}}))
    assert ws.route_settings(s).routes == {"custom": "shared"}


def test_odd_gateway_values_in_the_file_still_count_as_shared(tmp_path):
    for value in ("default", "Default", "  ", "privat", True, 1):
        assert ws.route_settings(ws.load(src(tmp_path, file={"gateway": value}))).gateway == SHARED, value
    for value in ("", None, False, 0):                      # empty: the default (Private here...)
        assert ws.route_settings(ws.load(src(tmp_path, file={"gateway": value}))).gateway == PRIVATE, value
        r = ws.route_settings(ws.load(src(tmp_path, file={"gateway": value}, hosted=True)))
        assert r.gateway == SHARED and r.gateway_hosted, value   # ...and Shared on a hosted Hermes


def test_a_sync_repository_from_hermes_takes_nothing_from_the_files_sync(tmp_path):
    file = {"sync": {"repo": "me/old", "name": "laptop", "branch": "only-old", "api_url": "http://127.0.0.1:9",
                     "path": "x/y.json", "interval": 300}}
    y = ws.sync_settings(ws.load(src(tmp_path, file=file, hermes={"sync_repo": "me/new"})))
    assert (y.repo, y.branch, y.path, y.name, y.interval) == ("me/new", "", "whos-listening/shared.json", "", 60)
    assert y.api_url == "https://api.github.com"
    y = ws.sync_settings(ws.load(src(tmp_path, file=file, hermes={"sync_name": "cloud"})))
    assert (y.repo, y.name, y.branch) == ("me/old", "cloud", "only-old")        # only the name changes


def test_a_zero_interval_from_desktop_means_the_default(tmp_path):
    s = ws.load(src(tmp_path, file={"sync": {"repo": "me/notes", "interval": 300}}, hermes={"sync_interval": 0}))
    assert ws.sync_settings(s).interval == 300 and s.origin["sync.interval"] == ws.FROM_FILE


def test_entries_settings_json_always_took_loosely_still_load(tmp_path):
    s = ws.load(src(tmp_path, file={"routes": {"a": None, "b": "shared"}, "private_hosts": ["x.example.org", 5]}))
    r = ws.route_settings(s)
    assert r.routes == {"b": "shared"} and r.private_hosts == ("x.example.org", "5")


def test_the_copy_is_rewritten_if_it_changed_on_disk(tmp_path):
    cache = {}
    ws.write_snapshot(tmp_path, {"gateway": "shared"}, False, cache)
    ws.snapshot_path(tmp_path).write_text("{broken")
    ws.write_snapshot(tmp_path, {"gateway": "shared"}, False, cache)
    assert ws.read_snapshot(tmp_path) == ({"gateway": "shared"}, "")
    ws.snapshot_path(tmp_path).unlink()
    ws.write_snapshot(tmp_path, {"gateway": "shared"}, False, cache)
    assert ws.read_snapshot(tmp_path) == ({"gateway": "shared"}, "")
    assert not list(tmp_path.glob("*.tmp")) and not list(tmp_path.glob(".*.tmp"))


def test_text_where_a_list_or_routes_belongs_fails_safe(tmp_path):
    for file in ({"routes": "default"}, {"routes": " "}, {"private_hosts": "default"},
                 {"private_platforms": "signal"}):
        with pytest.raises(ws.SettingsError):
            ws.load(src(tmp_path, file=file))
    s = ws.load(src(tmp_path, file={"routes": "", "private_hosts": ""}))     # empty: not set
    assert ws.route_settings(s).routes == {} and ws.route_settings(s).private_hosts == ()


def test_describe_shows_a_pasted_link_as_owner_and_name(tmp_path):
    s = ws.load(src(tmp_path, hermes={"sync_repo": "https://github.com/me/notes.git"}))
    assert "sync: me/notes  (Hermes settings)" in ws.describe(s) and ws.sync_settings(s).repo == "me/notes"


def test_hosted_reason():
    assert ws.hosted_reason({}) == ""
    assert ws.hosted_reason({"HERMES_CONFIG_BACKEND": "file"}) == ""
    assert "remote service" in ws.hosted_reason({"HERMES_CONFIG_BACKEND": "remote"})
    assert "Fly.io" in ws.hosted_reason({"FLY_APP_NAME": "made-up-app", "FLY_MACHINE_ID": "0123456789abcd"})
    assert ws.hosted_reason({"FLY_APP_NAME": "made-up-app"}) == ""        # both, as Hermes itself checks
    assert ws.hosted_reason({"FLY_APP_NAME": " ", "FLY_MACHINE_ID": "x"}) == ""


def test_describe_says_whether_this_machine_counts_as_hosted(tmp_path):
    text = ws.describe(ws.load(src(tmp_path, hosted="it runs on a Fly.io machine, as Hermes Cloud does")))
    assert "this machine: hosted, because it runs on a Fly.io machine" in text
    assert "gateway: shared  (default on a hosted Hermes)" in text
    assert "this machine: not recognized as hosted" in ws.describe(ws.load(src(tmp_path)))
    ws.snapshot_path(tmp_path).write_text('{"hosted": true, "settings": {}}')          # an older copy
    assert ws.read_snapshot(tmp_path) == ({}, "Hermes gets its config from a remote service")
