"""Shared-audience consent on a Private-capable gateway. All facts are made up."""

import json

import pytest

from test_sync import FakeFile
from whos_listening import cli, commands, order, sync, tools
from whos_listening.store import ACTIVE, FORGOTTEN, PENDING, PRIVATE, SHARED, Provenance, SQLiteStore, StoreError


ASSISTANT = Provenance(written_by="assistant", via=tools.REMEMBER)
USER = Provenance(written_by="user", via="test")
FACT = "Owns a blue mountain bike"


def propose(store, route=PRIVATE, **kwargs):
    return json.loads(tools.handle(store, tools.REMEMBER, {"text": FACT, "tier": SHARED},
                                   route_tier=route, provenance=lambda: ASSISTANT, **kwargs))


@pytest.mark.parametrize("route", (PRIVATE, SHARED))
@pytest.mark.parametrize("prior", ("none", "private", "tombstone"))
def test_proposal_is_uniform_and_hidden_before_private_checks(tmp_path, route, prior):
    store = SQLiteStore(tmp_path / f"{route}-{prior}.sqlite")
    if prior != "none":
        old = store.add(FACT, PRIVATE, USER)
        if prior == "tombstone":
            store.forget(old.id)
    judged = []
    out = propose(store, route, judge=lambda *_: judged.append(True), tier_duplicates="private",
                  outside_tool="read_file")
    assert out == {"ok": True, "pending": {"tier": SHARED},
                   "note": "Sharing waits for your direct choice on a Private-capable platform: /memories share."}
    assert not judged
    proposals = store.consent_proposals()
    assert len(proposals) == 1 and proposals[0].status == PENDING
    assert store.list(tiers=(SHARED,), statuses=(ACTIVE, PENDING, FORGOTTEN)) == []
    assert store.count(tiers=(SHARED,), statuses=(ACTIVE, PENDING, FORGOTTEN)) == 0
    assert store.search("blue", tiers=(SHARED,)) == []
    assert store.shared_for_sync() == []
    assert commands.cmd_pending(store, False) == "Nothing is waiting for you."
    assert json.loads(tools.handle(store, tools.REVIEW, {"action": "list"}, route_tier=SHARED,
                                   provenance=lambda: ASSISTANT))["waiting"] == []
    store.close()


def test_only_direct_local_share_choice_activates_a_proposal(store):
    old = store.add(FACT, PRIVATE, USER)
    out = propose(store)
    proposal = store.consent_proposals()[0]
    assert "id" not in json.dumps(out)
    token = f"P{proposal.id}@{proposal.order_key}"
    with pytest.raises(StoreError, match="No memory"):
        commands.cmd_approve(store, str(proposal.id), True)
    with pytest.raises(StoreError):
        store.set_status(proposal.id, ACTIVE)
    with pytest.raises(StoreError):
        store.forget(proposal.id)
    assert not store.apply_review(proposal.id, store.last_event_id(proposal.id), proposal.order_key, True, "")
    preview = commands.cmd_share(store, "")
    assert FACT in preview and old.text in preview and "Shared: any model may see it" in preview
    assert "keep-both" in preview and "<wl-private id=" in preview
    assert "Possible Private match" in commands.cmd_share(store, "")
    assert "Private memory may say" in commands.cmd_share(store, f"approve {token}")
    assert store.get(proposal.id).status == PENDING
    assert "Approved Shared" in commands.cmd_share(store, f"keep-both {token}")
    assert store.get(proposal.id).status == ACTIVE and not store.get(proposal.id).audience_consent_required
    assert store.get(old.id).status == ACTIVE
    assert [m.id for m in store.shared_for_sync()] == [proposal.id]


def test_guessed_proposal_number_answers_like_missing_one(store):
    propose(store)
    p = store.consent_proposals()[0]
    for action in ("approve", "reject", "keep", "keep_both"):
        def answer(memory_id):
            return json.loads(tools.handle(store, tools.REVIEW, {"action": action, "id": memory_id},
                                           route_tier=SHARED, provenance=lambda: ASSISTANT,
                                           user_message=f"{action} {memory_id}"))
        missing = answer(p.id + 1000)
        assert answer(p.id)["error"].replace(str(p.id), "N") == missing["error"].replace(str(p.id + 1000), "N")
    def forget(memory_id):
        return json.loads(tools.handle(store, tools.FORGET, {"id": memory_id},
                                       route_tier=SHARED, provenance=lambda: ASSISTANT))
    assert forget(p.id)["error"].replace(str(p.id), "N") == \
        forget(p.id + 1000)["error"].replace(str(p.id + 1000), "N")


