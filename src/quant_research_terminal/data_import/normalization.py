"""Normalization: validated raw records to immutable domain objects.

Normalization is the last import stage before domain objects exist, and the
first point at which a value acquires domain meaning. The direction of the
whole pipeline is one-way::

    Provider -> Raw Records -> Validation -> Normalization -> Domain Objects
                                                                    |
                                                                    v
                                                          Storage Contracts

Nothing in this module knows how domain objects are persisted. Storage encoding
is owned by :mod:`quant_research_terminal.data`, and keeping normalization
unaware of it is what allows the storage schema to change without touching a
single provider.

A normalizer assumes its input has already been validated, and raises rather
than guesses when a value is still unusable. It never repairs data: by this
stage a defect means a validation stage was skipped, which is a wiring bug
worth surfacing loudly rather than absorbing quietly. Every check it makes
therefore delegates to the same
:mod:`~quant_research_terminal.data_import.time_semantics` and
:mod:`~quant_research_terminal.data_import.numeric_semantics` rules the
validators used, so the two stages cannot drift apart.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from quant_research_terminal.data_import.contracts import ImportRecordType
from quant_research_terminal.data_import.numeric_semantics import to_decimal
from quant_research_terminal.data_import.raw_record import RawRecord
from quant_research_terminal.data_import.record_fields import (
    INSTRUMENT_FIELD,
    TIMESTAMP_FIELD,
)
from quant_research_terminal.data_import.time_semantics import require_utc
from quant_research_terminal.domain.models import Bar, Quote, Trade

DomainRecord = Trade | Quote | Bar


@runtime_checkable
class RecordNormalizer(Protocol):
    """Converts one validated raw record into an immutable domain object."""

    def normalize(self, record: RawRecord) -> DomainRecord:
        """Return the domain object described by ``record``.

        Raises:
            ValueError: when the record cannot be represented as a domain object.
        """
        ...


class DefaultRecordNormalizer:
    """Normalizes the record types the domain layer models today.

    Dispatch is driven by ``record.record_type``, so one stateless instance
    handles trades, quotes, and bars, and a mixed sequence needs no special
    handling from the caller.
    """

    def normalize(self, record: RawRecord) -> DomainRecord:
        """Return the domain object described by ``record``.

        Raises:
            ValueError: if the timestamp is not fixed-offset UTC, or a numeric
                field cannot convert exactly to :class:`~decimal.Decimal`.
        """
        if record.record_type is ImportRecordType.TRADE:
            return self._trade(record)
        if record.record_type is ImportRecordType.QUOTE:
            return self._quote(record)
        return self._bar(record)

    def _trade(self, record: RawRecord) -> Trade:
        return Trade(
            instrument_symbol=self._symbol(record),
            timestamp=require_utc(record.value(TIMESTAMP_FIELD)),
            price=to_decimal(record.value("price")),
            size=to_decimal(record.value("size")),
            side=str(record.value("side")),
        )

    def _quote(self, record: RawRecord) -> Quote:
        return Quote(
            instrument_symbol=self._symbol(record),
            timestamp=require_utc(record.value(TIMESTAMP_FIELD)),
            bid=to_decimal(record.value("bid")),
            ask=to_decimal(record.value("ask")),
            bid_size=to_decimal(record.value("bid_size")),
            ask_size=to_decimal(record.value("ask_size")),
        )

    def _bar(self, record: RawRecord) -> Bar:
        return Bar(
            instrument_symbol=self._symbol(record),
            timestamp=require_utc(record.value(TIMESTAMP_FIELD)),
            open=to_decimal(record.value("open")),
            high=to_decimal(record.value("high")),
            low=to_decimal(record.value("low")),
            close=to_decimal(record.value("close")),
            volume=to_decimal(record.value("volume")),
        )

    @staticmethod
    def _symbol(record: RawRecord) -> str:
        return str(record.value(INSTRUMENT_FIELD))
