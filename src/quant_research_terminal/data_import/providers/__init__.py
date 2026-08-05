"""Vendor-neutral market-data providers.

Every provider satisfies :class:`MarketDataProvider`, so the import pipeline is
written once against the interface and never against a vendor. Only the CSV
provider has behaviour in this phase; the vendor modules are interface-only
stubs that carry no credentials and make no network calls.
"""

from __future__ import annotations

from quant_research_terminal.data_import.providers.csv_provider import CsvMarketDataProvider
from quant_research_terminal.data_import.providers.databento_provider import (
    DatabentoMarketDataProvider,
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
from quant_research_terminal.data_import.providers.thetadata_provider import (
    ThetaDataMarketDataProvider,
)

# Re-exported for convenience: RawRecord is the providers' output type, but it
# is defined outside this package so validation can consume it without
# importing any vendor code.
from quant_research_terminal.data_import.raw_record import RawRecord

__all__ = [
    "CsvMarketDataProvider",
    "DatabentoMarketDataProvider",
    "MarketDataProvider",
    "ProviderCapabilities",
    "ProviderCapabilityError",
    "ProviderDecodeError",
    "ProviderError",
    "ProviderNotConfiguredError",
    "ProviderRequest",
    "RawRecord",
    "RecordStream",
    "ThetaDataMarketDataProvider",
]
