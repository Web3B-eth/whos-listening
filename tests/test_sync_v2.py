"""Version-two sync order and UID-only withdrawal, using made-up facts and a fake file."""

import json

import pytest

from test_sync import FakeFile
from whos_listening import commands, order, sync
from whos_listening.store import ACTIVE, FORGOTTEN, PENDING, PRIVATE, SHARED, Provenance, SQLiteStore, fingerprint


USER = Provenance(written_by="user", via="test")
FACT = "Owns a blue mountain bike"


def test_v1_fractional_offsets_and_ties_keep_existing_lww_rule():
    active = sync._record("a" * 32, {"status": ACTIVE, "text": FACT,
                                     "updated_at": "2026-01-01T00:00:00.123456+00:00"}, 1)
    same = sync._record("a" * 32, {"status": ACTIVE, "text": FACT,
                                   "updated_at": "2025-12-31T19:00:00.123456-05:00"}, 1)
    later = sync._record("a" * 32, {"status": ACTIVE, "text": FACT,
                                    "updated_at": "2026-01-01T00:00:00.123457+00:00"}, 1)
    forgotten = sync._record("a" * 32, {"status": FORGOTTEN, "fingerprint": fingerprint(FACT),
                                        "updated_at": "2026-01-01T00:00:00.123456+00:00"}, 1)
    assert active.order_key == same.order_key
    assert sync.newer(later, active)
    assert sync.newer(forgotten, active) and not sync.newer(active, forgotten)


def test_undo_approved_shared_withdraws_one_uid_and_reapproval_wins(tmp_path):
    a, b, file = SQLiteStore(tmp_path / "a.sqlite"), SQLiteStore(tmp_path / "b.sqlite"), FakeFile()
    try:
        m = a.add(FACT, SHARED, USER, status=PENDING, pending_reason="Review first.")
        a.log_event(m.id, "held", "assistant")
        assert "Saved" in commands.cmd_approve(a, str(m.id), True)
        sync.sync_once(a, file, "a")
        sync.sync_once(b, file, "b")
        other = b.add(FACT, SHARED, USER)
        private = b.add(FACT, PRIVATE, USER)
        assert "waiting for you again" in commands.cmd_undo(a, True)
        assert a.get(m.id).status == PENDING
        first_order = m.order_key
        withdrawal = a.withdrawal(m.uid)
        assert withdrawal and int(withdrawal.order_key) > int(first_order)
        sync.sync_once(a, file, "a")
        wire = file.doc()["memories"][m.uid]
        assert wire["status"] == sync.WITHDRAWN and "text" not in wire
        assert wire["fingerprint"] == fingerprint(FACT)
        assert FACT not in file.raw.decode()
        result = sync.sync_once(b, file, "b")
        assert result.withdrawn == 1
        assert b.by_uid(m.uid).status == FORGOTTEN and b.tombstone(fingerprint(FACT)) is None
        assert b.get(other.id).status == ACTIVE and b.get(private.id).status == ACTIVE
        sync.sync_once(b, file, "b")
        assert file.doc()["memories"][m.uid]["status"] == sync.WITHDRAWN
        assert "Saved" in commands.cmd_approve(a, str(m.id), True)
        assert int(a.get(m.id).order_key) > int(withdrawal.order_key)
        sync.sync_once(a, file, "a")
        assert file.doc()["memories"][m.uid]["status"] == ACTIVE
        sync.sync_once(b, file, "b")
        assert b.by_uid(m.uid).status == ACTIVE
    finally:
        a.close()
        b.close()


def test_max_legacy_active_forget_beats_stale_peer_after_restart(tmp_path):
    a, b, file = SQLiteStore(tmp_path / "a.sqlite"), SQLiteStore(tmp_path / "b.sqlite"), FakeFile()
    try:
        uid = "a" * 32
        old = {"format": sync.FORMAT, "version": 1, "memories": {uid: {
            "status": ACTIVE, "text": FACT, "fingerprint": fingerprint(FACT),
            "updated_at": "9999-12-31T23:59:59+00:00", "created_at": "2026-01-01T00:00:00+00:00", "origin": "a"}}}
        file.raw = (json.dumps(old) + "\n").encode()
        sync.sync_once(a, file, "a")
        sync.sync_once(b, file, "b")
        m = a.by_uid(uid)
        a.forget(m.id)
        assert int(a.get(m.id).order_key) > int(order.from_iso(old["memories"][uid]["updated_at"]))
        assert a.get(m.id).updated_at.startswith("2026-") or a.get(m.id).updated_at.startswith("2027-")
        a.close()
        a = SQLiteStore(tmp_path / "a.sqlite")
        sync.sync_once(a, file, "a")
        assert file.doc()["memories"][uid]["status"] == FORGOTTEN
        sync.sync_once(b, file, "b")
        assert b.by_uid(uid).status == FORGOTTEN
        # A stale v1 peer can re-emit its old operation, but it cannot outrank the v2 forget.
        file.raw = (json.dumps(old) + "\n").encode()
        sync.sync_once(b, file, "b")
        assert file.doc()["memories"][uid]["status"] == FORGOTTEN
    finally:
        a.close()
        b.close()


