import pytest

from whos_listening.store import (
    ACTIVE, FORGOTTEN, PENDING, PRIVATE, SHARED, Provenance, SQLiteStore, StoreError, fts_query,
)


def test_add_and_get_keeps_provenance(store):
    p = Provenance(written_by="assistant", via="wl_remember", route="custom @ 127.0.0.1 / m",
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
