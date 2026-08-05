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


def require_non_blank_symbol(symbol: str, label: str) -> str:
    """Return ``symbol`` unchanged, rejecting a blank or whitespace-only value.

    ``"   "`` satisfies the domain's ``min_length`` constraint while carrying no
    instrument identity at all, so it must never reach a record. Checking at
    configuration time fails at the call site rather than days later inside a
    research result.

    Raises:
        ValueError: if the symbol contains no non-whitespace character.
    """
    if not symbol.strip():
        raise ValueError(
            f"{label} is blank; an instrument symbol must contain a non-whitespace value"
        )
    return symbol


def reconcile_symbols(
    *,
    embedded: str | None,
    configured: str | None,
    context: str,
    configured_label: str,
) -> str:
    """Return the single instrument symbol two sources agree on.

    Both sources are consulted rather than short-circuiting on the first, so a
    disagreement is reported instead of being silently resolved in favour of
    one of them: two sources contradicting each other about instrument identity
    is a defect worth stopping for.

    Args:
        embedded: The symbol carried by the archived record, if any.
        configured: The symbol supplied by the caller, if any.
        context: Where the record came from, for the error message.
        configured_label: How to name the caller's source in the error message.

    Raises:
        ProviderDecodeError: if the sources conflict, or neither resolves.
    """
    embedded_symbol = (embedded or "").strip()
    configured_symbol = (configured or "").strip()

    if embedded_symbol and configured_symbol and embedded_symbol != configured_symbol:
        raise ProviderDecodeError(
            f"{context} has conflicting instrument identity: the export says "
            f"{embedded_symbol!r} but {configured_label} says {configured_symbol!r}"
        )

    resolved = embedded_symbol or configured_symbol
    if not resolved:
        raise ProviderDecodeError(
            f"{context} has no usable instrument symbol and no {configured_label}; "
            f"the instrument cannot be identified"
        )
    return resolved


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
