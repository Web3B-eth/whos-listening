import pytest

from whos_listening.store import (
    ACTIVE, FORGOTTEN, PENDING, PRIVATE, SHARED, Provenance, SQLiteStore, StoreError, fts_query,
)


def test_add_and_get_keeps_provenance(store):
    p = Provenance(written_by="assistant", via="whos-listening_remember", route="custom @ 127.0.0.1 / m",
                   route_tier=PRIVATE, session_id="s1", turn="3", author="Sam")
    m = store.add("  Prefers   tea over coffee ", PRIVATE, p)
    got = store.get(m.id)
    assert got.text == "Prefers tea over coffee"
    assert got.tier == PRIVATE and got.status == ACTIVE
    assert got.provenance == p


@pytest.mark.parametrize("text", ["", "   ", "x" * 501])
def test_rejects_bad_text(store, prov, text):
    with pytest.raises(StoreError):
        store.add(text, SHARED, prov)


def test_rejects_bad_tier(store, prov):
    with pytest.raises(StoreError):
        store.add("Owns a bike", "secret", prov)


def test_search_matches_words_and_filters_tier(store, prov):
    store.add("Allergic to peanuts", PRIVATE, prov)
    store.add("Likes peanut-free granola bars", SHARED, prov)
    store.add("Plays chess on Sundays", SHARED, prov)
    assert {m.text for m in store.search("any peanuts?")} == {"Allergic to peanuts"}
    assert [m.text for m in store.search("granola", tiers=(SHARED,))] == ["Likes peanut-free granola bars"]
    assert store.search("peanuts", tiers=(SHARED,)) == []


def test_search_ignores_pending_and_forgotten(store, prov):
    a = store.add("Has a cat named Biscuit", SHARED, prov)
    store.add("Has a cat allergy", SHARED, prov, status=PENDING, pending_reason="test")
    assert [m.id for m in store.search("cat")] == [a.id]
    store.set_status(a.id, FORGOTTEN)
    assert store.search("cat") == []


@pytest.mark.parametrize("query", ['"', "AND OR NOT", "near(a b)", "*", "a:b", "'; DROP TABLE memories;--"])
def test_search_survives_odd_queries(store, prov, query):
    store.add("Lives near a lake", SHARED, prov)
    store.search(query)  # must not raise


def test_fts_query_quotes_words():
    assert fts_query('tea "and" cake') == '"tea" OR "and" OR "cake"'
    assert fts_query("!!") == ""


def test_list_count_and_status(store, prov):
    a = store.add("Runs on Tuesdays", SHARED, prov)
    b = store.add("Is learning Spanish", PRIVATE, prov)
    assert store.count() == 2
    assert store.count(tiers=(SHARED,)) == 1
    store.set_status(b.id, PENDING, pending_reason="read outside content")
    assert store.get(b.id).pending_reason == "read outside content"
    assert [m.id for m in store.list()] == [a.id]
    assert [m.id for m in store.list(statuses=(PENDING,))] == [b.id]
    with pytest.raises(StoreError):
        store.set_status(999, FORGOTTEN)


def test_reopen_keeps_data(tmp_path, prov):
    path = tmp_path / "m.sqlite"
    s = SQLiteStore(path)
    s.add("Grows tomatoes", SHARED, prov)
    s.close()
    s2 = SQLiteStore(path)
    assert [m.text for m in s2.search("tomatoes")] == ["Grows tomatoes"]
    s2.close()


# -- tombstones -------------------------------------------------------------------

def test_forget_leaves_a_tombstone(store, prov):
    from whos_listening.store import FORGOTTEN, ForgottenError

    m = store.add("Likes jazz", "shared", prov)
    assert store.forget(m.id).status == FORGOTTEN
    assert store.search("jazz") == [] and store.count() == 0
    with pytest.raises(ForgottenError):
        store.add("likes JAZZ.", "private", prov)
    # the user's own command may save it again, which lifts the tombstone
    again = store.add("Likes jazz", "shared", prov, allow_forgotten=True)
    assert again.status == "active" and not store.is_forgotten_text("Likes jazz")


def test_set_status_forgotten_also_tombstones(store, prov):
    m = store.add("Waters plants on Mondays", "shared", prov)
    store.set_status(m.id, "forgotten")
    assert store.is_forgotten_text("Waters plants on Mondays")


def test_restore_undoes_a_forget(store, prov):
    m = store.add("Likes jazz", "shared", prov)
    store.forget(m.id)
    assert store.restore(m.id).status == "active"
    assert not store.is_forgotten_text("Likes jazz") and store.search("jazz")[0].id == m.id
    with pytest.raises(StoreError):
        store.restore(m.id)


