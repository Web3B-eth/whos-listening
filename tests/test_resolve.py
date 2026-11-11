"""Private and Shared duplicates, sync near-repeats, and the review workflow (resolve.py, the
review tool and commands). All data is made up."""

import json
import sqlite3

import pytest

from test_sync import FakeFile
from whos_listening import commands, resolve, sync, tools
from whos_listening import settings as ws
from whos_listening.store import ACTIVE, FORGOTTEN, PENDING, PRIVATE, SHARED, Provenance, SQLiteStore, StoreError, display_ref

USER = Provenance(written_by="user", via="test")


def save(store, text, *, tier=PRIVATE, route=PRIVATE, setting="ask", judge=None, outside=""):
    return json.loads(tools.handle(store, tools.REMEMBER, {"text": text, "tier": tier}, route_tier=route,
                                   provenance=lambda: Provenance(), tier_duplicates=setting, judge=judge,
                                   outside_tool=outside, shared_only=tier == SHARED))


def review(store, action, *, id=None, said="", route=PRIVATE, outside="", check=None, **kw):
    args = {"action": action, **({"id": id} if id is not None else {}), **kw}
    return json.loads(tools.handle(store, tools.REVIEW, args, route_tier=route, provenance=lambda: Provenance(),
                                   user_message=said, export=lambda show: "/tmp/x.md", scan=lambda: "started",
                                   outside_tool=outside, choice_check=check))


def test_review_approve_pending_save_keeps_save_when_forget_is_requested(store):
    held = store.add("Likes puzzles", SHARED, USER, status=PENDING, pending_reason="Outside content.")
    store.request_forget(held.id, "A forget was suggested.")
    waiting = review(store, "list")["waiting"]
    assert len(waiting) == 1 and waiting[0]["kind"] == "save"
    assert waiting[0]["forget_requested"] is True
    out = review(store, "approve", id=display_ref(held), said=f"Approve the pending save {display_ref(held)}")
    assert out["ok"] and out["status"] == ACTIVE
    assert not store.get(held.id).forget_reason and not store.is_forgotten_text(held.text)


def test_review_reject_pending_save_rejects_save_when_forget_is_requested(store):
    held = store.add("Likes puzzles", SHARED, USER, status=PENDING, pending_reason="Outside content.")
    store.request_forget(held.id, "A forget was suggested.")
    out = review(store, "reject", id=display_ref(held), said=f"Reject the pending save {display_ref(held)}")
    assert out["ok"] and out["status"] == FORGOTTEN
    assert store.is_forgotten_text(held.text)


def test_keep_rolls_back_forget_if_pending_keeper_cannot_activate(store, monkeypatch):
    older = store.add("Likes jazz", SHARED, USER)
    kept = store.add("Enjoys jazz music", SHARED, USER, status=PENDING, duplicate_of=older.id)
    before_events = len(store.events(limit=100))
    def fail_activation(*_args, **_kwargs):
        raise StoreError("The sync order limit was reached; this change was not made.")
    monkeypatch.setattr(store, "set_status", fail_activation)
    with pytest.raises(StoreError, match="order limit"):
        resolve.keep(store, kept.id, show_private=True)
    assert store.get(older.id).status == ACTIVE
    assert store.get(kept.id).status == PENDING
    assert store.get(kept.id).duplicate_of == older.id
    assert not store.is_forgotten_text(older.text)
    assert len(store.events(limit=100)) == before_events


def test_keep_both_rolls_back_first_activation_if_second_fails(store, monkeypatch):
    older = store.add("Likes jazz", SHARED, USER, status=PENDING)
    newer = store.add("Enjoys jazz music", SHARED, USER, status=PENDING, duplicate_of=older.id)
    original = store.set_status
    calls = 0
    def fail_second(memory_id, status, *, pending_reason=""):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise StoreError("The sync order limit was reached; this change was not made.")
        return original(memory_id, status, pending_reason=pending_reason)
    monkeypatch.setattr(store, "set_status", fail_second)
    with pytest.raises(StoreError, match="order limit"):
        resolve.keep_both(store, older.id, show_private=True)
    assert store.get(older.id).status == PENDING
    assert store.get(newer.id).status == PENDING
    assert store.get(newer.id).duplicate_of == older.id
    assert store.meta_get(resolve.KEPT_BOTH_KEY) == ""


