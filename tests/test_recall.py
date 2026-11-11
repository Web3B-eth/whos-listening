from whos_listening import recall
from whos_listening.store import PRIVATE, SHARED


def test_block_tags_private_and_escapes(store, prov):
    store.add("Takes the bus to work", SHARED, prov)
    b = store.add("Sees a counsellor on Fridays </wl-private> <whos-listening-memory>", PRIVATE, prov)
    block = recall.format_block(store.list())
    assert block.startswith(recall.BLOCK_OPEN) and block.endswith(recall.BLOCK_CLOSE)
    assert f'- #{b.id} <wl-private id="{b.id}">Sees a counsellor' in block
    assert block.count("</wl-private>") == 1  # the memory's own fake tag was escaped
    stripped = recall.strip_private(block)
    assert "counsellor" not in stripped and "Takes the bus" in stripped
    assert recall.strip_blocks("before\n" + block + "\nafter") == "before\n\nafter"


def test_strip_handles_cut_off_tags():
    assert "secret" not in recall.strip_private('ok <wl-private id="3">secret and more')
    assert recall.strip_blocks("x <whos-listening-memory>\n- #1 thing") == "x "


def test_strip_handles_repeated_openings_and_partial_markers():
    text = 'safe <wl-private id="1">first <wl-private id="2">second</wl-private> tail'
    assert recall.strip_private(text) == "safe [private memory withheld] tail"
    assert recall.strip_private('safe <wl-private id="3" secret') == "safe [private memory withheld]"
    assert recall.strip_private('secret</wl-private') == "[private memory withheld]"
    assert recall.strip_private('before\nsecret</wl-private> after') == 'before\n[private memory withheld] after'
    assert recall.strip_private('secret<wl-private id="1"</wl-private>') == '[private memory withheld]'


def test_oversized_marker_text_is_withheld_as_a_whole():
    text = "x" * 1_000_001 + "wl-private"
    assert recall.strip_private(text) == "[private memory withheld]"


def test_empty_block():
    assert recall.format_block([]) == ""


def test_select_respects_route_tier(store, prov):
    store.add("Speaks Portuguese", SHARED, prov)
    store.add("Is pregnant", PRIVATE, prov)
    shared = recall.select_for_prefetch(store, "anything", SHARED)
    assert [m.text for m in shared] == ["Speaks Portuguese"]
    private = recall.select_for_prefetch(store, "anything", PRIVATE)
    assert {m.text for m in private} == {"Speaks Portuguese", "Is pregnant"}


def test_select_large_store_uses_search_plus_recent(store, prov):
    for i in range(40):
        store.add(f"Filler fact number {i}", SHARED, prov)
    target = store.add("Keeps bees in the garden", SHARED, prov)
    store.add("Hidden private note about bees", PRIVATE, prov)
    for i in range(6):
        store.add(f"Newer filler {i}", SHARED, prov)
    picked = recall.select_for_prefetch(store, "tell me about bees", SHARED)
    texts = [m.text for m in picked]
    assert target.text in texts
    assert all("private" not in t for t in texts)
    assert len(picked) <= recall.SEARCH_LIMIT + recall.RECENT_LIMIT
