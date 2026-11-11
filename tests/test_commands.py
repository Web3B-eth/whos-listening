"""The /memories commands over a real store. All data is made up."""

from whos_listening import commands, resolve
from whos_listening.store import ACTIVE, FORGOTTEN, PENDING, Provenance, display_ref


def run(store, args, show_private=True):
    return commands.run(store, args, show_private=show_private)


def assistant():
    return Provenance(written_by="assistant", via="whos-listening_remember", route="custom @ 127.0.0.1 / m",
                      route_tier="private", session_id="s1", turn="2")


def test_list_hides_private_off_local_platforms(store):
    store.add("Has a peanut allergy", "private", assistant())
    store.add("Likes jazz", "shared", assistant())
    assert "peanut" in run(store, "")
    out = run(store, "list", show_private=False)
    assert "peanut" not in out and "jazz" in out and "private" not in out.lower()
    assert commands.shows_private("cli") and not commands.shows_private("telegram")


def test_pending_approve_and_reject(store):
    a = store.add("Prefers window seats", "private", assistant(), status=PENDING,
                  pending_reason="This chat read outside content (read_file), so it waits for you.")
    b = store.add("Visit www.example.com daily", "shared", assistant(), status=PENDING,
                  pending_reason="It has a link or web address.")
    out = run(store, "pending")
    assert display_ref(a) in out and "read_file" in out and "link" in out
    assert "Saved" in run(store, f"approve {display_ref(a)}")
    assert store.get(a.id).status == "active"
    assert "Turned down" in run(store, f"reject {display_ref(b)}")
    assert store.is_forgotten_text("Visit www.example.com daily")
    assert run(store, "pending") == "Nothing is waiting for you."
    assert "isn't waiting" in run(store, f"approve {display_ref(a)}")


def test_shared_views_hide_legacy_auto_review_reasons(store, tmp_path):
    private = store.add("Keeps a blue notebook", "private", assistant(), status=PENDING,
                        pending_reason="Outside content. Auto-review: Keeps a blue notebook")
    shared = store.add("Likes tea", "shared", assistant(), status=PENDING,
                       pending_reason="Outside content. Auto-review: The blue notebook is private")
    safe = store.add("Visits www.example.com", "shared", assistant(), status=PENDING,
                     pending_reason="It has a link or web address.")
    for command in ("pending", "review", f"show {display_ref(shared)}"):
        out = run(store, command, show_private=False)
        assert "blue notebook" not in out
        assert "Needs your review" in out
    assert "No memory" in run(store, f"show {display_ref(private)}", show_private=False)
    exported = resolve.export_markdown(store, tmp_path, show_private=False).read_text()
    assert "blue notebook" not in exported and "Likes tea" in exported
    assert "It has a link or web address." in exported
    assert "It has a link or web address." in run(store, f"show {display_ref(safe)}", show_private=False)
    assert "Keeps a blue notebook" in run(store, "pending", show_private=True)
    assert "The blue notebook is private" in run(store, "review", show_private=True)


def test_forget_requests(store):
    m = store.add("Likes jazz", "shared", assistant())
    store.request_forget(m.id, "This chat read outside content (web_extract), so a forget waits for you.")
    assert "forget S" in run(store, "pending")
    assert "Kept" in run(store, f"reject {display_ref(m)}") and store.get(m.id).status == "active"
    store.request_forget(m.id, "again")
    assert "Forgot" in run(store, f"approve {display_ref(m)}") and store.get(m.id).status == "forgotten"