def test_decline_has_no_tombstone_or_sync_record(store):
    propose(store)
    p = store.consent_proposals()[0]
    assert "Declined" in commands.cmd_share(store, f"reject P{p.id}@{p.order_key}")
    assert store.get(p.id).status == FORGOTTEN and store.get(p.id).audience_consent_required
    assert not store.is_forgotten_text(FACT) and store.shared_for_sync() == []
    with pytest.raises(StoreError):
        store.restore(p.id)
    assert "A declined sharing proposal has no tombstone" in cli.purge(store, str(p.id))
    assert store.get(p.id) is None and store.shared_for_sync() == []
    assert not store.is_forgotten_text(FACT)


def test_proposal_is_absent_from_actual_sync_until_owner_approves(store):
    file = FakeFile()
    propose(store)
    p = store.consent_proposals()[0]
    sync.sync_once(store, file, "local")
    assert FACT not in (file.raw or b"").decode()
    assert "Approved Shared" in commands.cmd_share(store, f"approve P{p.id}@{p.order_key}")
    sync.sync_once(store, file, "local")
    assert FACT in file.raw.decode()


def test_undo_direct_share_approval_withdraws_uid_and_requires_fresh_choice(tmp_path):
    a, b, file = SQLiteStore(tmp_path / "owner.sqlite"), SQLiteStore(tmp_path / "peer.sqlite"), FakeFile()
    try:
        private = a.add(FACT, PRIVATE, USER)
        propose(a)
        p = a.consent_proposals()[0]
        old_token = f"P{p.id}@{p.order_key}"
        assert "Approved Shared" in commands.cmd_share(a, f"keep-both {old_token}")
        approved = a.get(p.id)
        sync.sync_once(a, file, "owner")
        sync.sync_once(b, file, "peer")
        same_words = b.add(FACT, SHARED, USER)
        assert "Undid sharing approval" in commands.cmd_undo(a, True)
        waiting = a.get(p.id)
        assert waiting.status == PENDING and waiting.audience_consent_required
        assert "sharing proposal is waiting" in commands.cmd_undo(a, True)
        assert int(waiting.order_key) > int(approved.order_key)
        assert a.get(private.id).status == ACTIVE
        assert a.withdrawal(p.uid) is not None
        assert a.shared_for_sync() == []
        assert "changed" in commands.cmd_share(a, f"approve {old_token}")
        with pytest.raises(StoreError, match="No memory"):
            commands.cmd_approve(a, str(p.id), True)
        assert not a.apply_review(p.id, a.last_event_id(p.id), waiting.order_key, True, "")
        sync.sync_once(a, file, "owner")
        assert file.doc()["memories"][p.uid]["status"] == sync.WITHDRAWN
        assert FACT not in file.raw.decode()
        sync.sync_once(b, file, "peer")
        assert b.by_uid(p.uid).status == FORGOTTEN
        assert b.get(same_words.id).status == ACTIVE
        assert b.tombstone(sync.fingerprint(FACT)) is None
        fresh_token = f"P{p.id}@{waiting.order_key}"
        assert "Approved Shared" in commands.cmd_share(a, f"keep-both {fresh_token}")
        assert a.get(p.id).status == ACTIVE and not a.get(p.id).audience_consent_required
        sync.sync_once(a, file, "owner")
        assert file.doc()["memories"][p.uid]["status"] == ACTIVE
    finally:
        a.close()
        b.close()


def test_pending_or_declined_share_choice_does_not_undo_older_action(store):
    old = store.add("Likes jazz", PRIVATE, USER)
    store.log_event(old.id, "saved", "user")
    propose(store)
    p = store.consent_proposals()[0]
    assert "sharing proposal is waiting" in commands.cmd_undo(store, True)
    assert store.get(old.id).status == ACTIVE
    assert "Declined" in commands.cmd_share(store, f"reject P{p.id}@{p.order_key}")
    assert "proposal was declined" in commands.cmd_undo(store, True)
    assert store.get(old.id).status == ACTIVE
    assert "forgot" in commands.cmd_undo(store, True).lower()
    assert store.get(old.id).status == FORGOTTEN


def test_already_withdrawn_share_approval_does_not_undo_older_action(store):
    old = store.add("Likes jazz", PRIVATE, USER)
    store.log_event(old.id, "saved", "user")
    propose(store)
    p = store.consent_proposals()[0]
    assert "Approved Shared" in commands.cmd_share(store, f"approve P{p.id}@{p.order_key}")
    store.withdraw_approval(p.id, require_sharing_choice=True)
    assert "sharing proposal is waiting" in commands.cmd_undo(store, True)
    assert store.get(old.id).status == ACTIVE


