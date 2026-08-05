"""Deterministic ordering of accepted import events.

``docs/replay_rules.md`` fixes the sort priority as timestamp, then event-type
priority, then sequence number. This module supplies the concrete event-type
priorities and assembles the complete key, so ordering is defined once and the
orchestration layer only applies it.

Event-type precedence
---------------------
At an identical timestamp the order is **quote, then trade, then bar**:

* A quote publishes book state. A trade executes against that state, so
  processing the trade first would match it against a stale book.
* A bar summarises a period that has already closed. ``docs/replay_rules.md``
  requires that bars become visible only after they close, so a bar must never
  be seen before the ticks that compose it.

Sequence number
---------------
``RawRecord.source_index`` serves as the sequence number. It is the record's
position in its source, assigned before any filtering, so two records that
agree on both timestamp and event type still have a stable, reproducible order
that does not depend on dictionary iteration, filesystem order, or the machine
the import runs on.

The key is total: no two records in one import can compare equal unless they
occupy the same source position, which is impossible by construction.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Final

from quant_research_terminal.data_import.contracts import ImportRecordType

#: Lower values sort earlier among events sharing a timestamp.
EVENT_TYPE_PRECEDENCE: Final[Mapping[ImportRecordType, int]] = MappingProxyType(
    {
        ImportRecordType.QUOTE: 0,
        ImportRecordType.TRADE: 1,
        ImportRecordType.BAR: 2,
    }
)


def event_type_precedence(record_type: ImportRecordType) -> int:
    """Return the tiebreak rank for ``record_type``."""
    return EVENT_TYPE_PRECEDENCE[record_type]


def event_ordering_key(
    *, record_type: ImportRecordType, timestamp: datetime, source_index: int
) -> tuple[datetime, int, int]:
    """Return the complete ordering key for one accepted event.

    The key is ``(timestamp, event-type precedence, source index)``. Sorting a
    sequence of accepted records by this key yields the only ordering the
    replay engine may observe for that input.
    """
    return (timestamp, event_type_precedence(record_type), source_index)
