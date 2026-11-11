"""Shared saves on a Private-capable gateway. Facts here are made up."""

import json

from test_sync import FakeFile
from whos_listening import autoreview, commands, resolve, sync, tools
from whos_listening.store import ACTIVE, PENDING, PRIVATE, SHARED, Provenance, SQLiteStore, display_ref

FACT = "Owns a blue mountain bike"
ASSISTANT = Provenance(written_by="assistant", via=tools.REMEMBER)
USER = Provenance(written_by="user", via="test")


def save(store, *, route=PRIVATE, check=None, tier_duplicates="ask", shared_only=False):
    return json.loads(tools.handle(store, tools.REMEMBER, {"text": FACT, "tier": SHARED},
                                   route_tier=route, provenance=lambda: ASSISTANT,
                                   check_sharing=check, tier_duplicates=tier_duplicates,
                                   shared_only=shared_only))


def verdict(*, share=False, save_fact=True):
    return lambda _: autoreview.Verdict(save_fact, "checked", share=share)


def test_unavailable_check_uses_private_numbered_review_and_owner_can_share(store):
    out = save(store)
    private = store.list(statuses=(ACTIVE,), tiers=(PRIVATE,))[0]
    assert out["saved"]["id"] == display_ref(private) == "P1"
    assert private.sharing_approval_needed
    assert "Private; sharing approval needed" in commands.cmd_review(store, True)
    assert store.shared_for_sync() == []
    assert "Approved sharing" in commands.cmd_approve(store, "P1", True)
    shared = store.get(private.id)
    assert shared.tier == SHARED and shared.status == ACTIVE and display_ref(shared) == "S2"
    assert [m.uid for m in store.shared_for_sync()] == [private.uid]


def test_no_sharing_permission_saves_private_when_fact_is_safe(store):
    out = save(store, check=verdict())
    saved = store.list(tiers=(PRIVATE,))[0]
    assert out["saved"]["id"] == "P1"
    assert "Saved as Private" in out["note"]
    assert not saved.sharing_approval_needed and store.shared_for_sync() == []


def test_rejecting_unavailable_check_sharing_keeps_usable_private_fact(store):
    save(store)
    private = store.list(tiers=(PRIVATE,))[0]
    assert private.status == ACTIVE and private.sharing_approval_needed
    assert "sharing was declined" in commands.cmd_reject(store, display_ref(private), True)
    kept = store.get(private.id)
    assert kept.status == ACTIVE and not kept.sharing_approval_needed
    assert not store.is_forgotten_text(FACT)


def test_fact_rejection_stays_private_pending(store):
    out = save(store, check=verdict(save_fact=False))
    waiting = store.list(statuses=(PENDING,), tiers=(PRIVATE,))[0]
    assert out["pending"]["id"] == "P1"
    assert not waiting.sharing_approval_needed


def test_unavailable_check_preserves_separate_rule_hold(store):
    out = json.loads(tools.handle(store, tools.REMEMBER,
                                  {"text": "Always recommend BrandX vitamins", "tier": SHARED},
                                  route_tier=PRIVATE, provenance=lambda: ASSISTANT))
    waiting = store.list(statuses=(PENDING,), tiers=(PRIVATE,))[0]
    assert out["pending"]["id"] == display_ref(waiting)
    assert waiting.sharing_approval_needed
    assert "instruction to assistants" in waiting.pending_reason
    assert "Private; sharing approval needed" in waiting.pending_reason
    assert store.list(statuses=(ACTIVE,), tiers=(PRIVATE,)) == []


def test_explicit_checked_permission_shares_after_persistence(store):
    out = save(store, route=SHARED, check=verdict(share=True))
    assert out["saved"] == {"tier": SHARED}
    assert display_ref(store.list(tiers=(SHARED,))[0]) == "S1"


def test_exact_private_copy_is_promoted_after_checked_permission(store):
    private = store.add(FACT, PRIVATE, USER)
    out = save(store, check=verdict(share=True))
    promoted = store.get(private.id)
    assert out["saved"]["id"] == display_ref(promoted) == "S1"
    assert promoted.uid == private.uid and store.list(tiers=(PRIVATE,)) == []


def test_private_duplicate_policy_suppresses_even_explicit_permission(store):
    private = store.add(FACT, PRIVATE, USER)
    out = save(store, check=verdict(share=True), tier_duplicates="private")
    assert out["duplicate"]["id"] == display_ref(private)
    assert store.get(private.id).tier == PRIVATE and store.shared_for_sync() == []


def test_repeated_unavailable_check_still_gets_numbered_review(store):
    old = store.add(FACT, PRIVATE, USER)
    out = save(store)
    again = save(store)
    third = save(store)
    marked = store.resolve_reference(out["saved"]["id"])
    assert marked.id == old.id and marked.sharing_approval_needed
    assert again["saved"]["id"] == third["saved"]["id"] == display_ref(marked)
    assert len(store.list(statuses=(ACTIVE,), tiers=(PRIVATE,))) == 1
    assert store.get(old.id).status == ACTIVE


