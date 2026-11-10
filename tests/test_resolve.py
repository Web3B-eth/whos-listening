"""Private and Shared duplicates, sync near-repeats, and the review workflow (resolve.py, the
review tool and commands). All data is made up."""

import json

import pytest

from test_sync import FakeFile
from whos_listening import commands, resolve, sync, tools
from whos_listening import settings as ws
from whos_listening.store import ACTIVE, FORGOTTEN, PENDING, PRIVATE, SHARED, Provenance, SQLiteStore

USER = Provenance(written_by="user", via="test")


def save(store, text, *, tier=PRIVATE, route=PRIVATE, setting="ask", judge=None, outside=""):
    return json.loads(tools.handle(store, tools.REMEMBER, {"text": text, "tier": tier}, route_tier=route,
                                   provenance=lambda: Provenance(), tier_duplicates=setting, judge=judge,
                                   outside_tool=outside))


def review(store, action, *, id=None, said="", route=PRIVATE, **kw):
    args = {"action": action, **({"id": id} if id is not None else {}), **kw}
    return json.loads(tools.handle(store, tools.REVIEW, args, route_tier=route, provenance=lambda: Provenance(),
                                   user_message=said, export=lambda show: "/tmp/x.md", scan=lambda: "started"))


BIKE = "Owns a Santa Cruz Hightower mountain bike"
BIKE2 = "Bought a Santa Cruz Hightower mountain bike"


# -- saves -------------------------------------------------------------------------------

def test_a_private_save_that_repeats_a_shared_memory_is_skipped(store):
    shared = store.add(BIKE, SHARED, USER)
    out = save(store, BIKE2)
    assert out["duplicate"]["id"] == shared.id and store.count() == 1


def test_a_shared_save_over_a_private_memory_waits_for_the_user_by_default(store):
    private = store.add(BIKE, PRIVATE, USER)
    out = save(store, BIKE2, tier=SHARED)
    new = store.get(out["pending"]["id"])
    assert new.status == PENDING and new.duplicate_of == private.id and new.tier == SHARED
    assert f"private memory #{private.id}" in new.pending_reason and "whos-listening_review" in out["note"]
    assert [i.kind for i in resolve.items(store)] == ["duplicate"]


def test_a_shared_route_never_learns_of_the_private_memory(tmp_path):
    for setting in ("ask", "shared", "private"):
        store = SQLiteStore(tmp_path / f"{setting}.sqlite")
        store.add(BIKE, PRIVATE, USER)
        out = save(store, BIKE2, tier=SHARED, route=SHARED, setting=setting)
        assert out == json.loads(tools._shared_route_saved(SHARED, None)), setting       # looks like any save
        assert "Hightower" not in json.dumps(out) and "#" not in json.dumps(out)
        store.close()


def test_the_shared_setting_replaces_the_private_memory(store):
    private = store.add(BIKE, PRIVATE, USER)
    out = save(store, BIKE2, tier=SHARED, setting="shared")
    assert out["saved"]["tier"] == SHARED and f"replaced private memory #{private.id}" in out["note"]
    assert store.get(private.id).status == FORGOTTEN


def test_the_shared_setting_still_waits_when_the_chat_holds_saves(store):
    private = store.add(BIKE, PRIVATE, USER)
    out = save(store, BIKE2, tier=SHARED, setting="shared", outside="web_extract")
    assert store.get(out["pending"]["id"]).duplicate_of == private.id
    assert store.get(private.id).status == ACTIVE                    # nothing forgotten on a held save


def test_the_private_setting_keeps_the_private_memory(store):
    private = store.add(BIKE, PRIVATE, USER)
    out = save(store, BIKE2, tier=SHARED, setting="private")
    assert out["duplicate"]["id"] == private.id and store.count(statuses=(ACTIVE, PENDING)) == 1


def test_a_shared_save_checks_shared_memories_first_and_asks_the_judge_both(store):
    shared = store.add(BIKE, SHARED, USER)
    store.add(BIKE + " now", PRIVATE, USER)
    assert save(store, BIKE2, tier=SHARED)["duplicate"]["id"] == shared.id
    seen = []
    assert save(store, "Speaks French", tier=SHARED, judge=lambda t, tiers: seen.append(tiers))["saved"]
    assert seen == [(SHARED, PRIVATE)]


