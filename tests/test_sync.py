"""Sync between gateways (sync.py). Two in-memory stores share a fake sync file. All data is made up."""

import base64
import io
import json
import urllib.error

import pytest

from whos_listening import commands, sync
from whos_listening.store import (
    ACTIVE, FORGOTTEN, PENDING, PRIVATE, SHARED, ForgottenError, Provenance, SQLiteStore, fingerprint,
)

USER = Provenance(written_by="user", via="test")


class FakeFile:
    """The sync file, the way GitHub keeps it: bytes plus a version that changes on every write."""

    def __init__(self):
        self.raw, self.version, self.writes, self.conflicts = None, 0, [], 0

    def read(self):
        return self.raw, (f"v{self.version}" if self.raw is not None else None)

    def write(self, raw, sha, message):
        if self.conflicts:
            self.conflicts -= 1
            self.version += 1                     # someone else wrote in between
            raise sync.Conflict("changed")
        if (self.raw is None and sha) or (self.raw is not None and sha != f"v{self.version}"):
            raise sync.Conflict("stale")
        self.raw, self.version = raw, self.version + 1
        self.writes.append(raw)

    def doc(self):
        return json.loads(self.raw.decode()) if self.raw else {}


@pytest.fixture
def file():
    return FakeFile()


@pytest.fixture
def a(tmp_path):
    s = SQLiteStore(tmp_path / "a.sqlite")
    yield s
    s.close()


@pytest.fixture
def b(tmp_path):
    s = SQLiteStore(tmp_path / "b.sqlite")
    yield s
    s.close()


def go(store, file, name):
    return sync.sync_once(store, file, name)


def texts(store, *, status=ACTIVE):
    return sorted(m.text for m in store.list(statuses=(status,), limit=100))


# -- settings ---------------------------------------------------------------------------

def test_settings():
    assert not sync.SyncSettings.from_dict(None).enabled
    s = sync.SyncSettings.from_dict({"repo": "me/notes", "interval": 5, "name": "laptop<script>"})
    assert s.enabled and s.path == sync.DEFAULT_PATH and s.interval == 30 and s.name == "laptopscript"
    assert sync.SyncSettings.from_dict({"repo": "not a repo"}).error
    assert sync.SyncSettings.from_dict({"repo": "me/notes", "path": "../x"}).error
    assert sync.SyncSettings.from_dict({"repo": "me/notes", "api_url": "http://example.com"}).error
    assert sync.SyncSettings.from_dict({"repo": "me/notes", "api_url": "http://127.0.0.1:9"}).enabled
    assert sync.SyncSettings.from_dict("yes").error


def test_load_settings(tmp_path):
    f = tmp_path / "settings.json"
    assert not sync.load_settings(f).enabled
    f.write_text('{"sync": {"repo": "me/notes"}}')
    assert sync.load_settings(f).repo == "me/notes"
    f.write_text("{broken")
    assert sync.load_settings(f).error


# -- the file -----------------------------------------------------------------------------

def test_parse_drops_bad_entries_and_never_trusts_the_file():
    uid, uid2 = "a" * 32, "b" * 32
    doc = {"format": sync.FORMAT, "version": 1, "memories": {
        uid: {"status": "active", "text": "Likes jazz", "fingerprint": "0" * 64, "updated_at": "2026-11-01T00:00:00+00:00"},
        uid2: {"status": "forgotten", "text": "leftover words", "fingerprint": "1" * 64, "updated_at": "2026-11-01T00:00:00+00:00"},
        "not-a-uid": {"status": "active", "text": "x", "updated_at": "2026-11-01T00:00:00+00:00"},
        "c" * 32: {"status": "pending", "text": "x", "updated_at": "2026-11-01T00:00:00+00:00"},
        "d" * 32: {"status": "active", "text": "", "updated_at": "2026-11-01T00:00:00+00:00"},
    }}
    recs = sync.parse(json.dumps(doc).encode())
    assert set(recs) == {uid, uid2}
    assert recs[uid].fingerprint == fingerprint("Likes jazz")     # computed, not read
    assert recs[uid2].text is None                                 # a forget carries no text
    assert b"leftover" not in sync.render(recs)
    with pytest.raises(sync.SyncError):
        sync.parse(json.dumps({**doc, "version": 99}).encode())
    with pytest.raises(sync.SyncError):
        sync.parse(b"{not json")
    with pytest.raises(sync.SyncError):
        sync.parse(b'{"format": "something else", "version": 1}')


# -- two gateways ---------------------------------------------------------------------------