def test_pending_save_with_forget_request_has_one_current_choice(store):
    m = store.add("Likes puzzles", "shared", assistant(), status=PENDING, pending_reason="Outside content.")
    store.request_forget(m.id, "This chat read outside content, so a forget waits for you.")
    pending = run(store, "pending")
    assert pending.count(f"save {display_ref(m)}") == 1
    review = run(store, "review")
    assert "A save waiting" in review and "A forget waiting" not in review
    assert f"/memories forget {display_ref(m)}" in review
    assert "Saved" in run(store, f"approve {display_ref(m)}")
    assert store.get(m.id).status == ACTIVE and not store.get(m.id).forget_reason
    assert not store.is_forgotten_text(m.text)

    another = store.add("Likes chess", "shared", assistant(), status=PENDING, pending_reason="Outside content.")
    store.request_forget(another.id, "Try again.")
    assert "Turned down" in run(store, f"reject {display_ref(another)}")
    assert store.get(another.id).status == FORGOTTEN and store.is_forgotten_text(another.text)


def test_duplicate_choice_waits_for_pending_forget_decision(store):
    older = store.add("Likes puzzles", "shared", assistant())
    newer = store.add("Enjoys puzzles", "shared", assistant(), status=PENDING,
                      pending_reason="Outside content.", duplicate_of=older.id)
    store.request_forget(newer.id, "Forget waits for you.")
    assert "A save waiting" in run(store, "review")
    assert "These may say the same thing" not in run(store, "review")
    assert "decide the forget" in run(store, f"keep {display_ref(newer)}").lower()
    assert store.get(newer.id).status == PENDING
    assert "Saved" in run(store, f"approve {display_ref(newer)}")
    assert store.get(newer.id).status == ACTIVE
    assert not store.get(newer.id).forget_reason


def test_add_forget_restore(store):
    out = run(store, "add shared Likes jazz")
    ref = out.split("Saved ", 1)[1].split()[0]
    assert store.resolve_reference(ref).provenance.written_by == "user"
    assert "Forgot" in run(store, f"forget {ref}")
    assert "Restored" in run(store, f"restore {ref}")
    # the user's own add may bring back a forgotten fact
    run(store, f"forget {ref}")
    assert "Saved" in run(store, "add shared likes jazz")


def test_direct_owner_forget_private_from_shared_view_is_nonrevealing(store):
    m = store.add("Keeps a blue notebook", "private", assistant())
    out = run(store, f"forget {display_ref(m)}", show_private=False)
    assert out == run(store, "forget 99999", show_private=False)
    assert "blue notebook" not in out and str(m.id) not in out
    assert store.get(m.id).status == "forgotten"
    assert "Restored" in run(store, f"restore {display_ref(m)}", show_private=True)


def test_undo_walks_back(store):
    run(store, "add Collects stamps")
    m = store.list()[0]
    mid = m.id
    run(store, f"forget {display_ref(m)}")
    assert "restored" in run(store, "undo")          # undo the forget
    assert store.get(mid).status == "active"
    assert "forgot" in run(store, "undo")            # undo the add
    assert store.get(mid).status == "forgotten"
    assert run(store, "undo") == "Nothing to undo."


def test_undo_an_approval_and_a_rejection(store):
    m = store.add("Prefers tea", "private", assistant(), status=PENDING, pending_reason="r")
    store.log_event(m.id, "held", "assistant")
    run(store, f"approve {display_ref(m)}")
    run(store, "undo")
    assert store.get(m.id).status == "pending"
    run(store, f"reject {display_ref(m)}")
    run(store, "undo")
    assert store.get(m.id).status == "pending" and not store.is_forgotten_text("Prefers tea")


def test_show_has_provenance_and_history(store):
    m = store.add("Has a peanut allergy", "private", assistant())
    store.log_event(m.id, "saved", "assistant")
    out = run(store, f"show {display_ref(m)}")
    assert "assistant via whos-listening_remember" in out and "127.0.0.1" in out and "(private)" in out
    assert "saved by assistant" in out
    assert "peanut" not in run(store, f"show {display_ref(m)}", show_private=False)


def test_bad_input_answers(store):
    assert "No memory" in run(store, "approve x")
    assert "Usage" in run(store, "approve")
    assert "No memory" in run(store, "show 99")
    assert "Unknown command" in run(store, "dance")
    assert "/memories pending" in run(store, "help")
    assert run(store, 'add "unclosed') .startswith("Saved")


