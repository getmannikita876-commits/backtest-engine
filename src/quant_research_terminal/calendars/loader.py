"""Strict loader for declarative TOML calendar definitions.

The loader is a translator, not an interpreter: every schedule fact lives in
the TOML data, every rule of meaning lives in the domain models, and this
module only maps one onto the other. It is deliberately intolerant:

* unknown keys are errors, not extensions — a misspelled key must fail, never
  silently vanish;
* nothing has a default — a missing fact is a missing fact;
* nothing is normalized — the definition says what its author wrote.

TOML was chosen over YAML because Python 3.12 parses it with the standard
library (``tomllib``), deterministically, with native date/time/datetime
values and zero new dependencies. The parse result preserves file order, so
window ordering in the file is the ordering the models validate.
"""

from __future__ import annotations

import tomllib
from importlib import resources
from typing import Any, Final

from quant_research_terminal.domain.calendar_definition import (
    CalendarDefinition,
    DatedException,
    EvidenceRecord,
    ExceptionWindow,
    TimezoneProbe,
    WeeklyWindowRule,
)
from quant_research_terminal.domain.exchange_calendar import (
    CalendarDefinitionError,
    CalendarId,
    CalendarVersion,
    ExchangeTradingState,
    VerificationStatus,
)

#: The declarative format this loader understands. A definition naming any
#: other format string is rejected outright rather than best-effort parsed.
SCHEMA_TOKEN: Final[str] = "qrt-calendar-definition/1"

#: Package resource name of the shipped CME equity-index calendar, version 1.
CME_EQUITY_INDEX_V1_RESOURCE: Final[str] = "cme_equity_index_v1.toml"

_DEFINITIONS_PACKAGE: Final[str] = "quant_research_terminal.calendars"


def load_cme_equity_index_v1() -> CalendarDefinition:
    """Load the packaged CME equity-index calendar definition, version 1."""
    return load_packaged_definition(CME_EQUITY_INDEX_V1_RESOURCE)


def load_packaged_definition(resource_name: str) -> CalendarDefinition:
    """Load and validate a packaged TOML calendar definition by file name."""
    if (
        not resource_name
        or "/" in resource_name
        or "\\" in resource_name
        or resource_name.startswith(".")
    ):
        # A packaged definition is a file name inside the definitions
        # directory, never a path; traversal is refused, not resolved.
        raise CalendarDefinitionError(
            f"calendar definition resource must be a bare file name; got {resource_name!r}"
        )
    resource = resources.files(_DEFINITIONS_PACKAGE) / "definitions" / resource_name
    try:
        text = resource.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as error:
        raise CalendarDefinitionError(
            f"calendar definition resource {resource_name!r} could not be read: {error}"
        ) from error
    return parse_calendar_definition(text)


def parse_calendar_definition(text: str) -> CalendarDefinition:
    """Parse and validate one TOML calendar definition document.

    Raises:
        CalendarDefinitionError: for malformed TOML, unknown keys, a wrong
            schema token, or any value the domain models reject.
    """
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise CalendarDefinitionError(f"calendar definition is not valid TOML: {error}") from error

    try:
        return _translate_document(document)
    except (ValueError, TypeError, KeyError) as error:
        raise CalendarDefinitionError(f"calendar definition is invalid: {error}") from error


def _take(table: dict[str, Any], key: str, *, context: str) -> Any:
    if key not in table:
        raise ValueError(f"{context} is missing required key {key!r}")
    return table.pop(key)


def _reject_leftovers(table: dict[str, Any], *, context: str) -> None:
    if table:
        raise ValueError(f"{context} carries unknown keys {sorted(table)!r}")


