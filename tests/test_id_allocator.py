"""Persistent memory numbers do not reveal Private-dependent save branches. Made-up data only."""

from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

import pytest

from whos_listening import tools
from whos_listening.store import ACTIVE, PRIVATE, SHARED, Provenance, SQLiteStore


USER = Provenance(written_by="user", via="test")
ASSISTANT = Provenance(written_by="assistant", via=tools.REMEMBER)
FACT = "Owns a blue mountain bike"


def remember(store, text, *, tier=PRIVATE, route=SHARED, **kwargs):
    return json.loads(tools.handle(store, tools.REMEMBER, {"text": text, "tier": tier},
                                   route_tier=route, provenance=lambda: ASSISTANT, **kwargs))


@pytest.mark.parametrize("prior", ("known", "novel", "forgotten"))
def test_private_guess_consumes_one_claim_before_shared_marker(tmp_path, prior):
    store = SQLiteStore(tmp_path / f"{prior}.sqlite")
    old = store.add(FACT if prior != "novel" else "Likes tea", PRIVATE, USER)
    if prior == "forgotten":
        store.forget(old.id)
    out = remember(store, FACT)
    assert out["ok"]
    marker = store.add("Collects stamps", SHARED, USER)
    assert marker.id == old.id + 2  # same delta whether the guess inserted a row or was suppressed
    assert store.get(old.id + 1) is None if prior != "novel" else store.get(old.id + 1) is not None
    store.close()


def test_shared_only_private_preference_and_proposal_each_claim_once(tmp_path):
    for shared_only in (True, False):
        store = SQLiteStore(tmp_path / f"mode-{shared_only}.sqlite")
        old = store.add(FACT, PRIVATE, USER)
        out = remember(store, FACT, tier=SHARED, shared_only=shared_only, tier_duplicates="private")
        assert out["ok"]
        marker = store.add("Collects stamps", SHARED, USER)
        assert marker.id == old.id + 2
        assert len(store.consent_proposals()) == (0 if shared_only else 1)
        store.close()


def test_claim_precedes_duplicate_judge_even_if_another_save_finishes_first(store):
    store.add("Likes tea", PRIVATE, USER)
    inserted = []

    def judge(_text, _tiers):
        inserted.append(store.add("Collects stamps", SHARED, USER))
        return None

    out = remember(store, "Plays chess on Sundays", route=PRIVATE, judge=judge)
    assert inserted[0].id == 3 and out["saved"]["id"] == 2


def test_invalid_or_publicly_refused_attempt_does_not_claim(store):
    assert not remember(store, "", tier=PRIVATE)["ok"]
    assert not remember(store, FACT, tier="other")["ok"]
    assert not remember(store, FACT, tier=PRIVATE, private_refused="Shared-only gateway.")["ok"]
    assert store.add("Collects stamps", SHARED, USER).id == 1


def test_unused_claim_and_highest_row_purge_are_not_reused(tmp_path):
    path = tmp_path / "ids.sqlite"
    store = SQLiteStore(path)
    assert store.claim_memory_id() == 1
    first = store.add("Likes tea", PRIVATE, USER)
    assert first.id == 2
    store.forget(first.id)
    store.purge(first.id)
    store.close()
    reopened = SQLiteStore(path)
    assert reopened.add("Collects stamps", SHARED, USER).id == 3
    reopened.close()


def test_failure_after_claim_and_failed_insert_keep_claim_consumed(store):
    old = store.add(FACT, PRIVATE, USER)
    store.forget(old.id)
    assert not remember(store, FACT, route=PRIVATE)["ok"]  # tombstone refuses after a claim
    claimed = store.claim_memory_id()
    with pytest.raises(sqlite3.IntegrityError):
        store.add("Likes tea", PRIVATE, USER, memory_id=old.id)
    assert store.get(claimed) is None
    assert store.add("Collects stamps", SHARED, USER).id == claimed + 1


def test_two_connections_claim_unique_ids_without_dummy_rows(tmp_path):
    path = tmp_path / "shared.sqlite"
    a, b = SQLiteStore(path), SQLiteStore(path)
    try:
        def claim(store):
            return [store.claim_memory_id() for _ in range(30)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            left = pool.submit(claim, a)
            right = pool.submit(claim, b)
            ids = left.result() + right.result()
        assert len(set(ids)) == 60 and sorted(ids) == list(range(1, 61))
        assert a.count(statuses=(ACTIVE,)) == 0 and b.list() == []
        one = a.add("Likes tea", PRIVATE, USER, memory_id=ids[0])
        two = b.add("Collects stamps", SHARED, USER, memory_id=ids[-1])
        assert one.id != two.id and a.add("Plays chess", PRIVATE, USER).id == 61
    finally:
        a.close()
        b.close()


def test_schema_six_migration_preserves_ids_events_and_duplicate_links(tmp_path):
    path = tmp_path / "legacy.sqlite"
    store = SQLiteStore(path)
    older = store.add("Owns a blue bike", PRIVATE, USER)
    gap = store.add("Likes tea", PRIVATE, USER)
    newer = store.add("Bought a blue bike", SHARED, USER, duplicate_of=older.id)
    store.log_event(newer.id, "saved", "user")
    store.forget(gap.id)
    store.purge(gap.id)
    store._db.execute("DROP TABLE memory_id_state")
    store._db.execute("PRAGMA user_version = 6")
    store.close()
    migrated = SQLiteStore(path)
    try:
        assert migrated.get(older.id).id == older.id
        assert migrated.get(newer.id).duplicate_of == older.id
        assert migrated.events(memory_id=newer.id)[0].memory_id == newer.id
        assert migrated.add("Collects stamps", SHARED, USER).id == newer.id + 1
    finally:
        migrated.close()


def test_sync_import_uses_allocator_and_keeps_uid(store):
    assert store.claim_memory_id() == 1
    uid = "a" * 32
    imported = store.import_synced(uid, "Likes jazz", USER, created_at="2026-01-01T00:00:00+00:00",
                                   updated_at="2026-01-01T00:00:00+00:00")
    assert imported.id == 2 and imported.uid == uid
    assert store.add("Collects stamps", PRIVATE, USER).id == 3