def test_only_shared_memories_leave(a, b, file):
    a.add("Keeps a vegetarian diet on weekdays", PRIVATE, USER)
    a.add("Likes jazz", SHARED, USER)
    a.add("Likes opera", SHARED, USER, status=PENDING, pending_reason="waits")
    result = go(a, file, "laptop")
    assert result.sent == 1 and result.wrote
    raw = file.raw.decode()
    assert "Likes jazz" in raw
    assert "vegetarian" not in raw and "opera" not in raw            # Private and pending stay home
    assert all(e["origin"] == "laptop" for e in file.doc()["memories"].values())

    result = go(b, file, "cloud")
    assert result.arrived == 1 and texts(b) == ["Likes jazz"]
    m = b.list()[0]
    assert m.tier == SHARED and m.provenance.via == "sync" and m.provenance.route == "sync from laptop"
    assert [e.action for e in b.events(memory_id=m.id)] == ["synced_in"]
    assert commands.cmd_undo(b, True) == "Nothing to undo."          # an arrival isn't yours to undo


def test_a_second_sync_writes_nothing(a, b, file):
    a.add("Likes jazz", SHARED, USER)
    go(a, file, "laptop")
    go(b, file, "cloud")
    writes = len(file.writes)
    assert go(a, file, "laptop").summary() == "nothing new"
    assert go(b, file, "cloud").summary() == "nothing new"
    assert len(file.writes) == writes


def test_nothing_to_share_writes_no_file(a, file):
    a.add("Keeps a vegetarian diet on weekdays", PRIVATE, USER)
    go(a, file, "laptop")
    assert file.raw is None


def test_a_forget_travels_without_its_text(a, b, file):
    jazz = a.add("Likes jazz", SHARED, USER)
    go(a, file, "laptop")
    go(b, file, "cloud")
    b.forget(b.by_uid(jazz.uid).id)
    go(b, file, "cloud")
    entry = file.doc()["memories"][jazz.uid]
    assert entry["status"] == FORGOTTEN and "text" not in entry and b"Likes jazz" not in file.raw

    result = go(a, file, "laptop")
    assert result.forgotten == 1 and a.get(jazz.id).status == FORGOTTEN
    with pytest.raises(ForgottenError):
        a.add("likes JAZZ.", SHARED, USER)                           # can't come back from chat either


def test_a_restore_travels_too(a, b, file):
    jazz = a.add("Likes jazz", SHARED, USER)
    go(a, file, "laptop")
    go(b, file, "cloud")
    a.forget(jazz.id, at="2099-01-01T10:00:00+00:00")
    go(a, file, "laptop")
    go(b, file, "cloud")
    assert b.by_uid(jazz.uid).status == FORGOTTEN
    a.restore(jazz.id, at="2099-01-01T11:00:00+00:00")
    go(a, file, "laptop")
    result = go(b, file, "cloud")
    assert result.restored == 1 and b.by_uid(jazz.uid).status == ACTIVE


def test_last_change_wins_and_a_forget_wins_a_tie(a, b, file):
    jazz = a.add("Likes jazz", SHARED, USER)
    go(a, file, "laptop")
    go(b, file, "cloud")
    stamp = "2030-01-01T00:00:00+00:00"
    a.forget(jazz.id, at=stamp)
    go(a, file, "laptop")
    b_jazz = b.by_uid(jazz.uid)
    # B restores at the same second A forgot: still forgotten everywhere
    b.forget(b_jazz.id, at="2029-12-31T00:00:00+00:00")
    b.restore(b_jazz.id, at=stamp)
    go(b, file, "cloud")
    assert file.doc()["memories"][jazz.uid]["status"] == FORGOTTEN
    assert b.by_uid(jazz.uid).status == FORGOTTEN


def test_same_words_saved_on_both_stay_one_line(a, b, file):
    a.add("Likes jazz", SHARED, USER)
    b.add("likes jazz", SHARED, USER)
    go(a, file, "laptop")
    result = go(b, file, "cloud")
    assert result.duplicates == 1 and texts(b) == ["likes jazz"]
    go(a, file, "laptop")
    assert texts(a) == ["Likes jazz"]
    # Forgetting it on one gateway forgets the other's copy too (same words).
    b.forget(b.list()[0].id)
    go(b, file, "cloud")
    go(a, file, "laptop")
    assert texts(a) == []