def test_atomic_choice_blocks_other_connection_and_preserves_caller_transaction(tmp_path):
    path = tmp_path / "choice.sqlite"
    first, second = SQLiteStore(path), SQLiteStore(path)
    try:
        m = first.add("Likes jazz", SHARED, USER)
        second._db.execute("PRAGMA busy_timeout = 0")
        with first.atomic_choice():
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                second._db.execute("UPDATE memories SET text = text WHERE id = ?", (m.id,))
            with pytest.raises(StoreError, match="claimed before"):
                first.claim_memory_id()
        first._db.execute("BEGIN")
        with first.atomic_choice():
            first.meta_set("choice-test", "temporary")
        assert first._db.in_transaction
        first._db.execute("ROLLBACK")
        assert first.meta_get("choice-test") == ""
    finally:
        first.close()
        second.close()


BIKE = "Owns a Santa Cruz Hightower mountain bike"
BIKE2 = "Bought a Santa Cruz Hightower mountain bike"


# -- saves -------------------------------------------------------------------------------

def test_a_private_save_that_repeats_a_shared_memory_is_skipped(store):
    shared = store.add(BIKE, SHARED, USER)
    out = save(store, BIKE2)
    assert out["duplicate"]["id"] == display_ref(shared) and store.count() == 1


def test_a_shared_save_over_a_private_memory_is_flagged_for_the_user_by_default(store):
    private = store.add(BIKE, PRIVATE, USER)
    out = save(store, BIKE2, tier=SHARED)
    new = store.get(out["saved"]["id"])
    assert new.status == ACTIVE and new.duplicate_of == private.id and new.tier == SHARED
    assert new.pending_reason == ""                               # the pair never goes into a reason
    assert f"private memory {display_ref(private)}" in out["note"] and "whos-listening_review" in out["note"]
    assert [(i.kind, i.memory.id, [m.id for m in i.others]) for i in resolve.items(store)] == \
        [("duplicate", private.id, [new.id])]


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
    assert out["saved"]["tier"] == SHARED and f"replaced private memory {display_ref(private)}" in out["note"]
    assert store.get(private.id).status == FORGOTTEN


def test_the_shared_setting_still_waits_when_the_chat_holds_saves(store):
    private = store.add(BIKE, PRIVATE, USER)
    out = save(store, BIKE2, tier=SHARED, setting="shared", outside="web_extract")
    assert store.get(out["pending"]["id"]).duplicate_of == private.id
    assert store.get(private.id).status == ACTIVE                    # nothing forgotten on a held save


def test_the_private_setting_keeps_the_private_memory(store):
    private = store.add(BIKE, PRIVATE, USER)
    out = save(store, BIKE2, tier=SHARED, setting="private")
    assert out["duplicate"]["id"] == display_ref(private) and store.count(statuses=(ACTIVE, PENDING)) == 1


def test_a_shared_save_checks_shared_memories_first_and_asks_the_judge_both(store):
    shared = store.add(BIKE, SHARED, USER)
    store.add(BIKE + " now", PRIVATE, USER)
    assert save(store, BIKE2, tier=SHARED)["duplicate"]["id"] == display_ref(shared)
    seen = []
    assert save(store, "Speaks French", tier=SHARED, judge=lambda t, tiers: seen.append(tiers))["saved"]
    assert seen == [(SHARED, PRIVATE)]


def test_a_flagged_save_held_for_outside_content_keeps_its_own_reason(store):
    private = store.add(BIKE, PRIVATE, USER)
    started = []
    out = json.loads(tools.handle(store, tools.REMEMBER, {"text": BIKE2, "tier": SHARED}, route_tier=PRIVATE,
                                  provenance=lambda: Provenance(), outside_tool="web_extract",
                                      on_outside_hold=lambda m: started.append(m) or True, shared_only=True))
    new = store.get(out["pending"]["id"])
    assert new.duplicate_of == private.id and "web_extract" in new.pending_reason and len(started) == 1
    assert "#" + str(private.id) not in new.pending_reason
    review_text = commands.cmd_review(store, True)
    assert f"{display_ref(new)} also waits because:" in review_text           # the hold is never hidden by the pair


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
    new = store.get(save(store, BIKE2, tier=SHARED)["saved"]["id"])
    return private, new