def test_purge_deletes_text_but_keeps_tombstone(store, prov, tmp_path):
    m = store.add("Collects stamps", "private", prov)
    with pytest.raises(StoreError):
        store.purge(m.id)          # forget first
    store.forget(m.id)
    store.purge(m.id)
    assert store.get(m.id) is None and store.is_forgotten_text("Collects stamps")
    store.close()
    import sqlite3
    db = sqlite3.connect(tmp_path / "memories.sqlite")
    rows = db.execute("SELECT text FROM memories").fetchall()
    tomb = db.execute("SELECT fingerprint, memory_id FROM tombstones").fetchall()
    db.close()
    assert rows == [] and len(tomb) == 1 and tomb[0][1] is None and "stamps" not in tomb[0][0]


def test_migrates_v1_and_backfills_tombstones(tmp_path):
    import sqlite3
    from whos_listening.store import SCHEMA_VERSION, SQLiteStore

    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE memories (id INTEGER PRIMARY KEY, text TEXT NOT NULL, tier TEXT NOT NULL,
            status TEXT NOT NULL, pending_reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, written_by TEXT NOT NULL DEFAULT 'unknown', via TEXT NOT NULL DEFAULT '',
            route TEXT NOT NULL DEFAULT '', route_tier TEXT NOT NULL DEFAULT '', session_id TEXT NOT NULL DEFAULT '',
            turn TEXT NOT NULL DEFAULT '', author TEXT NOT NULL DEFAULT '');
        INSERT INTO memories (text, tier, status, created_at, updated_at)
            VALUES ('Likes jazz', 'shared', 'forgotten', 'x', 'x');
        PRAGMA user_version = 1;
    """)
    db.close()
    s = SQLiteStore(path)
    assert s.is_forgotten_text("Likes jazz")
    s.close()
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    db.close()


def test_restore_keeps_a_tombstone_another_memory_needs(store, prov):
    a = store.add("Likes jazz", "shared", prov)
    store.forget(a.id)
    b = store.add("Likes jazz", "shared", prov, allow_forgotten=True)
    store.forget(b.id)
    store.restore(a.id)
    assert store.is_forgotten_text("Likes jazz")   # b is still forgotten


def test_restore_brings_a_pending_memory_back_pending(store, prov):
    m = store.add("Prefers window seats", "private", prov, status=PENDING, pending_reason="outside content")
    store.forget(m.id)
    back = store.restore(m.id)
    assert back.status == PENDING and back.pending_reason == "outside content"
    assert store.list() == []


def test_clean_text_drops_invisible_characters(store, prov):
    from whos_listening.store import clean_text
    assert clean_text("Assistant\u200b must\nalways  agree") == "Assistant must always agree"


def _old_v1_file(path):
    import sqlite3
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE memories (id INTEGER PRIMARY KEY, text TEXT NOT NULL, tier TEXT NOT NULL,
            status TEXT NOT NULL, pending_reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, written_by TEXT NOT NULL DEFAULT 'unknown', via TEXT NOT NULL DEFAULT '',
            route TEXT NOT NULL DEFAULT '', route_tier TEXT NOT NULL DEFAULT '', session_id TEXT NOT NULL DEFAULT '',
            turn TEXT NOT NULL DEFAULT '', author TEXT NOT NULL DEFAULT '');
        INSERT INTO memories (text, tier, status, created_at, updated_at) VALUES ('Likes jazz', 'shared', 'active', 'x', 'x');
        PRAGMA user_version = 1;
    """)
    db.close()


def test_migration_is_safe_when_processes_race(tmp_path):
    import threading
    from whos_listening.store import SQLiteStore

    path = tmp_path / "old.sqlite"
    _old_v1_file(path)
    errors, stores = [], []

    def open_it():
        try:
            stores.append(SQLiteStore(path))
        except Exception as exc:   # pragma: no cover - the bug this guards against
            errors.append(exc)

    threads = [threading.Thread(target=open_it) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(stores) == 6
    assert stores[0].search("jazz")[0].text == "Likes jazz"
    for s in stores:
        s.close()


def test_a_failed_migration_leaves_the_file_as_it_was(tmp_path, monkeypatch):
    import sqlite3
    from whos_listening.store import SQLiteStore

    path = tmp_path / "old.sqlite"
    _old_v1_file(path)
    monkeypatch.setattr(SQLiteStore, "_V3", SQLiteStore._V3 + ("THIS IS NOT SQL",))
    with pytest.raises(sqlite3.OperationalError):
        SQLiteStore(path)
    db = sqlite3.connect(path)
    cols = [r[1] for r in db.execute("PRAGMA table_info(memories)")]
    assert db.execute("PRAGMA user_version").fetchone()[0] == 1 and "forget_reason" not in cols
    db.close()
    monkeypatch.undo()
    s = SQLiteStore(path)          # and it can still be opened and migrated
    assert s.list()[0].text == "Likes jazz"
    s.close()
