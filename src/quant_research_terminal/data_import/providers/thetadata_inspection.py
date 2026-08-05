"""Inspect a user-supplied ThetaData export and report what is actually in it.

The archived ThetaData decoder was written without a real vendor file to read,
so every column name, unit, and encoding it relies on is an assumption. This
module is how those assumptions get settled: point it at a small real export
and it reports what the file contains, so a human can compare that against
``docs/data-import.md`` and confirm or correct the decoder.

It is a **diagnostic**, not part of the import pipeline. Nothing here decodes
into a :class:`RawRecord`, and nothing here is used at import time.

Two rules govern the output:

* **It never guesses.** Findings are phrased as what was observed — "every
  value is an 8-digit integer" — not as conclusions about what the column
  means. Whether an 8-digit integer is a ``YYYYMMDD`` date is a judgement for
  the reader holding the vendor's documentation.
* **It never repairs.** The file is opened read-only, values are counted and
  echoed verbatim, and nothing is normalized, coerced, or written back.

Run it as::

    python -m quant_research_terminal.data_import.providers.thetadata_inspection FILE
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict

from quant_research_terminal.data_import.providers.thetadata_decoding import (
    COMMON_REQUIRED_COLUMNS,
    MILLISECONDS_PER_DAY,
    OPTIONAL_COLUMNS,
    SCHEMA_REQUIRED_COLUMNS,
    ThetaDataSchema,
)

#: Rows read before the inspection stops. A verification sample does not need
#: to be large, and a bound keeps the tool usable on a big export.
DEFAULT_MAX_ROWS: Final = 1_000

#: Distinct raw values echoed per column, so low-cardinality columns (side
#: codes, exchange codes, condition flags) reveal their vocabulary.
DISTINCT_EXAMPLE_LIMIT: Final = 12

#: Lower bound for calling an integer "large enough that it may be a scaled
#: fixed-point value rather than a plain count". Purely a reporting threshold.
LARGE_INTEGER_THRESHOLD: Final = 1_000_000

#: Lower bound below which a column is too small to be informative as a
#: candidate intraday offset. Purely a reporting threshold.
INTRADAY_MAGNITUDE_THRESHOLD: Final = 1_000

#: Byte-order mark. A file that begins with one gives its first column a name
#: the decoder will not match, so its presence is reported rather than stripped.
BYTE_ORDER_MARK: Final = "﻿"


class ColumnObservation(BaseModel):
    """What was seen in one column, with no interpretation applied."""

    model_config = ConfigDict(frozen=True, strict=True)

    name: str
    values_seen: int
    empty_count: int
    integer_count: int
    decimal_count: int
    text_count: int
    zero_count: int
    minimum_integer: int | None = None
    maximum_integer: int | None = None
    maximum_decimal_places: int | None = None
    distinct_examples: tuple[str, ...] = ()
    all_values_calendar_shaped: bool = False

    @property
    def is_all_integer(self) -> bool:
        """Return whether every non-empty value parsed as an integer."""
        return self.values_seen > 0 and self.integer_count == self.values_seen

    @property
    def is_all_numeric(self) -> bool:
        """Return whether every non-empty value parsed as a number."""
        return self.values_seen > 0 and self.text_count == 0 and self.empty_count == 0


class SchemaExpectation(BaseModel):
    """How the file's header compares with one schema's required columns."""

    model_config = ConfigDict(frozen=True, strict=True)

    schema_name: str
    present_required_columns: tuple[str, ...]
    missing_required_columns: tuple[str, ...]

    @property
    def is_satisfied(self) -> bool:
        """Return whether every required column for this schema is present."""
        return not self.missing_required_columns


class ExportInspection(BaseModel):
    """A read-only report describing one archived export."""

    model_config = ConfigDict(frozen=True, strict=True)

    path: str
    header: tuple[str, ...]
    rows_inspected: int
    truncated: bool
    columns: tuple[ColumnObservation, ...]
    recognised_columns: tuple[str, ...]
    unrecognised_columns: tuple[str, ...]
    schema_expectations: tuple[SchemaExpectation, ...]
    findings: tuple[str, ...]

    @property
    def satisfied_schemas(self) -> tuple[str, ...]:
        """Return the schemas whose required columns are all present."""
        return tuple(
            expectation.schema_name
            for expectation in self.schema_expectations
            if expectation.is_satisfied
        )


class _ColumnAccumulator:
    """Mutable tally for one column while the file is being read."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.values_seen = 0
        self.empty_count = 0
        self.integer_count = 0
        self.decimal_count = 0
        self.text_count = 0
        self.zero_count = 0
        self.minimum_integer: int | None = None
        self.maximum_integer: int | None = None
        self.maximum_decimal_places: int | None = None
        self.distinct_examples: list[str] = []
        self.calendar_shaped_count = 0

    def observe(self, raw: str) -> None:
        """Record one raw cell exactly as it appeared."""
        self.values_seen += 1
        self._remember_example(raw)

        text = raw.strip()
        if not text:
            self.empty_count += 1
            return

        try:
            number = Decimal(text)
        except InvalidOperation:
            self.text_count += 1
            return

        if number == 0:
            self.zero_count += 1

        exponent = number.as_tuple().exponent
        decimal_places = -exponent if isinstance(exponent, int) and exponent < 0 else 0
        if decimal_places == 0:
            self.integer_count += 1
            integer_value = int(number)
            self.minimum_integer = (
                integer_value
                if self.minimum_integer is None
                else min(self.minimum_integer, integer_value)
            )
            self.maximum_integer = (
                integer_value
                if self.maximum_integer is None
                else max(self.maximum_integer, integer_value)
            )
            if _is_calendar_shaped(integer_value):
                self.calendar_shaped_count += 1
        else:
            self.decimal_count += 1

        self.maximum_decimal_places = (
            decimal_places
            if self.maximum_decimal_places is None
            else max(self.maximum_decimal_places, decimal_places)
        )

    def _remember_example(self, raw: str) -> None:
        if len(self.distinct_examples) >= DISTINCT_EXAMPLE_LIMIT:
            return
        if raw not in self.distinct_examples:
            self.distinct_examples.append(raw)

    def finish(self) -> ColumnObservation:
        """Freeze the tally into an immutable observation."""
        return ColumnObservation(
            name=self.name,
            values_seen=self.values_seen,
            empty_count=self.empty_count,
            integer_count=self.integer_count,
            decimal_count=self.decimal_count,
            text_count=self.text_count,
            zero_count=self.zero_count,
            minimum_integer=self.minimum_integer,
            maximum_integer=self.maximum_integer,
            maximum_decimal_places=self.maximum_decimal_places,
            distinct_examples=tuple(self.distinct_examples),
            all_values_calendar_shaped=(
                self.integer_count > 0 and self.calendar_shaped_count == self.integer_count
            ),
        )


