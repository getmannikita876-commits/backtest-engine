"""Interface-only ThetaData provider stub.

Like the Databento stub, this module ships the *shape* of the integration and
none of its behaviour: no API keys, no network calls, no vendor SDK.

:meth:`ThetaDataMarketDataProvider.fetch` raises
:class:`ProviderNotConfiguredError` rather than returning an empty iterator, so
an unimplemented vendor can never be mistaken for a vendor that returned no
data.

Scope note
----------
ThetaData's primary value to this project is options data. Options are out of
scope for the current phase, and the record types below are restricted to the
instrument kinds the domain layer models today. Options records will require
new domain contracts and their own record types before this provider can
declare them — the stub does not pre-declare capabilities the rest of the
system cannot yet represent.
"""

from __future__ import annotations

from typing import Final

from quant_research_terminal.data_import.contracts import ImportRecordType
from quant_research_terminal.data_import.providers.provider import (
    ProviderCapabilities,
    ProviderNotConfiguredError,
    ProviderRequest,
    RecordStream,
)

PROVIDER_NAME: Final = "thetadata"

#: Record types the future integration is expected to serve, limited to the
#: kinds the domain layer models today.
SUPPORTED_RECORD_TYPES: Final[frozenset[ImportRecordType]] = frozenset(
    {ImportRecordType.TRADE, ImportRecordType.QUOTE, ImportRecordType.BAR}
)


class ThetaDataMarketDataProvider:
    """Placeholder that satisfies :class:`MarketDataProvider` without behaviour."""

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Return the declared, not-yet-implemented capability set."""
        return ProviderCapabilities(
            provider_name=PROVIDER_NAME,
            record_types=SUPPORTED_RECORD_TYPES,
            supports_time_window=True,
            requires_credentials=True,
            is_implemented=False,
        )

    def fetch(self, request: ProviderRequest) -> RecordStream:
        """Always raise: the ThetaData integration is not implemented.

        Raises:
            ProviderNotConfiguredError: unconditionally.
        """
        raise ProviderNotConfiguredError(
            f"provider {PROVIDER_NAME!r} is an interface-only stub in this phase; "
            f"cannot serve record type {request.record_type.value!r}"
        )
