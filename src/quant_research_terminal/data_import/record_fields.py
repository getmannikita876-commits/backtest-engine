"""Canonical field names for provider-neutral import records.

Providers decode source files into these field names, validators check them, and
normalizers map them onto domain models. Declaring the sets once here prevents
the three stages from drifting apart as vendors are added.

These are *import* field names that mirror the domain models in
:mod:`quant_research_terminal.domain`. They are deliberately not storage column
names: the encoding used for persistence is owned by
:mod:`quant_research_terminal.data` and must remain free to evolve separately.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from quant_research_terminal.data_import.contracts import ImportRecordType

TIMESTAMP_FIELD: Final = "timestamp"
INSTRUMENT_FIELD: Final = "instrument_symbol"

#: The duration a bar covers, carried as a :class:`~datetime.timedelta`.
#:
#: Bars carry availability time in ``timestamp`` and their duration here;
#: interval start is exactly ``timestamp - interval``. Keeping ``timestamp``
#: on every record type is what lets the timestamp, ordering, and window-filter
#: rules stay uniform across trades, quotes, and bars.
INTERVAL_FIELD: Final = "interval"

TRADE_FIELDS: Final[tuple[str, ...]] = (
    TIMESTAMP_FIELD,
    INSTRUMENT_FIELD,
    "price",
    "size",
    "side",
)

QUOTE_FIELDS: Final[tuple[str, ...]] = (
    TIMESTAMP_FIELD,
    INSTRUMENT_FIELD,
    "bid",
    "ask",
    "bid_size",
    "ask_size",
)

BAR_FIELDS: Final[tuple[str, ...]] = (
    TIMESTAMP_FIELD,
    INSTRUMENT_FIELD,
    INTERVAL_FIELD,
    "open",
    "high",
    "low",
    "close",
    "volume",
)

REQUIRED_FIELDS: Final[Mapping[ImportRecordType, tuple[str, ...]]] = MappingProxyType(
    {
        ImportRecordType.TRADE: TRADE_FIELDS,
        ImportRecordType.QUOTE: QUOTE_FIELDS,
        ImportRecordType.BAR: BAR_FIELDS,
    }
)

# Fields carrying a *price*: a fractional decimal on the fixed-point scale.
#
# Ordered tuples, not sets: iteration order over a set of strings depends on
# hash randomisation, which would make the field named in a diagnostic vary
# between runs.
PRICE_FIELDS: Final[Mapping[ImportRecordType, tuple[str, ...]]] = MappingProxyType(
    {
        ImportRecordType.TRADE: ("price",),
        ImportRecordType.QUOTE: ("bid", "ask"),
        ImportRecordType.BAR: ("open", "high", "low", "close"),
    }
)

# Fields carrying a *quantity*: a count, stored as an unsigned integer.
QUANTITY_FIELDS: Final[Mapping[ImportRecordType, tuple[str, ...]]] = MappingProxyType(
    {
        ImportRecordType.TRADE: ("size",),
        ImportRecordType.QUOTE: ("bid_size", "ask_size"),
        ImportRecordType.BAR: ("volume",),
    }
)

# Fields whose text form must decode to Decimal. Prices and sizes never pass
# through float, because binary floating point cannot represent tick values
# exactly and would corrupt fixed-point storage encoding downstream.
DECIMAL_FIELDS: Final[Mapping[ImportRecordType, frozenset[str]]] = MappingProxyType(
    {
        record_type: frozenset((*PRICE_FIELDS[record_type], *QUANTITY_FIELDS[record_type]))
        for record_type in ImportRecordType
    }
)


def required_fields(record_type: ImportRecordType) -> tuple[str, ...]:
    """Return the ordered required field names for ``record_type``."""
    return REQUIRED_FIELDS[record_type]


def decimal_fields(record_type: ImportRecordType) -> frozenset[str]:
    """Return the field names that must decode to :class:`~decimal.Decimal`."""
    return DECIMAL_FIELDS[record_type]


def price_fields(record_type: ImportRecordType) -> tuple[str, ...]:
    """Return the price field names, in a deterministic order."""
    return PRICE_FIELDS[record_type]


def quantity_fields(record_type: ImportRecordType) -> tuple[str, ...]:
    """Return the quantity field names, in a deterministic order."""
    return QUANTITY_FIELDS[record_type]