def _is_calendar_shaped(value: int) -> bool:
    """Return whether an integer has ``YYYYMMDD`` structure.

    Eight digits alone is not enough — a millisecond offset can also be eight
    digits. Requiring a month in 01-12 and a day in 01-31 is what makes this a
    useful discriminator. It remains an observation about shape, not a claim
    that the column is a date.
    """
    if not 1_000_01_01 <= value <= 9999_12_31:
        return False
    month, day = divmod(value % 10_000, 100)
    return 1 <= month <= 12 and 1 <= day <= 31


def _known_columns() -> frozenset[str]:
    known: set[str] = set(COMMON_REQUIRED_COLUMNS) | set(OPTIONAL_COLUMNS)
    for required in SCHEMA_REQUIRED_COLUMNS.values():
        known.update(required)
    return frozenset(known)


def _looks_like_yyyymmdd(observation: ColumnObservation) -> bool:
    """Return whether every value has ``YYYYMMDD`` *structure*.

    Checking structure — a plausible month and day, not merely eight digits —
    is what stops a millisecond offset from being reported as a candidate date.
    """
    return observation.is_all_integer and observation.all_values_calendar_shaped


def _within_day_milliseconds(observation: ColumnObservation) -> bool:
    """Return whether every value could be a millisecond offset into a day.

    Values below :data:`INTRADAY_MAGNITUDE_THRESHOLD` are excluded. A column
    whose largest value is a handful of units is consistent with almost any
    interpretation, so reporting it would add noise rather than evidence. This
    is a reporting threshold, not a claim about the column.
    """
    if not observation.is_all_integer:
        return False
    minimum = observation.minimum_integer
    maximum = observation.maximum_integer
    if minimum is None or maximum is None:
        return False
    if maximum < INTRADAY_MAGNITUDE_THRESHOLD:
        return False
    return minimum >= 0 and maximum < MILLISECONDS_PER_DAY