def test_undo_of_a_forgotten_pending_memory_keeps_it_pending(store):
    m = store.add("Prefers window seats", "private", assistant(), status=PENDING, pending_reason="outside")
    store.log_event(m.id, "held", "assistant")
    store.forget(m.id)
    store.log_event(m.id, "forgot", "assistant")
    run(store, "undo")
    assert store.get(m.id).status == "pending"
    run(store, f"forget {display_ref(m)}")
    run(store, f"restore {display_ref(m)}")
    assert store.get(m.id).status == "pending"


def test_undo_skips_purged_memories(store):
    run(store, "add Likes jazz")
    run(store, "add Collects stamps")
    stamps = [m for m in store.list() if "stamps" in m.text][0]
    run(store, f"forget {display_ref(stamps)}")
    store.purge(stamps.id)
    assert "forgot" in run(store, "undo")       # reaches the jazz save past the purged events
    assert store.count() == 0


def test_add_keeps_the_text_as_typed(store):
    run(store, "add I don't eat pork and won't start")
    run(store, "add shared Keeps notes in C:\\Users\\me")
    texts = sorted(m.text for m in store.list())
    assert texts == ["I don't eat pork and won't start", "Keeps notes in C:\\Users\\me"]


def test_private_memories_are_approved_only_where_they_can_be_read(store):
    m = store.add("Has a peanut allergy", "private", assistant(), status=PENDING, pending_reason="r")
    out = run(store, f"approve {display_ref(m)}", show_private=False)
    assert "No memory" in out and store.get(m.id).status == "pending"
    assert "Saved" in run(store, f"approve {display_ref(m)}", show_private=True)
    assert not commands.shows_private("") and not commands.shows_private(None)


def test_platform_settings_decide_where_private_text_shows():
    from whos_listening.routes import RouteSettings
    s = RouteSettings.from_dict({"private_platforms": ["signal"], "shared_platforms": ["desktop"]})
    assert commands.shows_private("signal", s) and not commands.shows_private("desktop", s)
    assert commands.shows_private("cli", s) and not commands.shows_private("", s)


def test_shared_platform_views_do_not_reveal_private_hold_or_duplicate(store):
    visible = store.add("Likes jazz", "shared", assistant())
    baseline = {cmd: run(store, cmd, show_private=False) for cmd in ("list", "pending", "review")}
    private = store.add("Keeps a blue notebook", "private", assistant(), status=PENDING,
                        pending_reason="Outside content.")
    store.log_event(private.id, "held", "assistant")
    assert {cmd: run(store, cmd, show_private=False) for cmd in baseline} == baseline
    for cmd in ("show", "approve", "reject", "forget", "restore"):
        assert run(store, f"{cmd} {display_ref(private)}", show_private=False) == run(store, f"{cmd} P99999", show_private=False).replace("P99999", display_ref(private))
    assert "No memory" in run(store, f"show {display_ref(private)}", show_private=False)
    assert store.get(private.id).status == FORGOTTEN
    assert "Likes jazz" in run(store, f"show {display_ref(visible)}", show_private=False)


def test_shared_pending_view_hides_private_duplicate_link(store):
    private = store.add("Owns a blue mountain bike", "private", assistant())
    shared = store.add("Owns a blue bike", "shared", assistant(), status=PENDING,
                       duplicate_of=private.id)
    assert str(private.id) not in run(store, "pending", show_private=False)
    assert str(shared.id) not in run(store, "pending", show_private=False)
    assert "Nothing is waiting" in run(store, "pending", show_private=False)


def test_add_on_a_shared_gateway(store):
    out = commands.run(store, "add Keeps bees", show_private=True, private_refused="No private memory was saved: x.")
    assert "No private memory was saved" in out and "add shared" in out and store.count() == 0
    assert "Saved" in commands.run(store, "add shared Likes jazz", show_private=True, private_refused="x")
