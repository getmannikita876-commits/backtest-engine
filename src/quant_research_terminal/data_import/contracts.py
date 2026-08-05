"""Data contracts for the import layer.

Everything here is a *contract*: a shape that stages agree on, carrying no
behaviour of its own. Validators, normalizers, providers, and orchestration all
depend on this module, and it depends on none of them. Keeping it free of logic
is what allows the stages to be recomposed without a circular import.

Every model is frozen and strictly validated, so a batch or an issue cannot be
altered after the stage that produced it has returned.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Final, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from quant_research_terminal.data.contracts import SCHEMA_VERSION
from quant_research_terminal.domain.models import Bar, Quote, Trade


class ImportRecordType(StrEnum):
    """The market-data record shapes the import layer can carry."""

    TRADE = "TRADE"
    QUOTE = "QUOTE"
    BAR = "BAR"


class ValidationSeverity(StrEnum):
    """How much weight a validation issue carries.

    ``ERROR`` and ``FATAL`` cause a row to be rejected. ``WARNING`` marks a
    condition the caller's policy decides about, such as a duplicate. ``INFO``
    records an observation that is legitimate but worth surfacing.
    """

    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    FATAL = "FATAL"


class ValidationIssueCode(StrEnum):
    """Machine-readable identifier for a validation finding.

    Codes are stable: operators and downstream tooling match on them, so a code
    is added rather than repurposed when a new condition needs reporting.
    """

    EMPTY_BATCH = "empty_batch"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    UNKNOWN_FIELD = "unknown_field"
    NAIVE_DATETIME = "naive_datetime"
    NON_DATETIME_TIMESTAMP = "non_datetime_timestamp"
    NON_UTC_TIMESTAMP = "non_utc_timestamp"
    NEGATIVE_VALUE = "negative_value"
    NON_DECIMAL_PRICE = "non_decimal_price"
    BID_ASK_INVERSION = "bid_ask_inversion"
    INVALID_OHLC = "invalid_ohlc"
    INVALID_BAR_INTERVAL = "invalid_bar_interval"
    INVALID_TRADE_SIDE = "invalid_trade_side"
    DUPLICATE_ROW = "duplicate_row"
    DESCENDING_TIMESTAMP = "descending_timestamp"
    AMBIGUOUS_TIMESTAMP_ORDER = "ambiguous_timestamp_order"


class DuplicatePolicy(StrEnum):
    """What to do when two records share an identity.

    Detecting a duplicate and deciding its fate are separate concerns: the
    validation stage only reports repeats, and this policy — supplied by the
    caller on the batch — determines which copy survives.

    ``REJECT`` and ``KEEP_FIRST`` both retain the earliest occurrence; they
    differ only in intent, and are kept distinct so a caller can express
    whether keeping the first copy is a deliberate choice or a fallback.
    """

    REJECT = "reject"
    KEEP_FIRST = "keep_first"
    KEEP_LAST = "keep_last"


class ValidationIssue(BaseModel):
    """One finding about one row, or about the batch as a whole.

    Attributes:
        severity: Weight of the finding.
        code: Stable machine-readable identifier.
        row_index: Source position of the offending row, or ``None`` when the
            finding concerns the batch rather than a row.
        field_name: The offending field, when the finding is attributable to one.
        message: Human-readable explanation.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    severity: ValidationSeverity
    code: ValidationIssueCode
    row_index: int | None = None
    field_name: str | None = None
    message: str


class ValidationReport(BaseModel):
    """Immutable summary of one batch's processing outcome.

    ``success`` is true only when no error or fatal issue was raised. Warnings
    do not fail a batch: a duplicate handled by policy is a resolved condition,
    not a defect.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    total_rows: int
    accepted_rows: int
    rejected_rows: int
    warning_count: int
    error_count: int
    fatal_count: int
    issues: tuple[ValidationIssue, ...]
    success: bool

    @property
    def issue_count(self) -> int:
        """Return the total number of issues recorded."""
        return len(self.issues)


class ImportBatch(BaseModel):
    """A provider-neutral set of rows submitted for import together.

    Attributes:
        record_type: The shape every row in the batch describes.
        rows: The rows themselves, as field-name mappings.
        schema_version: Storage schema the rows were produced against.
        strict_fields: Whether fields beyond the required set are reported.
        duplicate_policy: Which copy of a duplicate survives. The validated
            field is always a :class:`DuplicatePolicy`; the enum's string
            values are accepted at the external boundary and coerced, so
            configuration loaded from YAML or JSON needs no special handling.
            An unrecognised string is rejected rather than defaulted.
        source_name: Optional provenance label.
        source_metadata: Optional provenance detail.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    record_type: ImportRecordType
    rows: tuple[Mapping[str, Any], ...] = Field(default_factory=tuple)
    schema_version: int = SCHEMA_VERSION
    strict_fields: bool = True
    duplicate_policy: DuplicatePolicy = DuplicatePolicy.REJECT
    source_name: str | None = None
    source_metadata: Mapping[str, Any] | None = None

    @field_validator("duplicate_policy", mode="before")
    @classmethod
    def _coerce_duplicate_policy(cls, value: object) -> object:
        # Strict mode would otherwise reject the plain strings that serialized
        # configuration naturally carries.
        if isinstance(value, str) and not isinstance(value, DuplicatePolicy):
            try:
                return DuplicatePolicy(value.lower())
            except ValueError as exc:
                accepted = ", ".join(policy.value for policy in DuplicatePolicy)
                raise ValueError(
                    f"unsupported duplicate policy {value!r}; expected one of: {accepted}"
                ) from exc
        return value


class DataImportSource(Protocol):
    """A source that produces provider-neutral rows in one call.

    This is the eager counterpart to
    :class:`~quant_research_terminal.data_import.providers.provider.MarketDataProvider`,
    which streams. It suits sources small enough to materialise whole.
    """

    def fetch_rows(self) -> Sequence[Mapping[str, Any]]:
        """Return every row this source can supply."""
        ...


DomainModel = Trade | Quote | Bar


class ImportValidationError(ValueError):
    """Raised when an import batch cannot be validated safely."""


VALIDATION_ORDER: Final[tuple[ImportRecordType, ...]] = (
    ImportRecordType.TRADE,
    ImportRecordType.QUOTE,
    ImportRecordType.BAR,
)