def test_flagged_saves_are_never_auto_reviewed(store):
    store.add(BIKE, PRIVATE, USER)
    started = []
    json.loads(tools.handle(store, tools.REMEMBER, {"text": BIKE2, "tier": SHARED}, route_tier=PRIVATE,
                            provenance=lambda: Provenance(), outside_tool="web_extract",
                            on_outside_hold=lambda m: started.append(m) or True))
    assert started == []


def test_the_setting(tmp_path):
    f = tmp_path / "settings.json"
    s = ws.load(ws.Sources(f, {}, False))
    assert ws.route_settings(s).tier_duplicates == "ask" and "private and shared duplicates: ask  (default)" in ws.describe(s)
    assert ws.route_settings(ws.load(ws.Sources(f, {"tier_duplicates": "shared"}, False))).tier_duplicates == "shared"
    assert ws.route_settings(ws.load(ws.Sources(f, {"tier_duplicates": "default"}, False))).tier_duplicates == "ask"
    f.write_text('{"tier_duplicates": "newest"}')
    assert ws.route_settings(ws.load(ws.Sources(f, {}, False))).tier_duplicates == "ask"       # a typo asks


# -- keeping one or both -------------------------------------------------------------------------

def _pair(store):
    private = store.add(BIKE, PRIVATE, USER)
    new = store.get(save(store, BIKE2, tier=SHARED)["pending"]["id"])
    return private, new


def test_keep_new_forgets_the_old(store):
    private, new = _pair(store)
    kept, dropped = resolve.keep(store, new.id, show_private=True)
    assert kept.status == ACTIVE and kept.duplicate_of == 0 and [m.id for m in dropped] == [private.id]
    assert store.get(private.id).status == FORGOTTEN and resolve.items(store) == []


def test_keep_old_turns_down_the_new(store):
    private, new = _pair(store)
    kept, dropped = resolve.keep(store, private.id, show_private=True)
    assert kept.id == private.id and store.get(new.id).status == FORGOTTEN
    assert store.get(new.id).forgotten_from == PENDING                 # a local turn-down: never synced


def test_keep_both_and_approve_clear_the_flag(store):
    private, new = _pair(store)
    both = resolve.keep_both(store, new.id, show_private=True)
    assert {m.status for m in both} == {ACTIVE} and resolve.items(store) == []
    private2 = store.add("Has a dog named Max", PRIVATE, USER)
    new2 = store.get(save(store, "My dog is named Max", tier=SHARED)["pending"]["id"])
    commands.cmd_approve(store, str(new2.id), True)
    assert store.get(new2.id).duplicate_of == 0 and resolve.items(store) == []
    assert private2.id


def test_a_private_pair_isnt_chosen_where_private_text_is_hidden(store):
    private, new = _pair(store)
    with pytest.raises(Exception, match="private memory"):
        resolve.keep(store, new.id, show_private=False)
    assert "(private: shown only" in commands.cmd_review(store, show_private=False)


def test_a_flag_whose_other_memory_is_gone_clears_itself(store):
    private, new = _pair(store)
    store.forget(private.id)
    assert [i.kind for i in resolve.items(store)] == ["save"] and store.get(new.id).duplicate_of == 0


def test_the_review_command(store):
    private, new = _pair(store)
    held = store.add("Likes tea", PRIVATE, USER, status=PENDING, pending_reason="It came after a web page.")
    out = commands.run(store, "review", show_private=True)
    assert "These may say the same thing" in out and f"/memories keep {new.id}" in out
    assert f"/memories keep both {new.id}" in out and f"/memories approve {held.id}" in out
    assert "keep 41" in out
    out = commands.run(store, f"keep {new.id}", show_private=True)
    assert out.startswith("Kept") and f"Forgot #{private.id}" in out
    assert "Nothing is waiting" not in commands.run(store, "review", show_private=True)     # tea still waits
    assert "Say which one" in commands.run(store, "keep", show_private=True)


# -- the review tool: plain language -------------------------------------------------------------

def test_a_choice_needs_the_number_in_the_users_own_message(store):
    private, new = _pair(store)
    out = review(store, "keep", id=new.id, said="keep the new one")
    assert out["ok"] is False and "doesn't name" in out["error"] and store.get(new.id).status == PENDING
    out = review(store, "keep", id=new.id, said=f"ok let's keep new {new.id} and remove old number {private.id}")
    assert out["ok"] and out["forgotten"] == [private.id] and store.get(new.id).status == ACTIVE


