"""Data import layer.

Stages compose in one direction::

    Provider -> Raw Records -> Validation -> Normalization -> Domain -> Storage

Each stage owns exactly one concern: providers decode, validators judge,
normalizers convert, and :mod:`~quant_research_terminal.data_import.pipeline`
orchestrates without owning any rule of its own.
"""

from __future__ import annotations

from quant_research_terminal.data_import.contracts import (
    DataImportSource,
    DuplicatePolicy,
    ImportBatch,
    ImportRecordType,
    ValidationIssue,
    ValidationIssueCode,
    ValidationReport,
    ValidationSeverity,
)
from quant_research_terminal.data_import.event_order import (
    EVENT_TYPE_PRECEDENCE,
    event_ordering_key,
    event_type_precedence,
)
from quant_research_terminal.data_import.instrument_semantics import (
    InstrumentSymbolViolation,
    check_instrument_symbol,
    require_instrument_symbol,
)
from quant_research_terminal.data_import.normalization import (
    DefaultRecordNormalizer,
    DomainRecord,
    RecordNormalizer,
)
from quant_research_terminal.data_import.numeric_semantics import is_decimal_like, to_decimal
from quant_research_terminal.data_import.pipeline import (
    group_issues_by_row,
    validate_import_batch,
)
from quant_research_terminal.data_import.providers import (
    KNOWN_TRADE_SIDES,
    BarTimestampMeaning,
    CsvMarketDataProvider,
    DatabentoMarketDataProvider,
    DatabentoSchema,
    DatabentoTimestampField,
    MarketDataProvider,
    ProviderCapabilities,
    ProviderCapabilityError,
    ProviderDecodeError,
    ProviderError,
    ProviderNotConfiguredError,
    ProviderRequest,
    RecordStream,
    SubMicrosecondPolicy,
    ThetaDataMarketDataProvider,
    ThetaDataSchema,
    bar_availability_timestamp,
    databento_record_type,
    thetadata_record_type,
)
from quant_research_terminal.data_import.raw_record import (
    RawRecord,
    import_batch_to_raw_records,
    raw_records_to_import_batch,
)
from quant_research_terminal.data_import.record_fields import (
    decimal_fields,
    required_fields,
)
from quant_research_terminal.data_import.time_semantics import (
    TimestampStatus,
    UtcTimestampError,
    classify_timestamp,
    require_utc,
)
from quant_research_terminal.data_import.validation import (
    BAR_IDENTITY_FIELDS,
    DuplicateValidator,
    InstrumentSymbolValidator,
    OrderingValidator,
    RecordBatchValidator,
    SchemaValidator,
    TimestampValidator,
    ValidationPipeline,
    ValueValidator,
    batch_validation_pipeline,
    default_validation_pipeline,
    record_identity,
)

__all__ = [
    "BAR_IDENTITY_FIELDS",
    "EVENT_TYPE_PRECEDENCE",
    "KNOWN_TRADE_SIDES",
    "BarTimestampMeaning",
    "CsvMarketDataProvider",
    "DataImportSource",
    "DatabentoMarketDataProvider",
    "DatabentoSchema",
    "DatabentoTimestampField",
    "DefaultRecordNormalizer",
    "DomainRecord",
    "DuplicatePolicy",
    "DuplicateValidator",
    "ImportBatch",
    "ImportRecordType",
    "InstrumentSymbolValidator",
    "InstrumentSymbolViolation",
    "MarketDataProvider",
    "OrderingValidator",
    "ProviderCapabilities",
    "ProviderCapabilityError",
    "ProviderDecodeError",
    "ProviderError",
    "ProviderNotConfiguredError",
    "ProviderRequest",
    "RawRecord",
    "RecordBatchValidator",
    "RecordNormalizer",
    "RecordStream",
    "SchemaValidator",
    "SubMicrosecondPolicy",
    "ThetaDataMarketDataProvider",
    "ThetaDataSchema",
    "TimestampStatus",
    "TimestampValidator",
    "UtcTimestampError",
    "ValidationIssue",
    "ValidationIssueCode",
    "ValidationPipeline",
    "ValidationReport",
    "ValidationSeverity",
    "ValueValidator",
    "bar_availability_timestamp",
    "batch_validation_pipeline",
    "check_instrument_symbol",
    "classify_timestamp",
    "databento_record_type",
    "decimal_fields",
    "default_validation_pipeline",
    "event_ordering_key",
    "event_type_precedence",
    "group_issues_by_row",
    "import_batch_to_raw_records",
    "is_decimal_like",
    "raw_records_to_import_batch",
    "record_identity",
    "require_instrument_symbol",
    "require_utc",
    "required_fields",
    "thetadata_record_type",
    "to_decimal",
    "validate_import_batch",
]
