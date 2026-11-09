"""The /memories commands over a real store. All data is made up."""

from whos_listening import commands
from whos_listening.store import PENDING, Provenance


def run(store, args, show_private=True):
    return commands.run(store, args, show_private=show_private)


def assistant():
    return Provenance(written_by="assistant", via="wl_remember", route="custom @ 127.0.0.1 / m",
                      route_tier="private", session_id="s1", turn="2")


def test_list_hides_private_off_local_platforms(store):
    store.add("Has a peanut allergy", "private", assistant())
    store.add("Likes jazz", "shared", assistant())
    assert "peanut" in run(store, "")
    out = run(store, "list", show_private=False)
    assert "peanut" not in out and "jazz" in out and commands.HIDDEN in out
    assert commands.shows_private("cli") and not commands.shows_private("telegram")


def test_pending_approve_and_reject(store):
    a = store.add("Prefers window seats", "private", assistant(), status=PENDING,
                  pending_reason="This chat read outside content (read_file), so it waits for you.")
    b = store.add("Visit www.example.com daily", "shared", assistant(), status=PENDING,
                  pending_reason="It has a link or web address.")
    out = run(store, "pending")
    assert f"#{a.id}" in out and "read_file" in out and "link" in out
    assert "Saved" in run(store, f"approve {a.id}")
    assert store.get(a.id).status == "active"
    assert "Turned down" in run(store, f"reject #{b.id}")
    assert store.is_forgotten_text("Visit www.example.com daily")
    assert run(store, "pending") == "Nothing is waiting for you."
    assert "isn't waiting" in run(store, f"approve {a.id}")


def test_forget_requests(store):
    m = store.add("Likes jazz", "shared", assistant())
    store.request_forget(m.id, "This chat read outside content (web_extract), so a forget waits for you.")
    assert "forget #" in run(store, "pending")
    assert "Kept" in run(store, f"reject {m.id}") and store.get(m.id).status == "active"
    store.request_forget(m.id, "again")
    assert "Forgot" in run(store, f"approve {m.id}") and store.get(m.id).status == "forgotten"


def test_add_forget_restore(store):
    out = run(store, "add shared Likes jazz")
    mid = int(out.split("#")[1].split()[0])
    assert store.get(mid).provenance.written_by == "user"
    assert "Forgot" in run(store, f"forget {mid}")
    assert "Restored" in run(store, f"restore {mid}")
    # the user's own add may bring back a forgotten fact
    run(store, f"forget {mid}")
    assert "Saved" in run(store, "add shared likes jazz")


def test_undo_walks_back(store):
    run(store, "add Collects stamps")
    mid = store.list()[0].id
    run(store, f"forget {mid}")
    assert "restored" in run(store, "undo")          # undo the forget
    assert store.get(mid).status == "active"
    assert "forgot" in run(store, "undo")            # undo the add
    assert store.get(mid).status == "forgotten"
    assert run(store, "undo") == "Nothing to undo."


def test_undo_an_approval_and_a_rejection(store):
    m = store.add("Prefers tea", "private", assistant(), status=PENDING, pending_reason="r")
    store.log_event(m.id, "held", "assistant")
    run(store, f"approve {m.id}")
    run(store, "undo")
    assert store.get(m.id).status == "pending"
    run(store, f"reject {m.id}")
    run(store, "undo")
    assert store.get(m.id).status == "pending" and not store.is_forgotten_text("Prefers tea")


def test_show_has_provenance_and_history(store):
    m = store.add("Has a peanut allergy", "private", assistant())
    store.log_event(m.id, "saved", "assistant")
    out = run(store, f"show {m.id}")
    assert "assistant via wl_remember" in out and "127.0.0.1" in out and "(private)" in out
    assert "saved by assistant" in out
    assert "peanut" not in run(store, f"show {m.id}", show_private=False)


def test_bad_input_answers(store):
    assert "Give a memory number" in run(store, "approve x")
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
    run(store, f"forget {m.id}")
    run(store, f"restore {m.id}")
    assert store.get(m.id).status == "pending"


def test_undo_skips_purged_memories(store):
    run(store, "add Likes jazz")
    run(store, "add Collects stamps")
    stamps = [m for m in store.list() if "stamps" in m.text][0]
    run(store, f"forget {stamps.id}")
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
    out = run(store, f"approve {m.id}", show_private=False)
    assert "terminal" in out and store.get(m.id).status == "pending"
    assert "Saved" in run(store, f"approve {m.id}", show_private=True)
    assert not commands.shows_private("") and not commands.shows_private(None)


def test_platform_settings_decide_where_private_text_shows():
    from whos_listening.routes import RouteSettings
    s = RouteSettings.from_dict({"private_platforms": ["signal"], "shared_platforms": ["desktop"]})
    assert commands.shows_private("signal", s) and not commands.shows_private("desktop", s)
    assert commands.shows_private("cli", s) and not commands.shows_private("", s)


def test_add_on_a_shared_gateway(store):
    out = commands.run(store, "add Keeps bees", show_private=True, private_refused="No private memory was saved: x.")
    assert "No private memory was saved" in out and "add shared" in out and store.count() == 0
    assert "Saved" in commands.run(store, "add shared Likes jazz", show_private=True, private_refused="x")