def test_invalid_v2_order_refuses_entire_file_without_mutation(tmp_path):
    s, file = SQLiteStore(tmp_path / "one.sqlite"), FakeFile()
    try:
        m = s.add("Likes jazz", SHARED, USER)
        uid, good_uid = "b" * 32, "c" * 32
        good = {"status": ACTIVE, "text": "Collects stamps", "fingerprint": fingerprint("Collects stamps"),
                "updated_at": "2026-01-01T00:00:00+00:00",
                "order": order.from_iso("2026-01-01T00:00:00+00:00")}
        raw = {"format": sync.FORMAT, "version": 2, "memories": {good_uid: good, uid: {
            "status": ACTIVE, "text": FACT, "fingerprint": fingerprint(FACT),
            "updated_at": "2026-01-01T00:00:00+00:00", "order": "9" * 65}}}
        file.raw = (json.dumps(raw) + "\n").encode()
        with pytest.raises(sync.SyncError, match="invalid order"):
            sync.sync_once(s, file, "one")
        assert s.get(m.id).status == ACTIVE and s.by_uid(uid) is None and s.by_uid(good_uid) is None
        assert file.doc() == raw
    finally:
        s.close()


def test_max_legacy_pending_approval_and_forget_restore(tmp_path):
    s, file = SQLiteStore(tmp_path / "pending.sqlite"), FakeFile()
    try:
        uid = "c" * 32
        fact = "Visits https://example.com for puzzles"
        old = {"format": sync.FORMAT, "version": 1, "memories": {uid: {
            "status": ACTIVE, "text": fact, "updated_at": "9999-12-31T23:59:59.123456+00:00",
            "created_at": "2026-01-01T00:00:00+00:00", "origin": "older"}}}
        file.raw = (json.dumps(old) + "\n").encode()
        assert sync.sync_once(s, file, "s").held == 1
        pending = s.by_uid(uid)
        assert pending.status == PENDING
        assert "Saved" in commands.cmd_approve(s, str(pending.id), True)
        assert int(s.by_uid(uid).order_key) > int(pending.order_key)
        sync.sync_once(s, file, "s")
        assert file.doc()["memories"][uid]["status"] == ACTIVE
        assert int(file.doc()["memories"][uid]["order"]) > int(pending.order_key)
        s.forget(pending.id)
        sync.sync_once(s, file, "s")
        assert file.doc()["memories"][uid]["status"] == FORGOTTEN
        s.restore(pending.id)
        sync.sync_once(s, file, "s")
        assert file.doc()["memories"][uid]["status"] == ACTIVE
    finally:
        s.close()


def test_max_private_tombstone_restore_and_conflict_retry(tmp_path):
    s, file = SQLiteStore(tmp_path / "private.sqlite"), FakeFile()
    try:
        private = s.add(FACT, PRIVATE, USER)
        s.forget(private.id, at="9999-12-31T23:59:59+00:00")
        assert int(s.tombstone_order(fingerprint(FACT))) == int(order.from_iso("9999-12-31T23:59:59+00:00"))
        s.restore(private.id)
        assert s.get(private.id).status == ACTIVE
        assert int(s.get(private.id).order_key) > int(order.from_iso("9999-12-31T23:59:59+00:00"))
        shared = s.add("Likes jazz", SHARED, USER)
        s.forget(shared.id, at="9999-12-31T23:59:59+00:00")
        file.conflicts = 1
        result = sync.sync_once(s, file, "s")
        assert result.wrote and file.conflicts == 0
        assert file.doc()["memories"][shared.uid]["status"] == FORGOTTEN
        s.restore(shared.id)
        sync.sync_once(s, file, "s")
        assert file.doc()["memories"][shared.uid]["status"] == ACTIVE
    finally:
        s.close()