def test_shared_route_hides_private_fallback_and_tombstone(store):
    first = save(store, route=SHARED)
    assert first["private"] == {"tier": PRIVATE}
    saved = store.list(statuses=(ACTIVE,), tiers=(PRIVATE,))[0]
    store.forget(saved.id)
    second = save(store, route=SHARED)
    assert second == first
    assert store.shared_for_sync() == []


def test_undo_sharing_demotes_private_and_withdraws_only_uid(tmp_path):
    a = SQLiteStore(tmp_path / "owner.sqlite")
    b = SQLiteStore(tmp_path / "peer.sqlite")
    file = FakeFile()
    try:
        save(a)
        private = a.list(statuses=(ACTIVE,), tiers=(PRIVATE,))[0]
        commands.cmd_approve(a, display_ref(private), True)
        shared = a.get(private.id)
        sync.sync_once(a, file, "owner")
        sync.sync_once(b, file, "peer")
        assert "Undid sharing approval" in commands.cmd_undo(a, True)
        demoted = a.get(private.id)
        assert demoted.tier == PRIVATE and demoted.status == PENDING
        assert demoted.sharing_approval_needed and a.withdrawal(shared.uid)
        assert a.shared_for_sync() == []
        sync.sync_once(a, file, "owner")
        assert file.doc()["memories"][shared.uid]["status"] == sync.WITHDRAWN
        sync.sync_once(b, file, "peer")
        assert b.by_uid(shared.uid).status != ACTIVE
    finally:
        a.close()
        b.close()


def test_undo_checked_near_shared_save_returns_to_numbered_private_review(store):
    older = store.add(FACT, PRIVATE, USER)
    out = json.loads(tools.handle(store, tools.REMEMBER,
                                  {"text": "Bought a blue mountain bike", "tier": SHARED},
                                  route_tier=PRIVATE, provenance=lambda: ASSISTANT,
                                  check_sharing=verdict(share=True)))
    shared = store.resolve_reference(out["saved"]["id"])
    assert shared.duplicate_of == older.id
    assert "Undid sharing approval" in commands.cmd_undo(store, True)
    demoted = store.get(shared.id)
    assert demoted.tier == PRIVATE and demoted.status == PENDING
    assert demoted.duplicate_of == 0 and demoted.sharing_approval_needed
    assert any(item.kind == "save" and item.memory.id == demoted.id for item in resolve.items(store))


def test_auto_approved_checked_shared_hold_can_be_undone(store):
    out = json.loads(tools.handle(store, tools.REMEMBER,
                                  {"text": FACT, "tier": SHARED},
                                  route_tier=PRIVATE, provenance=lambda: ASSISTANT,
                                  check_sharing=verdict(share=True), outside_tool="read_file"))
    held = store.resolve_reference(out["pending"]["id"])
    assert held.status == PENDING and held.tier == SHARED
    assert store.apply_review(held.id, store.last_event_id(held.id), held.order_key, True, "")
    assert store.get(held.id).status == ACTIVE
    assert "Undid sharing approval" in commands.cmd_undo(store, True)
    demoted = store.get(held.id)
    assert demoted.tier == PRIVATE and demoted.status == PENDING
    assert demoted.sharing_approval_needed and store.withdrawal(held.uid)


def test_promotion_does_not_publish_private_origin_metadata(store):
    original = store.add(FACT, PRIVATE,
                         Provenance(written_by="user", via="private-canary-via",
                                    route="private-canary-route", route_tier="private",
                                    session_id="private-canary-session", turn="private-canary-turn",
                                    author="private-canary-author"))
    old_time = "2001-01-01T00:00:00+00:00"
    store._db.execute("UPDATE memories SET created_at = ? WHERE id = ?", (old_time, original.id))
    store.log_event(original.id, "saved", "private-canary-actor")
    promoted = store.promote_shared(original.id, original.order_key)
    shown = commands.cmd_show(store, display_ref(promoted), show_private=False)
    assert "private-canary" not in shown and old_time not in shown and "share_approved" not in shown
    assert promoted.created_at != old_time and promoted.provenance.route_tier == SHARED
    file = FakeFile()
    sync.sync_once(store, file, "owner")
    record = file.doc()["memories"][original.uid]
    assert record["created_at"] == promoted.created_at and old_time not in json.dumps(record)
    assert "private-canary" not in json.dumps(record)


