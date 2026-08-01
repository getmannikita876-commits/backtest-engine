from __future__ import annotations

from datetime import UTC, datetime


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
