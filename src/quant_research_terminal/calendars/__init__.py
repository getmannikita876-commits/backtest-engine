"""Versioned calendar-definition data and its strict loader.

This package owns the *data* side of exchange calendars: declarative TOML
definition files shipped as package resources, and the loader that validates
them into :class:`~quant_research_terminal.domain.calendar_definition.CalendarDefinition`
models. All calendar *semantics* — materialization, resolution, identity —
live in the domain layer, which this package depends on and which must never
depend back on it.
"""

from quant_research_terminal.calendars.loader import (
    CME_EQUITY_INDEX_V1_RESOURCE,
    load_cme_equity_index_v1,
    load_packaged_definition,
    parse_calendar_definition,
)

__all__ = [
    "CME_EQUITY_INDEX_V1_RESOURCE",
    "load_cme_equity_index_v1",
    "load_packaged_definition",
    "parse_calendar_definition",
]