def test_keep_new_forgets_the_old(store):
    private, new = _pair(store)
    kept, dropped = resolve.keep(store, new.id, show_private=True)
    assert kept.status == ACTIVE and kept.duplicate_of == 0 and [m.id for m in dropped] == [private.id]
    assert store.get(private.id).status == FORGOTTEN and resolve.items(store) == []


def test_keep_old_forgets_the_new(store):
    private, new = _pair(store)
    kept, dropped = resolve.keep(store, private.id, show_private=True)
    assert kept.id == private.id and store.get(new.id).status == FORGOTTEN


def test_two_saves_flagged_against_one_memory_are_one_group(store):
    private, new = _pair(store)       # a second one, as from a sync arrival or a scan:
    other = store.add("Got a Santa Cruz Hightower last spring", SHARED, USER, duplicate_of=private.id)
    groups = resolve.items(store)
    assert len(groups) == 1 and [m.id for m in groups[0].others] == [new.id, other.id]
    assert f"/memories keep {display_ref(private)}  ·  /memories keep {display_ref(new)}  ·  /memories keep {display_ref(other)}" in \
        commands.cmd_review(store, True)
    resolve.keep(store, new.id, show_private=True)                  # keeping one of the new ones...
    assert store.get(private.id).status == FORGOTTEN
    assert store.get(other.id).duplicate_of == new.id               # ...flags the other against it


def test_keep_both_and_approve_clear_the_flag(store):
    private, new = _pair(store)
    both = resolve.keep_both(store, new.id, show_private=True)
    assert {m.status for m in both} == {ACTIVE} and resolve.items(store) == []
    private2 = store.add("Has a dog named Max", PRIVATE, USER)
    new2 = store.get(save(store, "My dog is named Max", tier=SHARED)["saved"]["id"])
    store.set_status(new2.id, PENDING, pending_reason="It came after a web page.")
    commands.cmd_approve(store, display_ref(new2), True)
    assert store.get(new2.id).duplicate_of == 0 and resolve.items(store) == []
    assert private2.id


def test_a_private_pair_isnt_chosen_where_private_text_is_hidden(store):
    private, new = _pair(store)
    with pytest.raises(Exception, match="private memory"):
        resolve.keep(store, new.id, show_private=False)
    assert "Nothing is waiting for you" in commands.cmd_review(store, show_private=False)


def test_a_flag_whose_other_memory_is_gone_clears_itself(store):
    private, new = _pair(store)
    store.forget(private.id)
    assert resolve.items(store) == [] and store.get(new.id).duplicate_of == 0 and store.get(new.id).status == ACTIVE


def test_the_review_command(store):
    private, new = _pair(store)
    held = store.add("Likes tea", PRIVATE, USER, status=PENDING, pending_reason="It came after a web page.")
    out = commands.run(store, "review", show_private=True)
    assert "These may say the same thing" in out and f"/memories keep {display_ref(new)}" in out
    assert f"keep all: /memories keep both {display_ref(new)}" in out and f"/memories approve {display_ref(held)}" in out
    assert "keep S3" in out
    out = commands.run(store, f"keep {display_ref(new)}", show_private=True)
    assert out.startswith("Kept") and f"Forgot {display_ref(private)}" in out
    assert "Nothing is waiting" not in commands.run(store, "review", show_private=True)     # tea still waits
    assert "Say which one" in commands.run(store, "keep", show_private=True)


# -- the review tool: plain language -------------------------------------------------------------

def test_with_no_outside_content_a_choice_applies_on_the_users_word(store):
    private, new = _pair(store)
    out = review(store, "keep", id=display_ref(new), said="keep the new one")
    assert out["ok"] and out["forgotten"] == [display_ref(private)] and store.get(new.id).status == ACTIVE


def test_a_choice_needs_a_message_from_the_user_this_turn(store):
    private, new = _pair(store)
    out = review(store, "keep", id=display_ref(new), said="")
    assert out["ok"] is False and f'"keep {display_ref(new)}"' in out["error"] and store.get(private.id).status == ACTIVE