def test_names_number():
    assert tools.names_number("keep 41", 41) and tools.names_number("#41 please", 41)
    assert tools.names_number("keep 41.", 41) and tools.names_number("41, then 12", 12)
    assert not tools.names_number("keep 410", 41) and not tools.names_number("1.41", 41)
    assert not tools.names_number("version 41.2", 41) and not tools.names_number("", 41)


def test_the_tool_lists_and_hides_private_pairs_on_a_shared_route(store):
    _pair(store)
    store.add("Likes jazz", SHARED, USER, status=PENDING, pending_reason="It came after a web page.")
    out = review(store, "list")
    assert [w["kind"] for w in out["waiting"]] == ["duplicate", "save"] and "may_repeat" in out["waiting"][0]
    assert out["waiting"][0]["may_repeat"]["line"].startswith(f"- #{out['waiting'][0]['may_repeat']['id']} <wl-private")
    out = review(store, "list", route=SHARED)
    assert [w["kind"] for w in out["waiting"]] == ["save"] and out["private_withheld"] == 1


def test_the_tool_approves_and_rejects_with_the_number(store):
    held = store.add("Likes jazz", SHARED, USER, status=PENDING, pending_reason="r")
    assert review(store, "approve", id=held.id, said="yes")["ok"] is False
    assert review(store, "approve", id=held.id, said=f"yes, save {held.id}")["status"] == ACTIVE
    p = store.add("Likes tea", PRIVATE, USER, status=PENDING, pending_reason="r")
    out = review(store, "approve", id=p.id, said=f"approve {p.id}", route=SHARED)
    assert out["ok"] is False and store.get(p.id).status == PENDING      # Private, on a Shared route


def test_the_tool_shows_everything_a_page_at_a_time(store):
    for i in range(60):
        store.add(f"Made-up fact number {i}", SHARED, USER)
    store.add("A private fact", PRIVATE, USER)
    first = review(store, "all")
    assert first["total"] == 61 and first["pages"] == 2 and len(first["memories"]) == 50
    assert len(review(store, "all", page=2)["memories"]) == 11
    shared_only = review(store, "all", route=SHARED)
    assert shared_only["total"] == 60 and shared_only["private_withheld"] == 1


def test_the_tool_exports_and_scans(store):
    assert review(store, "export")["file"] == "/tmp/x.md"
    assert review(store, "scan")["note"] == "started"
    assert review(store, "fly")["ok"] is False


# -- scan ----------------------------------------------------------------------------------------

def test_a_scan_flags_repeats_saved_earlier_and_forgets_nothing(store):
    a = store.add(BIKE, PRIVATE, USER)
    b = store.add(BIKE2, SHARED, USER)
    c = store.add("Has a peanut allergy", PRIVATE, USER)
    d = store.add("Allergic to peanuts", PRIVATE, USER)
    store.add("Likes jazz", SHARED, USER)
    asked = []

    def judge(text, tiers, only=None):
        asked.append(text)
        return c if text == d.text and only(c) else None

    found = resolve.scan(store, judge)
    assert found == 2 and store.get(b.id).duplicate_of == a.id and store.get(d.id).duplicate_of == c.id
    assert store.count() == 5                                     # nothing forgotten
    assert BIKE2 not in asked                                     # the word check found that one first
    resolve.keep_both(store, d.id, show_private=True)
    assert resolve.scan(store, judge) == 0                        # a pair kept on purpose isn't flagged again


def test_scan_status(store):
    assert resolve.scan_status(store) == ""
    resolve.set_scan_status(store, "running", 3, 10, 1)
    assert "3 of 10 checked" in resolve.scan_status(store)
    resolve.set_scan_status(store, "done", found=2)
    assert "2 found" in resolve.scan_status(store)


# -- export --------------------------------------------------------------------------------------

def test_export_writes_a_readable_private_file(store, tmp_path):
    store.add("Likes jazz", SHARED, USER)
    store.add("Has a peanut allergy", PRIVATE, USER)
    store.add("Likes tea", PRIVATE, USER, status=PENDING, pending_reason="It came after a web page.")
    path = resolve.export_markdown(store, tmp_path, show_private=True)
    text = path.read_text()
    assert "## Private (1)" in text and "Has a peanut allergy" in text and "## Shared (1)" in text
    assert "## Waiting for you (1)" in text and (path.stat().st_mode & 0o777) == 0o600
    text = resolve.export_markdown(store, tmp_path / "b", show_private=False).read_text()
    assert "peanut" not in text and "1 private memories are left out" in text and "Likes tea" not in text