def test_a_newer_forget_here_beats_an_older_save_elsewhere(a, b, file):
    old = b.add("Likes jazz", SHARED, USER)
    go(b, file, "cloud")
    # A forgot the same words (its own copy) after B saved them, before ever syncing.
    mine = a.add("Likes jazz", SHARED, USER)
    a.forget(mine.id, at="2099-01-01T00:00:00+00:00")
    go(a, file, "laptop")
    assert file.doc()["memories"][old.uid]["status"] == FORGOTTEN
    go(b, file, "cloud")
    assert b.get(old.id).status == FORGOTTEN


def test_words_forgotten_privately_here_stay_out_here(a, b, file):
    private = a.add("Likes jazz", PRIVATE, USER)
    a.forget(private.id)
    jazz = b.add("Likes jazz", SHARED, USER)
    go(b, file, "cloud")
    result = go(a, file, "laptop")
    assert result.arrived == 0 and result.kept_out == 1 and texts(a) == []
    assert a.tombstone(fingerprint("Likes jazz"))[1] == PRIVATE     # the Private forget stays
    assert file.doc()["memories"][jazz.uid]["status"] == ACTIVE     # and isn't sent anywhere
    go(b, file, "cloud")
    assert b.get(jazz.id).status == ACTIVE
    with pytest.raises(ForgottenError):
        a.add("Likes jazz", PRIVATE, USER)


def test_restoring_a_shared_memory_never_lifts_a_private_tombstone(b):
    private = b.add("Likes tea", PRIVATE, USER)
    b.forget(private.id)
    b.purge(private.id)
    shared = b.import_synced("f" * 32, "Likes tea", USER, created_at="2026-11-01T00:00:00+00:00",
                             updated_at="2026-11-01T00:00:00+00:00")
    b.forget(shared.id)
    b.restore(shared.id)
    assert b.tombstone(fingerprint("Likes tea"))[1] == PRIVATE


def test_a_forget_reaches_a_held_arrival(a, b, file):
    link = a.add("Meet at example.com on Fridays", SHARED, USER)     # the rule screen holds it on B
    go(a, file, "laptop")
    go(b, file, "cloud")
    assert b.by_uid(link.uid).status == PENDING
    a.forget(link.id, at="2099-01-01T00:00:00+00:00")
    go(a, file, "laptop")
    go(b, file, "cloud")
    assert b.by_uid(link.uid).status == FORGOTTEN
    assert "isn't waiting" in commands.run(b, f"approve {b.by_uid(link.uid).id}", show_private=True)
    go(b, file, "cloud")
    go(a, file, "laptop")
    assert a.get(link.id).status == FORGOTTEN


def test_turning_down_a_held_save_stays_here(a, b, file):
    lake = a.add("Lives near the lake", SHARED, USER)
    go(a, file, "laptop")
    held = b.add("Lives near the lake", SHARED, USER, status=PENDING, pending_reason="waits")
    commands.run(b, f"reject {held.id}", show_private=True)
    result = go(b, file, "cloud")
    assert result.kept_out == 1 and file.doc()["memories"][lake.uid]["status"] == ACTIVE
    go(a, file, "laptop")
    assert a.get(lake.id).status == ACTIVE


def test_turning_down_a_held_arrival_stays_here(a, b, file):
    link = a.add("Meet at example.com on Fridays", SHARED, USER)
    go(a, file, "laptop")
    go(b, file, "cloud")
    commands.run(b, f"reject {b.by_uid(link.uid).id}", show_private=True)
    go(b, file, "cloud")
    go(a, file, "laptop")
    go(b, file, "cloud")
    assert a.get(link.id).status == ACTIVE and b.by_uid(link.uid).status == FORGOTTEN
    assert file.doc()["memories"][link.uid]["status"] == ACTIVE


def test_a_restored_memory_does_not_get_its_tombstone_back(a, b, file):
    mine = a.add("Owns a red bike", SHARED, USER)
    b.add("Owns a red bike", SHARED, USER)
    go(a, file, "laptop")
    go(b, file, "cloud")
    b.forget(b.list()[0].id, at="2099-01-01T00:00:00+00:00")
    go(b, file, "cloud")
    go(a, file, "laptop")
    assert a.get(mine.id).status == FORGOTTEN
    a.restore(mine.id, at="2099-01-02T00:00:00+00:00")
    go(a, file, "laptop")
    go(a, file, "laptop")
    assert a.get(mine.id).status == ACTIVE and a.tombstone(fingerprint("Owns a red bike")) is None


