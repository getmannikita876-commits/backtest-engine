from __future__ import annotations

from datetime import UTC, datetime, timedelta

#: The epoch every canonical serialization counts microseconds from.
_EPOCH: datetime = datetime(1970, 1, 1, tzinfo=UTC)


def epoch_microseconds(instant: datetime) -> int:
    """Return whole microseconds since the Unix epoch for a UTC instant.

    The canonical way this platform writes an instant into a deterministic
    serialization. Microseconds are the project's persisted precision (schema
    v2's ``timestamp[us, tz=UTC]``), integer arithmetic makes the encoding
    exact, and an integer has one textual form in every locale — unlike an ISO
    string, whose offset spelling and fractional digits are formatter choices.

    Lives here rather than in any one hashing module because more than one
    artifact is content-hashed, and two copies of this arithmetic would be two
    places for a serialization to drift.
    """
    return (validate_utc_datetime(instant) - _EPOCH) // timedelta(microseconds=1)


def validate_utc_datetime(value: object) -> datetime:
    """Validate that a datetime is timezone-aware and expressed in UTC."""
    if not isinstance(value, datetime):
        raise TypeError("datetime values must be timezone-aware UTC")
    if value.tzinfo is None:
        raise ValueError("datetime values must be timezone-aware UTC")
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("datetime values must be timezone-aware UTC")
    if value.tzinfo != UTC:
        raise ValueError("datetime values must be timezone-aware UTC")
    return value.astimezone(UTC)