def _require_table(value: Any, *, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be a table; got {type(value).__name__}")
    return dict(value)


def _require_array(value: Any, *, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{context} must be an array; got {type(value).__name__}")
    return list(value)


def _translate_document(document: dict[str, Any]) -> CalendarDefinition:
    root = dict(document)
    schema = _take(root, "schema", context="the definition document")
    if schema != SCHEMA_TOKEN:
        raise ValueError(
            f"unsupported definition schema {schema!r}; this loader understands "
            f"{SCHEMA_TOKEN!r} only"
        )

    calendar_table = _require_table(
        _take(root, "calendar", context="the definition document"), context="[calendar]"
    )
    calendar_id = CalendarId(token=_take(calendar_table, "id", context="[calendar]"))
    version = CalendarVersion(number=_take(calendar_table, "version", context="[calendar]"))
    timezone_name = _take(calendar_table, "timezone", context="[calendar]")
    first_trading_date = _take(calendar_table, "first_trading_date", context="[calendar]")
    last_trading_date = _take(calendar_table, "last_trading_date", context="[calendar]")
    _reject_leftovers(calendar_table, context="[calendar]")

    # Both sections are REQUIRED keys even when empty: `timezone_probe = []`
    # and `exception = []` are deliberate statements an auditor can see,
    # whereas a missing section is indistinguishable from an accidental
    # deletion — and a definition that silently loses its tzdb probes loses
    # exactly the protection they exist to provide (defect D5).
    probes = tuple(
        _translate_probe(_require_table(entry, context="[[timezone_probe]]"), index)
        for index, entry in enumerate(
            _require_array(
                _take(root, "timezone_probe", context="the definition document"),
                context="timezone_probe",
            )
        )
    )
    evidence = tuple(
        _translate_evidence(_require_table(entry, context="[[evidence]]"), index)
        for index, entry in enumerate(
            _require_array(
                _take(root, "evidence", context="the definition document"), context="evidence"
            )
        )
    )

    weekly_table = _require_table(
        _take(root, "weekly", context="the definition document"), context="[weekly]"
    )
    weekly_rules = tuple(
        (
            weekday,
            tuple(
                _translate_weekly_rule(
                    _require_table(entry, context=f"[[weekly.{weekday}]]"), weekday, index
                )
                for index, entry in enumerate(_require_array(rules, context=f"weekly.{weekday}"))
            ),
        )
        for weekday, rules in weekly_table.items()
    )

    exceptions = tuple(
        _translate_exception(_require_table(entry, context="[[exception]]"), index)
        for index, entry in enumerate(
            _require_array(
                _take(root, "exception", context="the definition document"),
                context="exception",
            )
        )
    )
    _reject_leftovers(root, context="the definition document")

    return CalendarDefinition(
        calendar_id=calendar_id,
        version=version,
        timezone_name=timezone_name,
        first_trading_date=first_trading_date,
        last_trading_date=last_trading_date,
        weekly_rules=weekly_rules,
        exceptions=exceptions,
        timezone_probes=probes,
        evidence=evidence,
    )


def _translate_probe(table: dict[str, Any], index: int) -> TimezoneProbe:
    context = f"timezone_probe[{index}]"
    probe = TimezoneProbe(
        utc_instant=_take(table, "utc_instant", context=context),
        expected_utc_offset_minutes=_take(table, "expected_utc_offset_minutes", context=context),
    )
    _reject_leftovers(table, context=context)
    return probe


def _translate_evidence(table: dict[str, Any], index: int) -> EvidenceRecord:
    context = f"evidence[{index}]"
    record = EvidenceRecord(
        evidence_id=_take(table, "id", context=context),
        source_type=_take(table, "source_type", context=context),
        title=_take(table, "title", context=context),
        source_url=_take(table, "source_url", context=context),
        archive_url=_take(table, "archive_url", context=context),
        accessed=_take(table, "accessed", context=context),
        claim=_take(table, "claim", context=context),
    )
    _reject_leftovers(table, context=context)
    return record


def _translate_weekly_rule(table: dict[str, Any], weekday: str, index: int) -> WeeklyWindowRule:
    context = f"weekly.{weekday}[{index}]"
    rule = WeeklyWindowRule(
        state=ExchangeTradingState(_take(table, "state", context=context)),
        start_day_offset=_take(table, "start_day_offset", context=context),
        start_time=_take(table, "start_time", context=context),
        end_day_offset=_take(table, "end_day_offset", context=context),
        end_time=_take(table, "end_time", context=context),
        provenance_status=VerificationStatus(_take(table, "verification", context=context)),
        evidence_ids=tuple(
            _require_array(_take(table, "evidence", context=context), context=f"{context}.evidence")
        ),
    )
    _reject_leftovers(table, context=context)
    return rule


def _translate_exception(table: dict[str, Any], index: int) -> DatedException:
    context = f"exception[{index}]"
    windows = tuple(
        _translate_exception_window(
            _require_table(entry, context=f"{context}.window[{window_index}]"),
            context,
            window_index,
        )
        for window_index, entry in enumerate(
            _require_array(table.pop("window", []), context=f"{context}.window")
        )
    )
    exception = DatedException(
        trading_date=_take(table, "trading_date", context=context),
        kind=_take(table, "kind", context=context),
        windows=windows,
        provenance_status=VerificationStatus(_take(table, "verification", context=context)),
        evidence_ids=tuple(
            _require_array(_take(table, "evidence", context=context), context=f"{context}.evidence")
        ),
    )
    _reject_leftovers(table, context=context)
    return exception


def _translate_exception_window(
    table: dict[str, Any], parent_context: str, index: int
) -> ExceptionWindow:
    context = f"{parent_context}.window[{index}]"
    window = ExceptionWindow(
        state=ExchangeTradingState(_take(table, "state", context=context)),
        start_date=_take(table, "start_date", context=context),
        start_time=_take(table, "start_time", context=context),
        end_date=_take(table, "end_date", context=context),
        end_time=_take(table, "end_time", context=context),
    )
    _reject_leftovers(table, context=context)
    return window