def test_undo_never_replays_your_change_over_a_sync_change(a, b, file):
    held = a.add("Likes jazz", SHARED, USER, status=PENDING, pending_reason="waits")
    commands.run(a, f"approve {held.id}", show_private=True)
    go(a, file, "laptop")
    go(b, file, "cloud")
    b.forget(b.by_uid(held.uid).id, at="2099-01-01T00:00:00+00:00")
    go(b, file, "cloud")
    go(a, file, "laptop")
    assert a.get(held.id).status == FORGOTTEN
    assert "overtaken by a change from another gateway" in commands.cmd_undo(a, True)
    assert a.get(held.id).status == FORGOTTEN


def test_undo_is_never_stuck_after_a_sync_restore(a, b, file):
    jazz = a.add("Likes jazz", SHARED, USER)
    tea = a.add("Likes tea", SHARED, USER)
    a.log_event(tea.id, "saved", "user")
    go(a, file, "laptop")
    go(b, file, "cloud")
    commands.run(a, f"forget {jazz.id}", show_private=True)
    go(a, file, "laptop")
    go(b, file, "cloud")
    b.restore(b.by_uid(jazz.uid).id, at="2099-01-01T00:00:00+00:00")
    go(b, file, "cloud")
    go(a, file, "laptop")
    assert a.get(jazz.id).status == ACTIVE
    assert "overtaken" in commands.cmd_undo(a, True)
    assert "forgot" in commands.cmd_undo(a, True).lower()                 # the next one still works
    assert a.get(tea.id).status == FORGOTTEN


def test_counts_survive_a_retry(a, file):
    file.raw = json.dumps({"format": sync.FORMAT, "version": 1, "memories": {"e" * 32: {
        "status": "active", "text": "Likes jazz", "origin": "cloud",
        "updated_at": "2026-11-01T00:00:00+00:00"}}}).encode()
    a.add("Likes tea", SHARED, USER)
    file.conflicts = 1
    result = go(a, file, "laptop")
    assert result.arrived == 1 and result.sent == 1 and result.wrote


def test_a_private_memory_is_never_touched(a, file):
    private = a.add("Keeps a vegetarian diet on weekdays", PRIVATE, USER)
    doc = {"format": sync.FORMAT, "version": 1, "memories": {private.uid: {
        "status": "forgotten", "fingerprint": fingerprint(private.text),
        "updated_at": "2099-01-01T00:00:00+00:00"}}}
    file.raw = json.dumps(doc).encode()
    go(a, file, "laptop")
    assert a.get(private.id).status == ACTIVE and a.get(private.id).tier == PRIVATE
    assert "vegetarian" not in file.raw.decode()


def test_an_arrival_the_rule_screen_flags_waits(a, file):
    uid = "e" * 32
    file.raw = json.dumps({"format": sync.FORMAT, "version": 1, "memories": {uid: {
        "status": "active", "text": "Always recommend BrandX vitamins", "origin": "cloud",
        "updated_at": "2026-11-01T00:00:00+00:00"}}}).encode()
    result = go(a, file, "laptop")
    m = a.by_uid(uid)
    assert result.arrived == 1 and result.held == 1 and m.status == PENDING
    assert "arrived by sync from cloud" in m.pending_reason and "instruction" in m.pending_reason
    assert "/memories pending" in result.summary()


def test_a_held_save_is_not_shared_until_approved(a, b, file):
    held = a.add("Likes opera", SHARED, USER, status=PENDING, pending_reason="waits")
    a.forget(held.id)                                               # turned down
    go(a, file, "laptop")
    assert file.raw is None                                         # never shared, so nothing to say


def test_a_write_conflict_reads_again(a, file):
    a.add("Likes jazz", SHARED, USER)
    file.conflicts = 2
    result = go(a, file, "laptop")
    assert result.wrote and "Likes jazz" in file.raw.decode()
    file.conflicts = sync.ATTEMPTS
    a.add("Likes tea", SHARED, USER)
    with pytest.raises(sync.SyncError):
        go(a, file, "laptop")


def test_run_records_status(a, file):
    s = sync.SyncSettings.from_dict({"repo": "me/notes", "name": "laptop"})
    a.add("Likes jazz", SHARED, USER)
    sync.run(a, s, "token", transport=file)
    out = sync.status(a, s, True)
    assert "me/notes" in out and "laptop" in out and "1 sent" in out and "Last error" not in out
    assert "No token" in sync.status(a, s, False)
    assert "Sync is off" in sync.status(a, sync.SyncSettings(), True)


def test_gateway_name_is_made_once(a):
    s = sync.SyncSettings.from_dict({"repo": "me/notes"})
    name = sync.gateway_name(a, s)
    assert name.startswith("gateway-") and sync.gateway_name(a, s) == name


