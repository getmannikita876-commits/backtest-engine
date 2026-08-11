"""Synthetic continuous-series identity and its contract-selection mapping.

A continuous series is a **research construct**: no order was ever filled in
one. This module gives it an identity that is structurally incapable of being
mistaken for something executable, and a definition that answers exactly one
question — *at this UTC instant, which listed contract?*

What a continuous series is **not**, here
-----------------------------------------
**Not a price series.** Phase 2.2 implements no back-adjustment, no ratio or
Panama method, no forward or return-preserving adjustment, and no roll-gap
smoothing — and deliberately no placeholder API for them either. An
``adjust(mode=...)`` that raised ``NotImplementedError`` would advertise a
capability the platform does not have and invite callers to write against it.
Price adjustment is future research-layer work whose method changes what the
returned numbers *mean*, so it needs its own decision.

**Not executable.** :class:`ContinuousSeriesId` is not a
:class:`~quant_research_terminal.domain.futures_contract.FuturesContractId` and
is not a subclass of one. It fails ``require_listed_contract`` automatically,
because that guard is an exact type check rather than a flag — ADR-009 built it
that way precisely so a continuous identity added later would fail it *without
the guard needing to know this module exists*.

The canonical form has four fields and carries a literal ``CONTINUOUS`` marker
in the third::

    CME:ES:CONTINUOUS:ACTIVE

A listed contract's canonical form has exactly three fields, so the two can
never be confused in either direction — including the adversarial case of a
product whose root is literally spelled ``CONTINUOUS``, where the contract form
``CME:CONTINUOUS:M2026`` still has three fields and the series form four. The
discriminator is positional, not textual.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Final, final

from pydantic import field_validator, model_validator

from quant_research_terminal.domain.futures_contract import (
    CANONICAL_SEPARATOR,
    FuturesContractId,
    FuturesProduct,
    Venue,
    _CanonicalModel,
    _split_canonical,
)
from quant_research_terminal.domain.rollover import (
    RollResolver,
    RollSchedule,
    UnsupportedRollTimestampError,
)
from quant_research_terminal.domain.time import epoch_microseconds, validate_utc_datetime

#: The literal marker distinguishing a synthetic series from a listed contract.
CONTINUOUS_MARKER: Final[str] = "CONTINUOUS"

#: Number of fields in a canonical continuous-series identity.
_CONTINUOUS_FIELDS: Final[int] = 4

_SERIES_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Z0-9_]{1,32}")
_HAS_LETTER_PATTERN: Final[re.Pattern[str]] = re.compile("[A-Z]")
_CONTENT_HASH_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")

#: Bumping this is a semantic event, exactly as for the schedule format.
_SERIALIZATION_FORMAT: Final[str] = "qrt-continuous-series/1"


@final
class ContinuousSeriesId(_CanonicalModel):
    """The identity of one synthetic continuous series.

    ``token`` is an **opaque operator-chosen label**, in the same spirit as
    ``Venue.code`` (ADR-009). ``"ACTIVE"`` is a name, not a claim: it does not
    assert front-month selection, volume weighting, or any particular roll
    method. What the series actually maps to is whatever its roll schedule
    says, and nothing else.
    """

    product: FuturesProduct
    token: str

    @field_validator("token")
    @classmethod
    def _validate_token(cls, value: str) -> str:
        if not value:
            raise ValueError("continuous series token must not be empty")
        if _SERIES_TOKEN_PATTERN.fullmatch(value) is None:
            raise ValueError(
                f"continuous series token must consist only of ASCII upper-case letters, "
                f"digits, and underscores (max 32 characters); got {value!r}. Values are "
                f"never trimmed or upper-cased — supply the canonical spelling"
            )
        if _HAS_LETTER_PATTERN.search(value) is None:
            raise ValueError(
                f"continuous series token must contain at least one ASCII letter; got {value!r}"
            )
        if value == CONTINUOUS_MARKER:
            raise ValueError(
                f"continuous series token must not be the marker {CONTINUOUS_MARKER!r} itself; "
                f"it names which series, not that it is one"
            )
        return value

    def canonical(self) -> str:
        """Return the canonical form, ``VENUE:ROOT:CONTINUOUS:TOKEN``."""
        return (
            f"{self.product.canonical()}{CANONICAL_SEPARATOR}"
            f"{CONTINUOUS_MARKER}{CANONICAL_SEPARATOR}{self.token}"
        )

    @classmethod
    def parse(cls, text: str) -> ContinuousSeriesId:
        """Return the series described by a canonical identity string.

        The exact inverse of :meth:`canonical`: it accepts everything
        :meth:`canonical` can emit and nothing else. No vendor alias parses —
        ``ES1!`` and ``NQ1!`` are not canonical continuous identities and never
        become one here.

        Raises:
            ValueError: if ``text`` is not exactly one canonical series form.
        """
        venue_code, root, marker, token = _split_canonical(
            text, expected_fields=_CONTINUOUS_FIELDS, label="continuous series"
        )
        if marker != CONTINUOUS_MARKER:
            raise ValueError(
                f"a canonical continuous series identity carries {CONTINUOUS_MARKER!r} in its "
                f"third field; got {marker!r} in {text!r}"
            )
        return cls(product=FuturesProduct(venue=Venue(code=venue_code), root=root), token=token)


def continuous_series_content_hash(
    *,
    series_id: ContinuousSeriesId,
    roll_schedule_hash: str,
    supported_start: datetime,
    supported_end: datetime,
) -> str:
    """Return the canonical SHA-256 content hash of a continuous series.

    The schedule participates **by its own content hash**, which makes this a
    Merkle link: changing anything about the referenced schedule's semantics
    changes that hash and therefore this one. The schedule is not re-serialized
    inline, because two definitions of one serialization is one too many.
    """
    payload = {
        "format": _SERIALIZATION_FORMAT,
        "series_id": series_id.canonical(),
        "product": series_id.product.canonical(),
        "roll_schedule": roll_schedule_hash,
        "supported_range": [
            epoch_microseconds(supported_start),
            epoch_microseconds(supported_end),
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


@final
class ContinuousSeriesDefinition(_CanonicalModel):
    """A continuous series: an identity, a roll schedule, and a supported range.

    Answers ``UTC instant -> listed FuturesContractId`` and nothing else. Its
    range must lie within the schedule's, so the series can never claim to
    answer where its own schedule refuses to.
    """

    series_id: ContinuousSeriesId
    schedule: RollSchedule
    supported_start: datetime
    supported_end: datetime
    content_hash: str

    @field_validator("supported_start", "supported_end", mode="before")
    @classmethod
    def _validate_instants(cls, value: object) -> datetime:
        return validate_utc_datetime(value)

    @field_validator("content_hash")
    @classmethod
    def _validate_content_hash(cls, value: str) -> str:
        if _CONTENT_HASH_PATTERN.fullmatch(value) is None:
            raise ValueError(
                f"a content hash must be 64 lower-case hexadecimal characters; got {value!r}"
            )
        return value

    @model_validator(mode="after")
    def _validate_series(self) -> ContinuousSeriesDefinition:
        if type(self.schedule) is not RollSchedule:
            raise ValueError(
                f"a continuous series requires a RollSchedule; got {type(self.schedule).__name__}"
            )
        if type(self.series_id) is not ContinuousSeriesId:
            # The hash is built from ``series_id.canonical()``, so a subclass
            # overriding it would let the identity in the hash diverge from the
            # identity in the object.
            raise ValueError(
                f"a continuous series requires an exact ContinuousSeriesId; got "
                f"{type(self.series_id).__name__}"
            )
        if self.supported_start >= self.supported_end:
            raise ValueError("supported_start must be strictly before supported_end")
        if self.series_id.product != self.schedule.product:
            raise ValueError(
                f"series {self.series_id.canonical()} names product "
                f"{self.series_id.product.canonical()}, but its schedule maps "
                f"{self.schedule.product.canonical()}"
            )
        if (
            self.supported_start < self.schedule.supported_start
            or self.supported_end > self.schedule.supported_end
        ):
            raise ValueError(
                f"the series range [{self.supported_start.isoformat()}, "
                f"{self.supported_end.isoformat()}) is not contained in its schedule's range "
                f"[{self.schedule.supported_start.isoformat()}, "
                f"{self.schedule.supported_end.isoformat()}); a series cannot answer where "
                f"its schedule refuses to"
            )
        expected = continuous_series_content_hash(
            series_id=self.series_id,
            roll_schedule_hash=self.schedule.content_hash,
            supported_start=self.supported_start,
            supported_end=self.supported_end,
        )
        if self.content_hash != expected:
            raise ValueError(
                f"content_hash does not match the series; expected {expected}, "
                f"got {self.content_hash!r}"
            )
        return self

    def canonical(self) -> str:
        """Return the canonical identity string of this series."""
        return self.series_id.canonical()

    def active_contract_at(self, timestamp: object) -> FuturesContractId:
        """Return the listed contract this series held at a UTC instant.

        Delegates to the schedule after enforcing the series' own, possibly
        narrower, range.

        For repeated lookups, build a
        :class:`~quant_research_terminal.domain.rollover.RollResolver` over
        ``self.schedule`` once and query that instead: this method constructs a
        resolver per call, because the model is frozen and cannot cache one.
        The convenience is for single answers, not for sweeps.

        Raises:
            UnsupportedRollTimestampError: outside the series' supported range.
        """
        instant = validate_utc_datetime(timestamp)
        if not (self.supported_start <= instant < self.supported_end):
            raise UnsupportedRollTimestampError(
                f"{instant.isoformat()} is outside the supported range "
                f"[{self.supported_start.isoformat()}, {self.supported_end.isoformat()}) of "
                f"continuous series {self.series_id.canonical()}"
            )
        return RollResolver(self.schedule).active_contract_at(instant)


def build_continuous_series(
    *,
    series_id: ContinuousSeriesId,
    schedule: RollSchedule,
    supported_start: datetime,
    supported_end: datetime,
) -> ContinuousSeriesDefinition:
    """Build a continuous series, computing its content hash."""
    return ContinuousSeriesDefinition(
        series_id=series_id,
        schedule=schedule,
        supported_start=supported_start,
        supported_end=supported_end,
        content_hash=continuous_series_content_hash(
            series_id=series_id,
            roll_schedule_hash=schedule.content_hash,
            supported_start=validate_utc_datetime(supported_start),
            supported_end=validate_utc_datetime(supported_end),
        ),
    )