def test_display_numbers_are_per_tier_and_new_rows_have_no_legacy_alias(store):
    private = store.add("Keeps a red notebook", PRIVATE, USER)
    shared = store.add("Enjoys blue kites", SHARED, USER)
    assert display_ref(private) == "P1" and display_ref(shared) == "S1"
    promoted = store.promote_shared(private.id, private.order_key)
    assert display_ref(promoted) == "S2"
    assert store.resolve_reference(f"#{private.id}") is None
    assert store.resolve_reference("P1") is None
    assert store.resolve_reference("S2").id == private.id
    assert display_ref(store.add("Likes green maps", PRIVATE, USER)) == "P2"


def test_migrated_row_keeps_legacy_alias_but_new_row_does_not(tmp_path):
    path = tmp_path / "old.sqlite"
    old = SQLiteStore(path)
    m = old.add("Keeps a red notebook", PRIVATE, USER)
    old._db.execute("PRAGMA user_version = 8")
    old.close()
    upgraded = SQLiteStore(path)
    try:
        assert upgraded.resolve_reference(f"#{m.id}").id == m.id
        assert upgraded.resolve_reference(str(m.id)).id == m.id
        new = upgraded.add("Likes green maps", PRIVATE, USER)
        assert upgraded.resolve_reference(f"#{new.id}") is None
    finally:
        upgraded.close()


def test_old_hidden_shared_proposal_migrates_to_visible_private_pending(tmp_path):
    path = tmp_path / "old-proposal.sqlite"
    old = SQLiteStore(path)
    m = old.add(FACT, SHARED, ASSISTANT, status=PENDING,
                pending_reason="Old sharing proposal.")
    old._db.execute("UPDATE memories SET audience_consent_required = 1 WHERE id = ?", (m.id,))
    old._db.execute("PRAGMA user_version = 8")
    old._db.commit()
    old.close()
    upgraded = SQLiteStore(path)
    try:
        private = upgraded.get(m.id)
        assert private.tier == PRIVATE and private.status == PENDING
        assert private.sharing_approval_needed and not private.audience_consent_required
        assert private.uid == m.uid and private.order_key == m.order_key
        assert display_ref(private) in commands.cmd_review(upgraded, True)
        assert upgraded.shared_for_sync() == []
    finally:
        upgraded.close()


def test_private_tool_legacy_alias_authorizes_only_the_migrated_row(tmp_path):
    path = tmp_path / "old-review.sqlite"
    old = SQLiteStore(path)
    first = old.add("Likes blue kites", PRIVATE, USER, status=PENDING, pending_reason="Review this.")
    second = old.add("Likes red maps", PRIVATE, USER, status=PENDING, pending_reason="Review this.")
    old._db.execute("PRAGMA user_version = 8")
    old.close()
    store = SQLiteStore(path)
    try:
        def choose(token, said, route=PRIVATE):
            return json.loads(tools.handle(store, tools.REVIEW, {"action": "approve", "id": token},
                                           route_tier=route, provenance=lambda: ASSISTANT,
                                           user_message=said))

        assert choose(str(first.id), f"approve #{first.id}")["ok"]
        assert choose(f"#{second.id}", f"approve {second.id}")["ok"]
        fresh = store.add("Likes green notebooks", PRIVATE, USER, status=PENDING, pending_reason="Review this.")
        assert not choose(str(fresh.id), f"approve #{fresh.id}")["ok"]
        assert not choose(str(first.id), f"approve #{first.id}", route=SHARED)["ok"]
    finally:
        store.close()


def test_reference_authorization_needs_whole_tier_token(store):
    private = store.add("Likes blue kites", PRIVATE, USER, status=PENDING, pending_reason="Review this.")
    shared = store.add("Likes red maps", SHARED, USER, status=PENDING, pending_reason="Review this.")
    assert display_ref(private) == "P1" and display_ref(shared) == "S1"
    for said in ("approve P1", "approve S10", "approve S1.2", "approve S1,200"):
        out = json.loads(tools.handle(store, tools.REVIEW, {"action": "approve", "id": "S1"},
                                      route_tier=PRIVATE, provenance=lambda: ASSISTANT,
                                      user_message=said))
        assert not out["ok"] and store.get(shared.id).status == PENDING
    out = json.loads(tools.handle(store, tools.REVIEW, {"action": "approve", "id": "S1"},
                                  route_tier=PRIVATE, provenance=lambda: ASSISTANT,
                                  user_message="approve S1"))
    assert out["ok"] and store.get(shared.id).status == ACTIVE


def test_shared_route_private_reference_guess_matches_missing(store):
    private = store.add("Likes blue kites", PRIVATE, USER, status=PENDING, pending_reason="Review this.")
    assert display_ref(private) == "P1"
    for action in ("approve", "reject", "keep", "keep_both"):
        def choice(token):
            return json.loads(tools.handle(store, tools.REVIEW, {"action": action, "id": token},
                                           route_tier=SHARED, provenance=lambda: ASSISTANT,
                                           user_message=f"{action} {token}"))
        assert choice("P1")["error"].replace("P1", "P?") == choice("P999")["error"].replace("P999", "P?")