# -- sync arrivals -------------------------------------------------------------------------------

@pytest.fixture
def two(tmp_path):
    a, b = SQLiteStore(tmp_path / "a.sqlite"), SQLiteStore(tmp_path / "b.sqlite")
    yield a, b
    a.close()
    b.close()


def near(store, setting="ask"):
    return sync.NearRepeats(find=lambda text: tools.repeat_of(store, text, SHARED), tier_duplicates=setting)


def test_an_arrival_that_rewords_a_shared_memory_is_left_out_once(two):
    a, b = two
    f = FakeFile()
    b.add(BIKE, SHARED, USER)
    a.add(BIKE2, SHARED, USER)
    sync.sync_once(a, f, "cloud")
    calls = []
    n = sync.NearRepeats(find=lambda text: calls.append(text) or tools.repeat_of(b, text, SHARED))
    result = sync.sync_once(b, f, "laptop", near=n)
    assert result.near_repeats == 1 and BIKE2 not in [m.text for m in b.list(limit=10)]
    assert "1 left out (already said here)" in result.summary()
    sync.sync_once(b, f, "laptop", near=n)
    assert calls == [BIKE2]                                      # remembered: not checked again


def test_an_arrival_like_a_private_memory_waits_for_the_user(two):
    a, b = two
    f = FakeFile()
    private = b.add(BIKE, PRIVATE, USER)
    a.add(BIKE2, SHARED, USER)
    sync.sync_once(a, f, "cloud")
    result = sync.sync_once(b, f, "laptop", near=near(b))
    new = [m for m in b.list(statuses=(PENDING,), limit=10)][0]
    assert result.flagged == 1 and new.duplicate_of == private.id and "arrived by sync from cloud" in new.pending_reason
    assert "waiting for you to choose" in result.summary()
    assert "(arrived from cloud)" in commands.cmd_review(b, True)
    # Keeping the private one forgets the shared copy on the other gateway too.
    resolve.keep(b, private.id, show_private=True)
    sync.sync_once(b, f, "laptop", near=near(b))
    sync.sync_once(a, f, "cloud")
    assert a.list(limit=10) == [] and a.list(statuses=(FORGOTTEN,), limit=10)[0].text == BIKE2


def test_keeping_the_arrival_forgets_the_private_memory_here_only(two):
    a, b = two
    f = FakeFile()
    private = b.add(BIKE, PRIVATE, USER)
    a.add(BIKE2, SHARED, USER)
    sync.sync_once(a, f, "cloud")
    sync.sync_once(b, f, "laptop", near=near(b))
    new = b.list(statuses=(PENDING,), limit=10)[0]
    resolve.keep(b, new.id, show_private=True)
    assert b.get(private.id).status == FORGOTTEN and b.get(new.id).status == ACTIVE
    sync.sync_once(b, f, "laptop", near=near(b))
    sync.sync_once(a, f, "cloud")
    assert [m.text for m in a.list(limit=10)] == [BIKE2]          # still there on the cloud


def test_the_settings_for_an_arrival_like_a_private_memory(two):
    a, b = two
    f = FakeFile()
    private = b.add(BIKE, PRIVATE, USER)
    a.add(BIKE2, SHARED, USER)
    sync.sync_once(a, f, "cloud")
    result = sync.sync_once(b, f, "laptop", near=near(b, "private"))
    assert result.removed == 1 and b.list(limit=10)[0].id == private.id
    sync.sync_once(a, f, "cloud")
    assert a.list(limit=10) == []                                 # forgotten on the cloud too


def test_the_shared_setting_lets_an_arrival_replace_the_private_memory(two):
    a, b = two
    f = FakeFile()
    private = b.add(BIKE, PRIVATE, USER)
    a.add(BIKE2, SHARED, USER)
    sync.sync_once(a, f, "cloud")
    result = sync.sync_once(b, f, "laptop", near=near(b, "shared"))
    assert result.replaced == 1 and b.get(private.id).status == FORGOTTEN
    assert [m.text for m in b.list(limit=10)] == [BIKE2]


def test_without_near_repeats_sync_works_as_before(two):
    a, b = two
    f = FakeFile()
    b.add(BIKE, SHARED, USER)
    a.add(BIKE2, SHARED, USER)
    sync.sync_once(a, f, "cloud")
    sync.sync_once(b, f, "laptop")
    assert len(b.list(limit=10)) == 2
