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


@pytest.mark.parametrize("damage", [
    {"uid": "bad uid"},
    {"entry": []},
    {"status": "pending"},
    {"updated_at": "not a date"},
    {"text": 123},
    {"text": "  Extra spaces  "},
    {"fingerprint": "0" * 64},
    {"forgotten": True, "fingerprint": "bad"},
    {"forgotten": True, "fingerprint": int("1" * 64)},
    {"forgotten": True, "text": FACT},
])
def test_malformed_v2_record_refuses_all_imports_and_preserves_file(tmp_path, damage):
    s, file = SQLiteStore(tmp_path / "strict.sqlite"), FakeFile()
    try:
        good_uid, bad_uid = "a" * 32, "b" * 32
        base = {"status": ACTIVE, "text": FACT, "fingerprint": fingerprint(FACT),
                "updated_at": "2026-01-01T00:00:00+00:00",
                "order": order.from_iso("2026-01-01T00:00:00+00:00")}
        bad = dict(base)
        uid = damage.get("uid", bad_uid)
        if damage.get("forgotten"):
            bad = {"status": FORGOTTEN, "fingerprint": fingerprint(FACT),
                   "updated_at": base["updated_at"], "order": base["order"]}
        bad.update({k: v for k, v in damage.items() if k not in ("uid", "forgotten", "entry")})
        raw = {"format": sync.FORMAT, "version": 2, "memories": {
            good_uid: {**base, "text": "Collects stamps", "fingerprint": fingerprint("Collects stamps")},
            uid: damage.get("entry", bad)}}
        file.raw = (json.dumps(raw) + "\n").encode()
        before = file.raw
        with pytest.raises(sync.SyncError):
            sync.sync_once(s, file, "strict")
        assert s.by_uid(good_uid) is None and s.by_uid(uid) is None
        assert file.raw == before
    finally:
        s.close()


def test_v2_non_map_memories_refuses_file(tmp_path):
    s, file = SQLiteStore(tmp_path / "non-map.sqlite"), FakeFile()
    try:
        raw = {"format": sync.FORMAT, "version": 2, "memories": []}
        file.raw = (json.dumps(raw) + "\n").encode()
        with pytest.raises(sync.SyncError, match="memories map"):
            sync.sync_once(s, file, "strict")
        assert file.doc() == raw
    finally:
        s.close()


@pytest.mark.parametrize("bad_version", [True, False, 1.0, 2.0, "1", "2", None])
def test_noninteger_version_refuses_without_v1_rewrite(tmp_path, bad_version):
    s, file = SQLiteStore(tmp_path / "version.sqlite"), FakeFile()
    try:
        raw = {"format": sync.FORMAT, "version": bad_version, "memories": {
            "a" * 32: {"status": ACTIVE, "text": FACT, "updated_at": "invalid"}}}
        file.raw = (json.dumps(raw) + "\n").encode()
        before = file.raw
        with pytest.raises(sync.SyncError, match="newer"):
            sync.sync_once(s, file, "peer")
        assert s.by_uid("a" * 32) is None and file.raw == before
    finally:
        s.close()


def test_genuine_forget_after_equal_key_withdrawal_installs_global_tombstone(tmp_path):
    s, file = SQLiteStore(tmp_path / "withdrawn.sqlite"), FakeFile()
    try:
        m = s.add(FACT, SHARED, USER)
        other = s.add(FACT, SHARED, USER)
        terminal = str(order.MAX_VALUE)
        s._db.execute("UPDATE memories SET order_key = ? WHERE id = ?", (terminal, m.id))
        s._db.commit()
        s.apply_withdrawal(m.uid, fingerprint(FACT), "2026-01-01T00:00:00+00:00",
                           m.created_at, "other", terminal)
        assert s.by_uid(m.uid).status == FORGOTTEN
        assert s.tombstone(fingerprint(FACT)) is None
        forgotten = {"status": FORGOTTEN, "fingerprint": fingerprint(FACT),
                     "updated_at": "2026-01-01T00:00:00+00:00", "order": terminal}
        file.raw = (json.dumps({"format": sync.FORMAT, "version": 2,
                                "memories": {m.uid: forgotten}}) + "\n").encode()
        sync.sync_once(s, file, "peer")
        assert file.doc()["memories"][m.uid]["status"] == FORGOTTEN
        assert s.tombstone(fingerprint(FACT))[2] is True
        assert s.tombstone_order(fingerprint(FACT)) == terminal
        assert s.get(other.id).status == FORGOTTEN
    finally:
        s.close()


