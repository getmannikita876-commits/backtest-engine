"""Vendor-neutral market-data providers.

Every provider satisfies :class:`MarketDataProvider`, so the import pipeline is
written once against the interface and never against a vendor. Only the CSV
provider has behaviour in this phase; the vendor modules are interface-only
stubs that carry no credentials and make no network calls.
"""

from __future__ import annotations

from quant_research_terminal.data_import.providers.csv_provider import CsvMarketDataProvider
from quant_research_terminal.data_import.providers.databento_decoding import (
    KNOWN_TRADE_SIDES,
    DatabentoSchema,
    DatabentoTimestampField,
    SubMicrosecondPolicy,
    bar_availability_timestamp,
)
from quant_research_terminal.data_import.providers.databento_provider import (
    DatabentoMarketDataProvider,
    databento_record_type,
)
from quant_research_terminal.data_import.providers.provider import (
    MarketDataProvider,
    ProviderCapabilities,
    ProviderCapabilityError,
    ProviderDecodeError,
    ProviderError,
    ProviderNotConfiguredError,
    ProviderRequest,
    RecordStream,
)
from quant_research_terminal.data_import.providers.thetadata_decoding import (
    BarTimestampMeaning,
    ThetaDataSchema,
)

# thetadata_inspection is deliberately NOT re-exported. It is an operator
# diagnostic rather than pipeline API, and importing it here would also make
# `python -m ...thetadata_inspection` emit a runpy double-import warning.
from quant_research_terminal.data_import.providers.thetadata_provider import (
    ThetaDataMarketDataProvider,
    thetadata_record_type,
)

# Re-exported for convenience: RawRecord is the providers' output type, but it
# is defined outside this package so validation can consume it without
# importing any vendor code.
from quant_research_terminal.data_import.raw_record import RawRecord

__all__ = [
    "KNOWN_TRADE_SIDES",
    "BarTimestampMeaning",
    "CsvMarketDataProvider",
    "DatabentoMarketDataProvider",
    "DatabentoSchema",
    "DatabentoTimestampField",
    "MarketDataProvider",
    "ProviderCapabilities",
    "ProviderCapabilityError",
    "ProviderDecodeError",
    "ProviderError",
    "ProviderNotConfiguredError",
    "ProviderRequest",
    "RawRecord",
    "RecordStream",
    "SubMicrosecondPolicy",
    "ThetaDataMarketDataProvider",
    "ThetaDataSchema",
    "bar_availability_timestamp",
    "databento_record_type",
    "thetadata_record_type",
]
