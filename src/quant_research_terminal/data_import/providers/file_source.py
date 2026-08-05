"""Shared plumbing for file-backed providers.

Reading a delimited export and deciding whether a decoded record satisfies a
request are mechanical concerns that every file-backed provider needs and none
of them should re-implement. Centralising them here keeps one definition of
what a structurally broken file is, and one definition of when filtering is
allowed to drop a row.

Nothing here interprets a value. Field semantics belong to the provider that
knows the vendor.
"""

from __future__ import annotations

import csv
from collections.abc import Generator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from quant_research_terminal.data_import.providers.provider import (
    ProviderDecodeError,
    ProviderRequest,
)
from quant_research_terminal.data_import.record_fields import (
    INSTRUMENT_FIELD,
    TIMESTAMP_FIELD,
)
from quant_research_terminal.data_import.time_semantics import (
    TimestampStatus,
    classify_timestamp,
)


def iter_delimited_rows(
    path: Path, *, encoding: str
) -> Generator[tuple[int, dict[str, str]], None, None]:
    """Yield ``(source_index, row)`` pairs from a delimited text file.

    The file is opened with ``newline=""`` so the :mod:`csv` module, rather
    than the text layer, decides where a row ends. A field may therefore
    contain an embedded newline without splitting the row.

    ``source_index`` is the row's zero-based position among the data rows,
    assigned before any filtering, so it remains a stable provenance reference.

    The caller owns the returned generator: the file handle is released when it
    is exhausted, closed, or an exception propagates out of it.

    Raises:
        ProviderDecodeError: if the file has no header row, or a data row's
            column count disagrees with the header. Both make the row-to-column
            mapping ambiguous, so no per-row diagnosis would be meaningful.
    """
    with path.open("r", encoding=encoding, newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header is None:
            raise ProviderDecodeError(f"{path} contains no header row")

        for source_index, row in enumerate(reader):
            if len(row) != len(header):
                raise ProviderDecodeError(
                    f"{path} row {source_index} has {len(row)} columns, expected {len(header)}"
                )
            yield source_index, dict(zip(header, row, strict=True))


def is_excluded_by_request(fields: Mapping[str, Any], request: ProviderRequest) -> bool:
    """Return whether a decoded record falls outside ``request``.

    A record is only ever excluded on evidence. If its instrument field is
    absent, or its timestamp did not decode to a valid UTC datetime, the record
    is kept so the validation stage can report the defect. Filtering must never
    be the reason a malformed row disappears.
    """
    symbol = fields.get(INSTRUMENT_FIELD)
    if isinstance(symbol, str) and symbol != request.instrument_symbol:
        return True

    timestamp = fields.get(TIMESTAMP_FIELD)
    if classify_timestamp(timestamp) is not TimestampStatus.VALID:
        return False
    if not isinstance(timestamp, datetime):
        return False
    return not request.contains(timestamp)