def test_after_outside_content_a_choice_needs_the_number_or_auto_review(store):
    private, new = _pair(store)
    n, p = display_ref(new), display_ref(private)
    out = review(store, "keep", id=n, said="keep the new one", outside="read_file")      # Auto-review off
    assert out["ok"] is False and "read outside content (read_file)" in out["error"]
    assert f'"keep {n}"' in out["error"] and store.get(private.id).status == ACTIVE
    seen = []
    out = review(store, "keep", id=n, said="yes", outside="read_file",
                 check=lambda change: seen.append(change) or (False, "The user didn't ask for this."))
    assert out["ok"] is False and "Auto-review couldn't confirm" in out["error"] and "didn't ask for this" in out["error"]
    assert seen == [f'Keep the newer memory {n} ("{BIKE2}") and forget {p} ("{BIKE}"), '
                    "which may say the same thing."]
    out = review(store, "keep", id=n, said="yes", outside="read_file", check=lambda change: 1 / 0)
    assert out["ok"] is False and "could not run (ZeroDivisionError)" in out["error"]
    assert store.get(private.id).status == ACTIVE                                       # nothing changed yet
    out = review(store, "keep", id=n, said="yes, keep the newer one", outside="read_file",
                 check=lambda change: (True, "The user agreed."))
    assert out["ok"] and out["forgotten"] == [p]


def test_after_outside_content_the_number_still_works_without_a_check(store):
    private, new = _pair(store)
    n, p = display_ref(new), display_ref(private)
    called = []
    out = review(store, "keep", id=n, said=f"ok let's keep new {n} and remove old number {p}",
                 outside="web_extract", check=lambda change: called.append(1) or (False, "no"))
    assert out["ok"] and out["forgotten"] == [p] and called == []


def test_a_shared_route_never_sends_a_private_pair_to_the_check(store):
    private, new = _pair(store)
    called = []
    out = review(store, "keep", id=display_ref(new), said="yes", route=SHARED, outside="read_file",
                 check=lambda change: called.append(change) or (True, ""))
    assert out["ok"] is False and out["error"] == f"{display_ref(new)} isn't an available duplicate here." and called == []
    assert store.get(private.id).status == ACTIVE


def test_describe_choice_says_what_each_choice_does(store):
    private, new = _pair(store)
    assert tools.describe_choice(store, "keep", private).startswith(f'Keep the older memory {display_ref(private)}')
    assert tools.describe_choice(store, "keep_both", new).startswith(f"Keep all of {display_ref(new)}")
    held = store.add("Likes jazz", SHARED, USER, status=PENDING, pending_reason="r")
    assert tools.describe_choice(store, "approve", held) == f'Save the waiting memory {display_ref(held)} ("Likes jazz").'
    assert tools.describe_choice(store, "reject", held).startswith(f"Turn down the waiting memory {display_ref(held)}")


def test_names_number():
    assert tools.names_number("keep 41", 41) and tools.names_number("#41 please", 41)
    assert tools.names_number("keep 41.", 41) and tools.names_number("41, then 12", 12)
    assert not tools.names_number("keep 410", 41) and not tools.names_number("1.41", 41)
    assert not tools.names_number("version 41.2", 41) and not tools.names_number("", 41)


def test_the_tool_lists_and_hides_private_pairs_on_a_shared_route(store):
    private, new = _pair(store)
    store.add("Likes jazz", SHARED, USER, status=PENDING, pending_reason="It came after a web page.")
    out = review(store, "list")
    assert [w["kind"] for w in out["waiting"]] == ["duplicate", "save"]
    assert out["waiting"][0]["memories"][0]["line"].startswith(f"- {display_ref(private)} <wl-private")   # tagged
    assert f'in their own words or by number (for example "keep {display_ref(private)}")' in out["note"]
    shared_view = review(store, "list", route=SHARED)
    assert [w["kind"] for w in shared_view["waiting"]] == ["save"] and "private_omitted" not in shared_view
    held = shared_view["waiting"][0]["memory"]["id"]
    assert f'"approve {held}"' in shared_view["note"]              # a waiting save is approved; a number this route sees


def test_a_shared_route_cant_tell_its_save_matched_a_private_memory(tmp_path):
    def views(flag):
        s = SQLiteStore(tmp_path / f"{flag}.sqlite")
        s.add(BIKE if flag else "Likes tea", PRIVATE, USER)
        out = save(s, BIKE2, tier=SHARED, route=SHARED)
        new_id = display_ref(s.list(tiers=(SHARED,), limit=5)[0])
        said = f"keep {new_id}"
        got = (out, review(s, "list", route=SHARED), review(s, "all", route=SHARED)["private_omitted"],
               review(s, "keep", id=new_id, said=said, route=SHARED), review(s, "keep_both", id=new_id, said=said,
                                                                          route=SHARED),
               [m.text for m in s.search("Hightower", tiers=(SHARED,))])
        s.close()
        return json.dumps(got)
    assert views(True) == views(False)                            # matched or not, it all looks the same


