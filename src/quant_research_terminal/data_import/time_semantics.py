"""UTC timestamp semantics shared by the data-import stages.

The repository rules require UTC-only timestamps that are rejected rather than
silently repaired. Two stages need that rule with different failure modes:

* Provider request construction must *raise* immediately, because a malformed
  request is a programming error rather than a data defect.
* Batch validation must *report* an issue, because a malformed record is data
  the operator needs to see rather than an exception that aborts a whole file.

Both behaviours are derived from :func:`classify_timestamp` so the definition of
"a valid UTC timestamp" exists exactly once.

A timestamp is accepted only when it carries a fixed zero UTC offset. Named
zones are rejected even when their current offset happens to be zero (for
example ``Europe/London`` in winter), because their offset is not constant and
would reintroduce daylight-saving ambiguity into replay ordering.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import StrEnum

_ZERO_OFFSET = timedelta(0)


class TimestampStatus(StrEnum):
    """Outcome of inspecting a candidate timestamp value."""

    VALID = "valid"
    NOT_DATETIME = "not_datetime"
    NAIVE = "naive"
    NON_UTC = "non_utc"


class UtcTimestampError(ValueError):
    """Raised when a value is required to be a UTC timestamp but is not."""

    def __init__(self, status: TimestampStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


_STATUS_MESSAGES: dict[TimestampStatus, str] = {
    TimestampStatus.NOT_DATETIME: "timestamp must be a datetime instance",
    TimestampStatus.NAIVE: "timestamp must be timezone-aware UTC",
    TimestampStatus.NON_UTC: "timestamp must use a fixed UTC offset of zero",
}


def classify_timestamp(value: object) -> TimestampStatus:
    """Classify ``value`` against the repository's UTC timestamp rule.

    The value is never modified, converted, or normalized; this function only
    reports what the value is.
    """
    if not isinstance(value, datetime):
        return TimestampStatus.NOT_DATETIME

    offset = value.utcoffset()
    if offset is None:
        return TimestampStatus.NAIVE
    if offset != _ZERO_OFFSET:
        return TimestampStatus.NON_UTC
    if not isinstance(value.tzinfo, timezone):
        # Zero offset right now, but a non-fixed zone: reject it rather than
        # accept a value whose offset depends on the date.
        return TimestampStatus.NON_UTC
    return TimestampStatus.VALID


def status_message(status: TimestampStatus) -> str:
    """Return the canonical human-readable message for a failing status."""
    return _STATUS_MESSAGES[status]


def require_utc(value: object) -> datetime:
    """Return ``value`` unchanged when it is a valid UTC timestamp.

    Raises:
        UtcTimestampError: when the value is not a fixed-offset UTC datetime.
    """
    if not isinstance(value, datetime):
        status = TimestampStatus.NOT_DATETIME
        raise UtcTimestampError(status, status_message(status))

    status = classify_timestamp(value)
    if status is not TimestampStatus.VALID:
        raise UtcTimestampError(status, status_message(status))
    return value