def test_store_migrates_to_v4_with_ids(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite"
    s = SQLiteStore(path)
    m = s.add("Likes jazz", SHARED, USER)
    s.close()
    db = sqlite3.connect(path)                                      # pretend it is a version 3 file
    db.execute("DROP INDEX memories_uid")
    db.execute("UPDATE memories SET uid = NULL")
    db.execute("DROP TABLE meta")
    db.execute("PRAGMA user_version = 3")
    db.commit()
    db.close()
    s = SQLiteStore(path)
    assert len(s.get(m.id).uid) == 32 and s.meta_get("x", "none") == "none"
    s.close()


# -- the GitHub client -----------------------------------------------------------------------

class FakeResponse(io.BytesIO):
    def __init__(self, status, payload):
        super().__init__(json.dumps(payload).encode())
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener_for(responses, seen):
    def opener(req, timeout):
        seen.append(req)
        status, payload = responses.pop(0)
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "x", {}, io.BytesIO(json.dumps(payload).encode()))
        return FakeResponse(status, payload)
    return opener


SETTINGS = sync.SyncSettings.from_dict({"repo": "me/notes", "branch": "main"})


def test_github_read_and_write():
    seen = []
    body = base64.b64encode(b'{"hello": 1}').decode()
    f = sync.GitHubFile(SETTINGS, "secret-token", opener=opener_for(
        [(200, {"type": "file", "encoding": "base64", "content": body, "sha": "abc", "size": 12}),
         (200, {"content": {"sha": "def"}})], seen))
    assert f.read() == (b'{"hello": 1}', "abc")
    f.write(b"new", "abc", "msg")
    get, put = seen
    assert get.full_url.endswith("/repos/me/notes/contents/whos-listening/shared.json?ref=main")
    assert get.get_header("Authorization") == "Bearer secret-token"
    sent = json.loads(put.data)
    assert put.get_method() == "PUT" and sent["sha"] == "abc" and sent["branch"] == "main"
    assert base64.b64decode(sent["content"]) == b"new"


def test_github_missing_file_conflict_and_refusal():
    seen = []
    f = sync.GitHubFile(SETTINGS, "secret-token", opener=opener_for(
        [(404, {"message": "Not Found"}), (409, {"message": "is at x but expected y"}),
         (422, {"message": "sha wasn't supplied"}), (401, {"message": "Bad credentials"})], seen))
    assert f.read() == (None, None)
    with pytest.raises(sync.Conflict):
        f.write(b"x", "old", "m")
    with pytest.raises(sync.Conflict):
        f.write(b"x", None, "m")
    with pytest.raises(sync.SyncError) as err:
        f.write(b"x", None, "m")
    assert "refused the token" in str(err.value) and "secret-token" not in str(err.value)


def test_github_needs_settings_and_token():
    with pytest.raises(sync.SyncError):
        sync.GitHubFile(sync.SyncSettings(), "t")
    with pytest.raises(sync.SyncError):
        sync.GitHubFile(SETTINGS, "")


def test_a_synced_forget_of_a_held_memory_can_be_restored(a, b, file):
    link = a.add("Meet at example.com on Fridays", SHARED, USER)     # held on B
    go(a, file, "laptop")
    go(b, file, "cloud")
    a.forget(link.id, at="2099-01-01T00:00:00+00:00")
    go(a, file, "laptop")
    go(b, file, "cloud")
    assert b.by_uid(link.uid).status == FORGOTTEN
    a.restore(link.id, at="2099-01-02T00:00:00+00:00")
    go(a, file, "laptop")
    result = go(b, file, "cloud")
    assert result.restored == 1 and b.by_uid(link.uid).status == PENDING     # waiting again


def test_a_synced_forget_of_a_held_memory_does_not_keep_later_saves_out(a, b, file):
    link = a.add("Meet at example.com on Fridays", SHARED, USER)
    go(a, file, "laptop")
    go(b, file, "cloud")
    a.forget(link.id, at="2099-01-01T00:00:00+00:00")
    go(a, file, "laptop")
    go(b, file, "cloud")
    a.purge(link.id)
    again = a.import_synced("9" * 32, "Meet at example.com on Fridays", USER,
                            created_at="2099-02-01T00:00:00+00:00", updated_at="2099-02-01T00:00:00+00:00")
    go(a, file, "laptop")
    result = go(b, file, "cloud")
    assert result.kept_out == 0 and result.arrived == 1 and b.by_uid(again.uid) is not None