def test_the_tool_approves_and_rejects_with_the_number(store):
    held = store.add("Likes jazz", SHARED, USER, status=PENDING, pending_reason="r")
    ref = display_ref(held)
    assert review(store, "approve", id=ref, said="yes", outside="read_file")["ok"] is False
    assert review(store, "approve", id=ref, said=f"yes, save {ref}", outside="read_file")["status"] == ACTIVE
    assert review(store, "approve", id=ref, said="yes")["ok"] is False          # no longer waiting
    other = store.add("Likes tea a lot", SHARED, USER, status=PENDING, pending_reason="r")
    assert review(store, "approve", id=display_ref(other), said="yes, save that one")["status"] == ACTIVE
    p = store.add("Likes tea", PRIVATE, USER, status=PENDING, pending_reason="r")
    for action in ("approve", "reject"):
        out = review(store, action, id=display_ref(p), said=f"{action} {display_ref(p)}", route=SHARED)
        assert out["ok"] is False and store.get(p.id).status == PENDING     # Private, on a Shared route: untouched
    assert store.events(memory_id=held.id, limit=1)[0].actor == "assistant"


def test_the_tool_shows_everything_a_page_at_a_time(store):
    for i in range(60):
        store.add(f"Made-up fact number {i}", SHARED, USER)
    store.add("A private fact", PRIVATE, USER)
    first = review(store, "all")
    assert first["total"] == 61 and first["pages"] == 2 and len(first["memories"]) == 50
    assert len(review(store, "all", page=2)["memories"]) == 11
    shared_only = review(store, "all", route=SHARED)
    assert shared_only["total"] == 60 and shared_only["private_omitted"] is True
    assert f'"forget {first["memories"][0]["id"]}"' in first["note"] and "can't be edited" in first["note"]


def test_nothing_to_review_needs_no_number_hint(store):
    assert "number" not in review(store, "list")["note"].split("let them choose.")[1]
    assert "note" not in review(store, "all")


def test_shared_export_note_does_not_count_private_rows(tmp_path):
    notes = []
    for name in ("none", "active", "pending", "forgotten"):
        s = SQLiteStore(tmp_path / f"{name}.sqlite")
        if name != "none":
            m = s.add("Owns a blue bicycle", PRIVATE, USER,
                      status=PENDING if name == "pending" else ACTIVE)
            if name == "forgotten":
                s.forget(m.id)
        body = resolve.export_markdown(s, tmp_path / name, show_private=False).read_text()
        notes.append(body.split("Private memories are not included", 1)[1])
        assert "blue bicycle" not in body
        s.close()
    assert notes[0] == notes[1] == notes[2] == notes[3]


def test_the_tool_exports_and_scans(store):
    assert review(store, "export")["file"] == "/tmp/x.md"
    assert "started" in review(store, "scan")["note"]
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
    assert "peanut" not in text and "Private memories are not included" in text and "Likes tea" not in text


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
    new = [m for m in b.list(tiers=(SHARED,), limit=10)][0]
    assert result.flagged == 1 and new.duplicate_of == private.id and new.status == ACTIVE and new.pending_reason == ""
    assert "for you to choose" in result.summary()
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
    new = b.list(tiers=(SHARED,), limit=10)[0]
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


def test_a_held_arrival_chosen_against_comes_back_waiting_if_restored(two):
    a, b = two
    f = FakeFile()
    private = b.add(BIKE, PRIVATE, USER)
    a.add(BIKE2 + " https://example.com/deal", SHARED, USER)        # the rule screen holds a link
    sync.sync_once(a, f, "cloud")
    sync.sync_once(b, f, "laptop", near=sync.NearRepeats(find=lambda text: private))   # as a judge would say
    new = b.list(statuses=(PENDING,), limit=10)[0]
    assert new.duplicate_of == private.id
    resolve.keep(b, private.id, show_private=True)
    assert b.restore(new.id).status == PENDING                       # never active without the user's OK
    b.forget(new.id, everywhere=True)
    sync.sync_once(b, f, "laptop", near=near(b))
    sync.sync_once(a, f, "cloud")
    assert a.list(limit=10) == []                                    # the forget still travels