def test_withdrawal_before_first_upload_and_restart(tmp_path):
    path, file = tmp_path / "owner.sqlite", FakeFile()
    s = SQLiteStore(path)
    try:
        m = s.add(FACT, SHARED, USER, status=PENDING, pending_reason="Review first.")
        s.log_event(m.id, "held", "assistant")
        commands.cmd_approve(s, str(m.id), True)
        commands.cmd_undo(s, True)
        assert s.get(m.id).status == PENDING
        s.close()
        s = SQLiteStore(path)
        file.conflicts = 1
        sync.sync_once(s, file, "owner")
        assert file.doc()["memories"][m.uid]["status"] == sync.WITHDRAWN
        assert FACT not in file.raw.decode()
        assert s.get(m.id).status == PENDING
    finally:
        s.close()


def test_unknown_uid_withdrawal_does_not_touch_same_text_and_rejects_bad_shape(tmp_path):
    s, file = SQLiteStore(tmp_path / "peer.sqlite"), FakeFile()
    try:
        same = s.add(FACT, SHARED, USER)
        uid = "d" * 32
        row = {"status": sync.WITHDRAWN, "fingerprint": fingerprint(FACT),
               "updated_at": "2026-01-01T00:00:00+00:00", "created_at": "2026-01-01T00:00:00+00:00",
               "order": order.from_iso("2026-01-01T00:00:00+00:00"), "origin": "owner"}
        doc = {"format": sync.FORMAT, "version": 2, "memories": {uid: row}}
        file.raw = (json.dumps(doc) + "\n").encode()
        sync.sync_once(s, file, "peer")
        assert s.get(same.id).status == ACTIVE and s.withdrawal(uid) is not None
        assert s.tombstone(fingerprint(FACT)) is None
        stale = {**row, "status": ACTIVE, "text": FACT,
                 "updated_at": "2025-01-01T00:00:00+00:00",
                 "order": order.from_iso("2025-01-01T00:00:00+00:00")}
        file.raw = (json.dumps({**doc, "memories": {uid: stale}}) + "\n").encode()
        sync.sync_once(s, file, "peer")
        assert file.doc()["memories"][uid]["status"] == sync.WITHDRAWN
        assert s.by_uid(uid) is None and s.get(same.id).status == ACTIVE
        bad = {**doc, "memories": {uid: {**row, "text": FACT}}}
        file.raw = (json.dumps(bad) + "\n").encode()
        with pytest.raises(sync.SyncError, match="invalid withdrawal"):
            sync.sync_once(s, file, "peer")
        assert s.get(same.id).status == ACTIVE
    finally:
        s.close()


def test_known_uid_withdrawal_for_different_words_rejects_without_mutation(tmp_path):
    s, file = SQLiteStore(tmp_path / "wrong-hash.sqlite"), FakeFile()
    try:
        m = s.add(FACT, SHARED, USER)
        row = {"status": sync.WITHDRAWN, "fingerprint": fingerprint("Likes tea"),
               "updated_at": "2030-01-01T00:00:00+00:00", "created_at": m.created_at,
               "order": order.from_iso("2030-01-01T00:00:00+00:00"), "origin": "other"}
        raw = {"format": sync.FORMAT, "version": 2, "memories": {m.uid: row}}
        file.raw = (json.dumps(raw) + "\n").encode()
        with pytest.raises(sync.SyncError, match="different words"):
            sync.sync_once(s, file, "s")
        assert s.get(m.id).status == ACTIVE and s.withdrawal(m.uid) is None
    finally:
        s.close()