def test_remote_forget_overtakes_unmarked_share_approval(store):
    propose(store)
    p = store.consent_proposals()[0]
    assert "Approved Shared" in commands.cmd_share(store, f"approve P{p.id}@{p.order_key}")
    store.withdraw_approval(p.id, require_sharing_choice=True)
    withdrawal = store.withdrawal(p.uid)
    forgotten = {"status": FORGOTTEN, "fingerprint": sync.fingerprint(FACT),
                 "updated_at": "2026-01-01T00:00:00+00:00", "order": withdrawal.order_key}
    file = FakeFile()
    file.raw = (json.dumps({"format": sync.FORMAT, "version": 2,
                            "memories": {p.uid: forgotten}}) + "\n").encode()
    sync.sync_once(store, file, "peer")
    assert "no longer waiting" in commands.cmd_undo(store, True)
    assert "overtaken by a change from another gateway" in commands.cmd_undo(store, True)


def test_terminal_share_undo_can_still_be_declined_and_purged(store):
    propose(store)
    p = store.consent_proposals()[0]
    near_end = str(order.MAX_VALUE - 1)
    store._db.execute("UPDATE memories SET order_key = ? WHERE id = ?", (near_end, p.id))
    store._db.commit()
    assert "Approved Shared" in commands.cmd_share(store, f"approve P{p.id}@{near_end}")
    assert store.get(p.id).order_key == str(order.MAX_VALUE)
    assert "Undid sharing approval" in commands.cmd_undo(store, True)
    waiting = store.get(p.id)
    assert waiting.status == PENDING and waiting.audience_consent_required
    assert waiting.order_key == str(order.MAX_VALUE)
    assert "Declined" in commands.cmd_share(store, f"reject P{p.id}@{waiting.order_key}")
    assert store.get(p.id).status == FORGOTTEN
    assert not store.is_forgotten_text(FACT)
    assert "A declined sharing proposal has no tombstone" in cli.purge(store, str(p.id))
    assert store.get(p.id) is None


def test_remote_forget_and_later_active_leave_undone_share_hidden(store):
    older = store.add("Likes jazz", PRIVATE, USER)
    store.log_event(older.id, "saved", "user")
    propose(store)
    p = store.consent_proposals()[0]
    assert "Approved Shared" in commands.cmd_share(store, f"approve P{p.id}@{p.order_key}")
    assert "Undid sharing approval" in commands.cmd_undo(store, True)
    withdrawal = store.withdrawal(p.uid)
    file = FakeFile()
    stamp = "2026-01-01T00:00:00+00:00"
    forgotten = {"status": FORGOTTEN, "fingerprint": sync.fingerprint(FACT),
                 "updated_at": stamp, "order": withdrawal.order_key}
    file.raw = (json.dumps({"format": sync.FORMAT, "version": 2,
                            "memories": {p.uid: forgotten}}) + "\n").encode()
    sync.sync_once(store, file, "peer")
    hidden = store.get(p.id)
    assert hidden.status == FORGOTTEN and hidden.audience_consent_required
    assert store.tombstone(sync.fingerprint(FACT))[2] is True
    assert "no longer waiting" in commands.cmd_undo(store, True)
    assert store.get(older.id).status == ACTIVE
    newer = order.next_key(withdrawal.order_key, stamp)
    active = {"status": ACTIVE, "text": FACT, "fingerprint": sync.fingerprint(FACT),
              "updated_at": stamp, "order": newer}
    file.raw = (json.dumps({"format": sync.FORMAT, "version": 2,
                            "memories": {p.uid: active}}) + "\n").encode()
    sync.sync_once(store, file, "peer")
    waiting = store.get(p.id)
    assert waiting.status == PENDING and waiting.audience_consent_required
    assert store.consent_proposals()[0].id == p.id
    assert store.shared_for_sync() == []
    assert any(e.action == "share_held" and e.actor == "sync" for e in store.events(memory_id=p.id))
    assert "sharing proposal is waiting" in commands.cmd_undo(store, True)
    assert store.get(older.id).status == ACTIVE
    with pytest.raises(StoreError, match="No memory"):
        commands.cmd_approve(store, str(p.id), True)


def test_declined_renewed_share_stays_declined_after_newer_peer_active(store):
    propose(store)
    p = store.consent_proposals()[0]
    assert "Approved Shared" in commands.cmd_share(store, f"approve P{p.id}@{p.order_key}")
    assert "Undid sharing approval" in commands.cmd_undo(store, True)
    waiting = store.get(p.id)
    assert "Declined" in commands.cmd_share(store, f"reject P{p.id}@{waiting.order_key}")
    declined = store.get(p.id)
    assert declined.status == FORGOTTEN and declined.forgotten_from == PENDING
    newer = order.next_key(declined.order_key, "2026-01-01T00:00:00+00:00")
    active = {"status": ACTIVE, "text": FACT, "fingerprint": sync.fingerprint(FACT),
              "updated_at": "2026-01-01T00:00:00+00:00", "order": newer}
    file = FakeFile()
    file.raw = (json.dumps({"format": sync.FORMAT, "version": 2,
                            "memories": {p.uid: active}}) + "\n").encode()
    sync.sync_once(store, file, "peer")
    assert store.get(p.id).status == FORGOTTEN and store.get(p.id).audience_consent_required
    assert store.consent_proposals() == []
    assert store.shared_for_sync() == []
    assert "A declined sharing proposal has no tombstone" in cli.purge(store, str(p.id))


