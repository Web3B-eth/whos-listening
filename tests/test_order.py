"""Scalar sync order at legacy timestamp and finite-budget boundaries."""

from datetime import datetime, timedelta, timezone

import pytest

from whos_listening import order


def test_fractional_and_offset_legacy_instants_map_exactly():
    same = order.from_iso("2026-01-01T00:00:00.123456+00:00")
    assert same == order.from_iso("2025-12-31T19:00:00.123456-05:00")
    assert int(order.from_iso("2026-01-01T00:00:00.123457+00:00")) == int(same) + 1
    assert int(order.from_iso("2026-01-01T00:00:01+00:00")) > int(same)


def test_max_legacy_date_has_whole_second_successor():
    old = order.from_iso("9999-12-31T23:59:59+00:00")
    next_value = order.next_key(old, "2026-01-01T00:00:00+00:00")
    assert int(next_value) > int(old)
    assert order.display(next_value, "2026-01-01T00:00:00+00:00") == "2026-01-01T00:00:00+00:00"
    shifted = order.from_iso("9999-12-31T23:59:59-05:00")
    assert int(shifted) > int(old)
    assert int(order.next_key(shifted, "2026-01-01T00:00:00+00:00")) > int(shifted)


def test_successor_keeps_legacy_whole_second_granularity():
    old = order.from_iso("2099-01-01T00:00:00.5+00:00")
    next_value = order.next_key(old, "2026-01-01T00:00:00+00:00")
    assert int(next_value) == int(old) + 500_000


@pytest.mark.parametrize("bad", ["", "00", "01", "-1", "+1", "1.0", "1e3", "９", "9" * 65,
                                  "9" * 64])
def test_malformed_or_budget_exhausting_order_is_rejected(bad):
    with pytest.raises(order.OrderError):
        order.parse(bad)


def test_boundary_fails_without_wrapping():
    with pytest.raises(order.OrderError):
        order.next_key(str(order.MAX_VALUE), "2026-01-01T00:00:00+00:00")


def test_store_change_at_order_budget_fails_without_mutation(tmp_path):
    from whos_listening.store import Provenance, SQLiteStore, StoreError, fingerprint

    s = SQLiteStore(tmp_path / "budget.sqlite")
    try:
        m = s.add("Likes jazz", "shared", Provenance(written_by="user"))
        s._db.execute("UPDATE memories SET order_key = ? WHERE id = ?", (str(order.MAX_VALUE), m.id))
        s._db.commit()
        before = s.get(m.id)
        with pytest.raises(StoreError, match="order limit"):
            s.forget(m.id)
        assert s.get(m.id) == before and s.tombstone_order(fingerprint("Likes jazz")) is None
    finally:
        s.close()