def test_pending_row_approval_advances_beyond_newer_observed_head(tmp_path):
    s, file = SQLiteStore(tmp_path / "pending-head.sqlite"), FakeFile()
    try:
        uid = "e" * 32
        fact = "Visits https://example.com for puzzles"
        first = order.from_iso("2026-01-01T00:00:00+00:00")
        later = order.from_iso("9999-12-31T23:59:59+00:00")
        base = {"status": ACTIVE, "text": fact, "fingerprint": fingerprint(fact),
                "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
                "order": first, "origin": "other"}
        file.raw = (json.dumps({"format": sync.FORMAT, "version": 2, "memories": {uid: base}}) + "\n").encode()
        sync.sync_once(s, file, "s")
        pending = s.by_uid(uid)
        assert pending.status == PENDING
        higher = {**base, "order": later, "updated_at": "9999-12-31T23:59:59+00:00"}
        file.raw = (json.dumps({"format": sync.FORMAT, "version": 2, "memories": {uid: higher}}) + "\n").encode()
        sync.sync_once(s, file, "s")
        assert s.by_uid(uid).status == PENDING
        assert s.by_uid(uid).order_key == first
        commands.cmd_approve(s, str(pending.id), True)
        assert int(s.by_uid(uid).order_key) > int(later)
        sync.sync_once(s, file, "s")
        assert int(file.doc()["memories"][uid]["order"]) > int(later)
    finally:
        s.close()


def test_private_preference_global_forget_gets_successor_after_max_legacy_date(tmp_path):
    s, file = SQLiteStore(tmp_path / "private-preference.sqlite"), FakeFile()
    try:
        private = s.add(FACT, PRIVATE, USER)
        uid = "f" * 32
        maximum = "9999-12-31T23:59:59+00:00"
        old = {"format": sync.FORMAT, "version": 1, "memories": {uid: {
            "status": ACTIVE, "text": FACT, "updated_at": maximum, "created_at": maximum,
            "origin": "other"}}}
        file.raw = (json.dumps(old) + "\n").encode()
        near = sync.NearRepeats(find=lambda _: private, tier_duplicates="private")
        result = sync.sync_once(s, file, "s", near=near)
        wire = file.doc()["memories"][uid]
        assert result.removed == 1 and wire["status"] == FORGOTTEN and "text" not in wire
        assert int(wire["order"]) > int(order.from_iso(maximum))
        assert s.get(private.id).status == ACTIVE
    finally:
        s.close()


def test_direct_owner_resave_advances_beyond_max_global_tombstone(tmp_path):
    s, file = SQLiteStore(tmp_path / "owner-resave.sqlite"), FakeFile()
    try:
        old_uid = "1" * 32
        maximum = "9999-12-31T23:59:59+00:00"
        old = {"format": sync.FORMAT, "version": 1, "memories": {old_uid: {
            "status": FORGOTTEN, "fingerprint": fingerprint(FACT), "updated_at": maximum,
            "created_at": maximum, "origin": "other"}}}
        file.raw = (json.dumps(old) + "\n").encode()
        sync.sync_once(s, file, "s")
        assert s.tombstone_order(fingerprint(FACT)) == order.from_iso(maximum)
        newer = s.add(FACT, SHARED, USER, allow_forgotten=True)
        assert int(newer.order_key) > int(order.from_iso(maximum))
        sync.sync_once(s, file, "s")
        assert s.get(newer.id).status == ACTIVE
        assert file.doc()["memories"][newer.uid]["status"] == ACTIVE
    finally:
        s.close()


def test_withdrawn_pending_peer_stays_pending_and_local_rejection_keeps_marker(tmp_path):
    a, b, file = SQLiteStore(tmp_path / "owner-pending.sqlite"), SQLiteStore(tmp_path / "peer-pending.sqlite"), FakeFile()
    try:
        fact = "Visits https://example.com for puzzles"
        m = a.add(fact, SHARED, USER, status=PENDING, pending_reason="Review first.")
        a.log_event(m.id, "held", "assistant")
        commands.cmd_approve(a, str(m.id), True)
        sync.sync_once(a, file, "owner")
        sync.sync_once(b, file, "peer")
        assert b.by_uid(m.uid).status == PENDING
        commands.cmd_undo(a, True)
        sync.sync_once(a, file, "owner")
        sync.sync_once(b, file, "peer")
        assert b.by_uid(m.uid).status == PENDING
        assert b.withdrawal(m.uid) is not None
        commands.cmd_reject(a, str(m.id), True)
        sync.sync_once(a, file, "owner")
        assert file.doc()["memories"][m.uid]["status"] == sync.WITHDRAWN
        commands.cmd_undo(a, True)
        assert a.get(m.id).status == PENDING and a.withdrawal(m.uid) is not None
        commands.cmd_approve(a, str(m.id), True)
        sync.sync_once(a, file, "owner")
        sync.sync_once(b, file, "peer")
        assert file.doc()["memories"][m.uid]["status"] == ACTIVE
        assert b.by_uid(m.uid).status == PENDING
    finally:
        a.close()
        b.close()