def test_second_connection_cannot_decide_a_stale_proposal(tmp_path):
    path = tmp_path / "consent.sqlite"
    first = SQLiteStore(path)
    second = SQLiteStore(path)
    try:
        propose(first)
        p = first.consent_proposals()[0]
        second.decide_shared_proposal(p.id, p.order_key, approve=False)
        with pytest.raises(StoreError, match="changed"):
            first.decide_shared_proposal(p.id, p.order_key, approve=True)
        assert first.shared_for_sync() == []
    finally:
        first.close()
        second.close()


def test_private_tombstone_needs_explicit_owner_restore_or_add(store):
    old = store.add(FACT, PRIVATE, USER)
    store.forget(old.id)
    propose(store)
    p = store.consent_proposals()[0]
    assert "forgotten" in commands.cmd_share(store, f"approve P{p.id}@{p.order_key}")
    assert store.get(p.id).status == PENDING
    assert store.is_forgotten_text(FACT)


def test_shared_only_gateway_still_saves_automatically(store):
    store.add(FACT, PRIVATE, USER)
    out = propose(store, SHARED, shared_only=True, tier_duplicates="private")
    assert out["ok"] and store.consent_proposals() == []


def test_shared_route_private_hold_reply_does_not_reveal_duplicate(tmp_path):
    responses = []
    for name, prior in (("known", True), ("new", False)):
        s = SQLiteStore(tmp_path / f"{name}.sqlite")
        if prior:
            s.add(FACT, PRIVATE, USER)
        responses.append(json.loads(tools.handle(s, tools.REMEMBER, {"text": FACT}, route_tier=SHARED,
                                                provenance=lambda: ASSISTANT, outside_tool="read_file",
                                                on_outside_hold=lambda _: True)))
        s.close()
    assert responses[0] == responses[1]


def test_legacy_pending_shared_assistant_save_is_migrated(tmp_path):
    path = tmp_path / "legacy.sqlite"
    store = SQLiteStore(path)
    old = store.add(FACT, SHARED, ASSISTANT, status=PENDING, pending_reason="Outside content.")
    since, stamp = store.last_event_id(old.id), old.updated_at
    store._db.execute("PRAGMA user_version = 5")
    store.close()
    migrated = SQLiteStore(path)
    p = migrated.get(old.id)
    assert p.audience_consent_required and p.updated_at == stamp
    assert not migrated.apply_review(old.id, since, old.order_key, True, "")
    assert migrated.list(statuses=(PENDING,)) == []
    migrated.close()


def test_max_timestamp_legacy_pending_proposal_migrates_without_advancing_date(tmp_path):
    path = tmp_path / "max-date.sqlite"
    old = SQLiteStore(path)
    m = old.add(FACT, SHARED, ASSISTANT, status=PENDING, pending_reason="Outside content.")
    old._db.execute("UPDATE memories SET updated_at = ? WHERE id = ?",
                    ("9999-12-31T23:59:59+00:00", m.id))
    old._db.commit()
    since = old.last_event_id(m.id)
    old._db.execute("PRAGMA user_version = 5")
    old.close()
    migrated = SQLiteStore(path)
    try:
        assert migrated.get(m.id).updated_at == "9999-12-31T23:59:59+00:00"
        assert migrated.get(m.id).audience_consent_required
        assert not migrated.apply_review(m.id, since, migrated.get(m.id).order_key, True, "")
        assert migrated.get(m.id).status == PENDING
    finally:
        migrated.close()


def test_shared_route_private_reply_is_uniform_with_visible_and_hidden_duplicate(tmp_path):
    responses = []
    for name, with_private in (("visible", False), ("hidden", True)):
        s = SQLiteStore(tmp_path / f"{name}.sqlite")
        s.add(FACT, SHARED, USER)
        if with_private:
            s.add(FACT, PRIVATE, USER)
        responses.append(json.loads(tools.handle(s, tools.REMEMBER, {"text": FACT}, route_tier=SHARED,
                                                provenance=lambda: ASSISTANT, outside_tool="read_file")))
        s.close()
    assert responses[0] == responses[1]