def _timestamp_findings(observations: Sequence[ColumnObservation]) -> list[str]:
    """Report which columns are *consistent with* a timestamp representation."""
    findings: list[str] = []
    for observation in observations:
        if _looks_like_yyyymmdd(observation):
            findings.append(
                f"column {observation.name!r}: every value is an 8-digit integer in "
                f"[{observation.minimum_integer}, {observation.maximum_integer}], "
                f"consistent with a YYYYMMDD date — not confirmed to be one"
            )
        if _within_day_milliseconds(observation):
            findings.append(
                f"column {observation.name!r}: every value is an integer in "
                f"[{observation.minimum_integer}, {observation.maximum_integer}], "
                f"within one day in milliseconds — consistent with a "
                f"milliseconds-since-midnight offset, but seconds or microseconds "
                f"cannot be ruled out from range alone"
            )
    return findings


def _numeric_findings(observations: Sequence[ColumnObservation]) -> list[str]:
    """Report how numeric columns are written, without deciding what they mean.

    Columns already reported as candidate temporal representations are skipped:
    a date or an offset is expected to be a large integer, so repeating the
    scale-factor caution for them would bury the columns where it matters.
    """
    findings: list[str] = []
    for observation in observations:
        if _looks_like_yyyymmdd(observation) or _within_day_milliseconds(observation):
            continue
        if observation.decimal_count:
            findings.append(
                f"column {observation.name!r}: {observation.decimal_count} of "
                f"{observation.values_seen} values carry a decimal point, up to "
                f"{observation.maximum_decimal_places} decimal places — consistent "
                f"with decimal text rather than scaled integers"
            )
        elif (
            observation.is_all_integer
            and observation.maximum_integer is not None
            and observation.maximum_integer >= LARGE_INTEGER_THRESHOLD
        ):
            findings.append(
                f"column {observation.name!r}: every value is an integer and the "
                f"largest is {observation.maximum_integer}; if this column is a "
                f"price, a scale factor may be in use — confirm against the vendor "
                f"schema before decoding it as decimal text"
            )
    return findings


def _header_findings(header: Sequence[str]) -> list[str]:
    """Report header-level defects that would break column matching.

    A byte-order mark is reported rather than stripped: silently removing it
    would hide the fact that the decoder — which does not strip it — will fail
    to recognise the first column of this file.
    """
    findings: list[str] = []
    for name in header:
        if name.startswith(BYTE_ORDER_MARK):
            findings.append(
                f"header: the first column name begins with a byte-order mark, so it "
                f"reads as {name!r} rather than {name.lstrip(BYTE_ORDER_MARK)!r}; the "
                f"decoder matches column names literally and will not recognise it. "
                f"Re-save the file as UTF-8 without a BOM, or read it with the "
                f"'utf-8-sig' encoding"
            )
    return findings


def _sentinel_findings(observations: Sequence[ColumnObservation]) -> list[str]:
    """Report values that may be sentinels, without asserting that they are."""
    findings: list[str] = []
    for observation in observations:
        if observation.zero_count:
            findings.append(
                f"column {observation.name!r}: {observation.zero_count} of "
                f"{observation.values_seen} values are zero — candidate 'no value' "
                f"sentinel; the import contract rejects non-positive prices and sizes"
            )
        if observation.empty_count:
            findings.append(
                f"column {observation.name!r}: {observation.empty_count} of "
                f"{observation.values_seen} values are empty"
            )
        if observation.text_count and not observation.is_all_numeric:
            findings.append(
                f"column {observation.name!r}: {observation.text_count} of "
                f"{observation.values_seen} values are non-numeric text; examples "
                f"{list(observation.distinct_examples)}"
            )
    return findings


