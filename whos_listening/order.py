"""Bounded decimal order values for last-change-wins sync.

Legacy ISO times map exactly to UTC microseconds since 0001-01-01. The value is stored as
canonical decimal text but compared as an integer. A 64-digit budget leaves vast practical
headroom after every representable legacy date; it is still finite.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

EPOCH = datetime(1, 1, 1, tzinfo=timezone.utc)
MICROS = 1_000_000
MAX_DIGITS = 64
# Reserve one million whole-second successors before the 64-digit storage ceiling.
MAX_VALUE = 10**MAX_DIGITS - 1 - 1_000_000 * MICROS


class OrderError(ValueError):
    pass


def parse(value: str) -> int:
    if (not isinstance(value, str) or not value or len(value) > MAX_DIGITS
            or not value.isascii() or not value.isdecimal() or (len(value) > 1 and value[0] == "0")):
        raise OrderError("The sync order value is invalid or too long.")
    number = int(value)
    if number > MAX_VALUE:
        raise OrderError("The sync order value has no supported successor headroom.")
    return number


def key(value: int) -> str:
    if not isinstance(value, int) or value < 0 or value > MAX_VALUE:
        raise OrderError("No supported sync order value remains.")
    return str(value)


def from_iso(stamp: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(stamp))
        parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        delta = parsed - EPOCH
        value = ((delta.days * 86400 + delta.seconds) * MICROS + delta.microseconds)
    except (ValueError, OverflowError, TypeError) as exc:
        raise OrderError("The timestamp cannot be ordered.") from exc
    return key(value)


def next_key(previous: str, now: str) -> str:
    prior = parse(previous)
    wall = parse(from_iso(now))
    # Keep legacy stamp_after behavior: use the current whole second if later; otherwise the
    # next whole second after the observed value, including fractional legacy instants.
    next_value = wall if wall > prior else (prior // MICROS + 1) * MICROS
    return key(next_value)


def display(value: str, wall_now: str) -> str:
    """Keep the old ISO display when representable; otherwise show the actual local wall time."""
    number = parse(value)
    try:
        return (EPOCH + timedelta(microseconds=number)).replace(microsecond=0).isoformat()
    except OverflowError:
        return wall_now