def test_older_same_words_tombstone_does_not_reclassify_withdrawal(tmp_path):
    s, file = SQLiteStore(tmp_path / "older-tombstone.sqlite"), FakeFile()
    try:
        m = s.add(FACT, SHARED, USER)
        s._db.execute("UPDATE memories SET order_key = '90' WHERE id = ?", (m.id,))
        s._db.commit()
        s.add_tombstone(fingerprint(FACT), SHARED, "2026-01-01T00:00:00+00:00", "50")
        s.apply_withdrawal(m.uid, fingerprint(FACT), "2026-01-01T00:00:00+00:00",
                           m.created_at, "other", "100")
        active = {"status": ACTIVE, "text": FACT, "fingerprint": fingerprint(FACT),
                  "updated_at": "2026-01-01T00:00:00+00:00", "order": "90"}
        file.raw = (json.dumps({"format": sync.FORMAT, "version": 2,
                                "memories": {m.uid: active}}) + "\n").encode()
        sync.sync_once(s, file, "peer")
        assert file.doc()["memories"][m.uid]["status"] == sync.WITHDRAWN
        assert s.tombstone_order(fingerprint(FACT)) == "50"
    finally:
        s.close()


def test_terminal_v2_active_can_be_forgotten_and_converges(tmp_path):
    a, b, file = SQLiteStore(tmp_path / "terminal-a.sqlite"), SQLiteStore(tmp_path / "terminal-b.sqlite"), FakeFile()
    try:
        uid = "c" * 32
        terminal = str(order.MAX_VALUE)
        active = {"status": ACTIVE, "text": FACT, "fingerprint": fingerprint(FACT),
                  "updated_at": "2026-01-01T00:00:00+00:00", "order": terminal}
        file.raw = (json.dumps({"format": sync.FORMAT, "version": 2,
                                "memories": {uid: active}}) + "\n").encode()
        sync.sync_once(a, file, "a")
        sync.sync_once(b, file, "b")
        a.forget(a.by_uid(uid).id)
        assert a.by_uid(uid).order_key == terminal
        sync.sync_once(a, file, "a")
        assert file.doc()["memories"][uid]["status"] == FORGOTTEN
        sync.sync_once(b, file, "b")
        assert b.by_uid(uid).status == FORGOTTEN
        assert b.tombstone_order(fingerprint(FACT)) == terminal
    finally:
        a.close()
        b.close()


def test_terminal_private_preference_suppresses_matching_shared_arrival(tmp_path):
    s, file = SQLiteStore(tmp_path / "private-terminal.sqlite"), FakeFile()
    try:
        private = s.add(FACT, PRIVATE, USER)
        uid, unrelated_uid = "d" * 32, "e" * 32
        terminal = str(order.MAX_VALUE)
        stamp = "2026-01-01T00:00:00+00:00"
        matching = {"status": ACTIVE, "text": FACT, "fingerprint": fingerprint(FACT),
                    "updated_at": stamp, "order": terminal}
        unrelated_text = "Collects stamps"
        unrelated = {"status": ACTIVE, "text": unrelated_text,
                     "fingerprint": fingerprint(unrelated_text), "updated_at": stamp,
                     "order": order.from_iso(stamp)}
        file.raw = (json.dumps({"format": sync.FORMAT, "version": 2,
                                "memories": {uid: matching, unrelated_uid: unrelated}}) + "\n").encode()
        near = sync.NearRepeats(find=lambda text: private if text == FACT else None,
                                tier_duplicates=PRIVATE)
        sync.sync_once(s, file, "peer", near=near)
        wire = file.doc()["memories"]
        assert wire[uid]["status"] == FORGOTTEN and wire[uid]["order"] == terminal
        assert wire[unrelated_uid]["status"] == ACTIVE
        assert s.get(private.id).status == ACTIVE
        assert s.by_uid(unrelated_uid).status == ACTIVE
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
        with pytest.raises(sync.SyncError, match="invalid forget or withdrawal"):
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
