from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from quant_research_terminal.domain.common import _BaseDomainModel
from quant_research_terminal.domain.time import validate_utc_datetime


class Instrument(_BaseDomainModel):
    """Immutable descriptive record for a symbol. **Not** an identity model.

    This is a Phase 1.1 placeholder and predates the futures identity model. It
    describes a symbol with free-text attributes; it does not identify a
    specific listed contract, and its ``symbol`` and ``exchange`` fields are
    unvalidated free text rather than canonical identity components.

    Canonical identity of a listed futures contract is
    :class:`~quant_research_terminal.domain.futures_contract.FuturesContractId`,
    whose ``venue`` is a strictly validated namespace token and is *not* the
    same concept as this model's ``exchange``. Nothing in the platform consumes
    this model today. See ADR-009.
    """

    symbol: str = Field(min_length=1)
    exchange: str = Field(min_length=1)
    asset_class: str = Field(min_length=1)
    currency: str = Field(min_length=1)
    created_at: datetime = Field(...)

    @field_validator("created_at", mode="before")
    @classmethod
    def validate_created_at(cls, value: object) -> datetime:
        # Instrument creation time must be timezone-aware UTC to preserve deterministic ordering.
        return validate_utc_datetime(value)