def inspect_thetadata_export(
    path: Path, *, encoding: str = "utf-8", max_rows: int = DEFAULT_MAX_ROWS
) -> ExportInspection:
    """Read an archived export and report what it contains.

    The file is opened read-only and nothing is written, coerced, or repaired.
    Values are counted and echoed exactly as they appear.

    Args:
        path: The export to inspect.
        encoding: Text encoding used to read the file.
        max_rows: Stop after this many data rows.

    Returns:
        A report describing the header, per-column value shapes, how the header
        compares with each decoder schema's requirements, and neutral findings
        about timestamp, numeric, and sentinel representations.

    Raises:
        ValueError: if the file has no header row. Nothing can be said about a
            file whose columns are unnamed.
    """
    accumulators: list[_ColumnAccumulator] = []
    rows_inspected = 0
    truncated = False

    with path.open("r", encoding=encoding, newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header is None:
            raise ValueError(f"{path} contains no header row")

        accumulators = [_ColumnAccumulator(name) for name in header]
        for row in reader:
            if rows_inspected >= max_rows:
                truncated = True
                break
            for accumulator, raw in zip(accumulators, row, strict=False):
                accumulator.observe(raw)
            rows_inspected += 1

    observations = tuple(accumulator.finish() for accumulator in accumulators)
    known = _known_columns()
    header_names = tuple(header)

    expectations = tuple(
        SchemaExpectation(
            schema_name=schema.value,
            present_required_columns=tuple(
                column
                for column in (*COMMON_REQUIRED_COLUMNS, *SCHEMA_REQUIRED_COLUMNS[schema])
                if column in header_names
            ),
            missing_required_columns=tuple(
                column
                for column in (*COMMON_REQUIRED_COLUMNS, *SCHEMA_REQUIRED_COLUMNS[schema])
                if column not in header_names
            ),
        )
        for schema in ThetaDataSchema
    )

    findings = (
        *_header_findings(header_names),
        *_timestamp_findings(observations),
        *_numeric_findings(observations),
        *_sentinel_findings(observations),
    )

    return ExportInspection(
        path=str(path),
        header=header_names,
        rows_inspected=rows_inspected,
        truncated=truncated,
        columns=observations,
        recognised_columns=tuple(name for name in header_names if name in known),
        unrecognised_columns=tuple(name for name in header_names if name not in known),
        schema_expectations=expectations,
        findings=findings,
    )


def format_inspection_report(inspection: ExportInspection) -> str:
    """Render an inspection as readable text.

    Every line describes an observation. None of them asserts what a column
    means: that judgement belongs to whoever is holding the vendor's schema.
    """
    lines: list[str] = [
        f"ThetaData export inspection: {inspection.path}",
        "",
        f"Rows inspected: {inspection.rows_inspected}"
        + (" (truncated)" if inspection.truncated else ""),
        f"Columns ({len(inspection.header)}): {', '.join(inspection.header)}",
        "",
        "Decoder column coverage",
        f"  recognised:   {', '.join(inspection.recognised_columns) or '(none)'}",
        f"  unrecognised: {', '.join(inspection.unrecognised_columns) or '(none)'}",
        "",
        "Schema requirements",
    ]

    for expectation in inspection.schema_expectations:
        status = "satisfied" if expectation.is_satisfied else "missing columns"
        lines.append(f"  {expectation.schema_name}: {status}")
        if expectation.missing_required_columns:
            lines.append(f"    missing: {', '.join(expectation.missing_required_columns)}")

    lines.extend(["", "Column values"])
    for column in inspection.columns:
        lines.append(
            f"  {column.name}: seen={column.values_seen} int={column.integer_count} "
            f"dec={column.decimal_count} text={column.text_count} "
            f"empty={column.empty_count} zero={column.zero_count}"
        )
        if column.distinct_examples:
            lines.append(f"    examples: {', '.join(column.distinct_examples)}")

    lines.extend(["", "Findings (observations only — none of these are conclusions)"])
    if inspection.findings:
        lines.extend(f"  - {finding}" for finding in inspection.findings)
    else:
        lines.append("  (none)")

    lines.extend(
        [
            "",
            "This report describes the file only. It does not verify the decoder's",
            "assumptions; compare it against the vendor's published schema.",
        ]
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the inspection from the command line."""
    parser = argparse.ArgumentParser(
        prog="thetadata-inspection",
        description=(
            "Report what an archived ThetaData export contains. Read-only: "
            "nothing is decoded, repaired, or written."
        ),
    )
    parser.add_argument("path", type=Path, help="the export file to inspect")
    parser.add_argument("--encoding", default="utf-8", help="text encoding (default: utf-8)")
    parser.add_argument(
        "--max-rows",
        type=int,
        default=DEFAULT_MAX_ROWS,
        help=f"rows to inspect (default: {DEFAULT_MAX_ROWS})",
    )
    arguments = parser.parse_args(argv)

    try:
        inspection = inspect_thetadata_export(
            arguments.path, encoding=arguments.encoding, max_rows=arguments.max_rows
        )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(format_inspection_report(inspection))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via main()
    raise SystemExit(main())
