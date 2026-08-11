"""Canonical semantic identity of a market-data dataset.

A dataset has three identities and this module owns the one that matters most
to research:

* **Semantic** — *what the data means*: the canonical logical records, in
  order, for one listed contract. Computed here.
* **Physical** — *which exact bytes*: a SHA-256 of a stored artifact. Lives in
  the storage layer, because it is about files.
* **Manifest** — *which immutable claims and provenance*: lives in the catalog
  layer.

Two artifacts written by different vendors, with different aliases, different
Parquet compression, and different row-group layout are the **same dataset**
if they decode to the same records in the same order. That statement is the
whole purpose of the semantic hash, and it is why nothing about a provider, an
alias, a path, a codec, or a wall clock reaches this encoding.

Why this hashes records and not columns
---------------------------------------
The semantic hash is a pure function of ``(record type, contract, ordered
records)``. It is deliberately **not** computed from stored column values,
because storage is allowed to be lenient where semantics are not:
:func:`~quant_research_terminal.domain.trade_side.parse_trade_side` normalises
case and whitespace, so a file holding ``"BUY"`` and one holding ``"buy"``
decode to the identical record. Hashing the columns would call those two
datasets different; hashing the decoded records calls them the same, which is
what "semantic" has to mean.

Ordering
--------
This is a **sequence** hash: same records in a different order give a different
result, and the hasher never sorts. Duplicate records are preserved exactly —
ADR-003 makes repeated identical trades legitimate, so collapsing them would
destroy real data.

The order it pins is the artifact's **stored order**. That is *not* the future
replay total order: ``source_index`` is not persisted, so nothing in a stored
artifact proves the order came from ``event_ordering_key``. The hash certifies
*which* order the artifact has, never *how* that order was derived.

Framing
-------
Every variable-length element is length-prefixed and every integer is
fixed-width, so no two distinct datasets can serialize to the same byte stream
by re-splitting the same concatenation. This matters concretely rather than
theoretically: the fixed row widths are 25 bytes for a trade, 40 for a quote,
and 56 for a bar, and ``25 * 8 == 40 * 5 == 200``. Eight trades can therefore
be chosen whose row bytes are byte-identical to five valid quotes.

The **record-type token in the header** is what makes that aliasing impossible,
and it alone is sufficient for it. The **row-count footer** is not a second
defence against the same attack — within one record type every row is
fixed-width, so the count is recoverable from the stream length. It earns its
place for a different reason: it turns the row count into an explicit claim the
hasher can be asked to check (:meth:`SemanticDatasetHasher.finalize`'s
``expected_row_count``), so a generator that stops early cannot produce a
digest that looks valid. Stating this precisely matters, because "both fields
are load-bearing against collisions" would be an overclaim, and an overclaimed
guarantee is one nobody re-checks.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum, unique
from typing import Final, final

from pydantic import field_validator

from quant_research_terminal.domain.common import CanonicalModel
from quant_research_terminal.domain.futures_contract import (
    FuturesContractId,
    require_listed_contract,
)
from quant_research_terminal.domain.models import Bar, Quote, Trade
from quant_research_terminal.domain.numeric import price_to_fixed_point
from quant_research_terminal.domain.time import epoch_microseconds
from quant_research_terminal.domain.trade_side import TradeSide

#: Version of the canonical semantic encoding.
#:
#: Deliberately separate from the storage ``SCHEMA_VERSION``. A physical schema
#: can change — a column added, a Parquet option adjusted — without changing
#: what the records *mean*, and a semantic encoding can change without any
#: physical change. Tying semantic identity to the storage integer would make
#: a harmless schema refactor re-key every dataset in the catalog.
SEMANTIC_ENCODING_FORMAT: Final[str] = "qrt-semantic-dataset/1"

#: A content hash is a SHA-256 hex digest, nothing looser.
_HASH_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")

#: Width of every integer field in the encoding, and of the row-count footer.
_INT_WIDTH: Final[int] = 8

#: The unit bar intervals are encoded in, matching the storage contract.
#:
#: Public so that anything else deriving a bar's period uses this exact unit
#: rather than a second copy of it; two spellings of one unit in two modules is
#: the drift this module's canonical encoding exists to prevent.
MICROSECOND_UNIT: Final[timedelta] = timedelta(microseconds=1)
_MICROSECOND_UNIT: Final[timedelta] = MICROSECOND_UNIT


@unique
class RecordType(StrEnum):
    """The market-data record shapes a dataset artifact can hold.

    Values are lower case, matching the persisted ``TradeSide`` vocabulary.

    Deliberately distinct from
    :class:`~quant_research_terminal.data_import.contracts.ImportRecordType`,
    and the casing difference is load-bearing rather than cosmetic. ``StrEnum``
    members compare *and hash* equal to any equal-valued string, including a
    member of a different ``StrEnum`` — so upper-case values here would let an
    import-layer enum silently key a domain hash. The storage layer may not
    import ``data_import`` in any case (the layering forbids it), so the two
    vocabularies cannot be merged without moving an established public contract;
    that is a larger change than this phase needs, and the disjointness is
    asserted by a test instead.
    """

    TRADE = "trade"
    QUOTE = "quote"
    BAR = "bar"


def require_record_type(value: object) -> RecordType:
    """Return ``value`` if it is exactly a :class:`RecordType`.

    An exact type check, for the reason in :class:`RecordType`'s docstring: an
    equal-valued string or a foreign ``StrEnum`` member compares equal and would
    otherwise select an encoding silently.

    Raises:
        TypeError: if ``value`` is not exactly a :class:`RecordType`.
    """
    if type(value) is not RecordType:
        raise TypeError(
            f"a RecordType is required, exactly; got {type(value).__name__}: {value!r}. "
            f"An equal-valued string or another StrEnum's member compares equal and must "
            f"not be allowed to select an encoding"
        )
    return value


#: The fields each record type contributes, in canonical encoding order.
#:
#: Serialized into the header as a schema token, so reordering or renaming a
#: field moves every hash *even if the author forgets to bump*
#: :data:`SEMANTIC_ENCODING_FORMAT`. That is a structural guarantee where the
#: format token alone is only a human promise.
SEMANTIC_FIELD_ORDER: Final[Mapping[RecordType, tuple[str, ...]]] = {
    RecordType.TRADE: ("timestamp", "price", "size", "side"),
    RecordType.QUOTE: ("timestamp", "bid", "ask", "bid_size", "ask_size"),
    RecordType.BAR: (
        "timestamp",
        "interval_microseconds",
        "open",
        "high",
        "low",
        "close",
        "volume",
    ),
}

#: Stable one-byte codes for the trade-side vocabulary.
#:
#: A fixed width rather than a token, so the encoding cannot become ambiguous
#: if the vocabulary ever grows a member that prefixes another.
SIDE_CODE: Final[Mapping[TradeSide, int]] = {
    TradeSide.BUY: 0,
    TradeSide.SELL: 1,
    TradeSide.UNKNOWN: 2,
}


class DatasetIdentityError(Exception):
    """Base class for semantic-identity errors."""


class SemanticEncodingError(DatasetIdentityError):
    """A record could not be encoded into the canonical semantic stream."""


class HexDigest(CanonicalModel):
    """A validated lower-case SHA-256 hex digest.

    Lower case, exactly 64 hex characters, and **never normalised**: an
    upper-case or whitespace-padded digest is rejected rather than tidied, for
    the ADR-009 reason that a normalising parser is a second, looser definition
    of identity. No ``sha256:`` prefix convention is adopted, because the
    project has exactly one digest algorithm and a prefix that never varies
    carries no information.
    """

    value: str

    @field_validator("value")
    @classmethod
    def _validate_value(cls, value: str) -> str:
        if _HASH_PATTERN.fullmatch(value) is None:
            raise ValueError(
                f"a content hash must be exactly 64 lower-case hexadecimal characters; "
                f"got {value!r}. Values are never trimmed or lower-cased"
            )
        return value

    def canonical(self) -> str:
        """Return the digest as its canonical lower-case hex string."""
        return self.value


@final
class SemanticDatasetHash(HexDigest):
    """Identity of a dataset's canonical logical semantics."""


@final
class PhysicalArtifactHash(HexDigest):
    """Identity of an artifact's exact stored bytes."""


@final
class ManifestHash(HexDigest):
    """Identity of an immutable manifest's claims and provenance."""


def require_digest[DigestT: HexDigest](value: object, expected: type[DigestT]) -> DigestT:
    """Return ``value`` if it is exactly ``expected``, never a subclass.

    ``@final`` is a checker-time promise, not a runtime one: a subclass
    overriding :meth:`HexDigest.canonical` can still be constructed at runtime,
    and it would serialize one digest while the in-memory field holds another.
    That is the ADR-009 D4 failure applied to identity itself, so the guard is
    the same exact-type check :func:`require_record_type` uses.

    Raises:
        TypeError: if ``value`` is not exactly of type ``expected``.
    """
    if type(value) is not expected:
        raise TypeError(
            f"a {expected.__name__} is required, exactly; got {type(value).__name__}: "
            f"{value!r}. A subclass may override canonical() and make the serialized "
            f"digest disagree with the stored one"
        )
    return value


def _frame(text: str) -> bytes:
    """Return a length-prefixed UTF-8 encoding of ``text``.

    The length is of the encoded **bytes**, not the character count, so a
    non-ASCII value cannot desynchronise the stream.
    """
    encoded = text.encode("utf-8")
    return len(encoded).to_bytes(4, "big", signed=False) + encoded


def _signed(value: int, field: str) -> bytes:
    """Encode a signed integer, e.g. an epoch-microsecond timestamp.

    Timestamps before 1970 are negative — ``MIN_TIMESTAMP_MICROSECONDS`` is
    about -6.2e16 — so an unsigned encoding here would raise on perfectly valid
    historical data.
    """
    try:
        return value.to_bytes(_INT_WIDTH, "big", signed=True)
    except OverflowError as error:
        raise SemanticEncodingError(
            f"{field} value {value} does not fit a signed {_INT_WIDTH}-byte integer"
        ) from error


def _unsigned(value: int, field: str) -> bytes:
    """Encode an unsigned integer, e.g. a quantity or an interval.

    Quantities reach ``2**64 - 1``, which does not fit a *signed* 8-byte
    integer, so signedness is per-field rather than uniform.
    """
    try:
        return value.to_bytes(_INT_WIDTH, "big", signed=False)
    except OverflowError as error:
        raise SemanticEncodingError(
            f"{field} value {value} does not fit an unsigned {_INT_WIDTH}-byte integer"
        ) from error


def _quantity(value: Decimal, field: str) -> bytes:
    """Encode a whole-number quantity exactly as storage does.

    Quantities are integral by the domain envelope, so ``to_integral_value``
    discards nothing; it is used rather than ``int()`` to mirror
    ``data/conversion.py`` exactly and keep one scaling convention.
    """
    return _unsigned(int(value.to_integral_value()), field)


def _encode_trade(trade: Trade) -> bytes:
    return b"".join(
        (
            _signed(epoch_microseconds(trade.timestamp), "timestamp"),
            _signed(price_to_fixed_point(trade.price), "price"),
            _quantity(trade.size, "size"),
            _side_byte(trade.side),
        )
    )


def _side_byte(side: TradeSide) -> bytes:
    code = SIDE_CODE.get(side)
    if code is None:
        # Unreachable for the closed vocabulary, and deliberately explicit: a
        # member added later must fail loudly here rather than be silently
        # dropped from the digest.
        raise SemanticEncodingError(f"no canonical semantic code for trade side {side!r}")
    return code.to_bytes(1, "big", signed=False)


def _encode_quote(quote: Quote) -> bytes:
    return b"".join(
        (
            _signed(epoch_microseconds(quote.timestamp), "timestamp"),
            _signed(price_to_fixed_point(quote.bid), "bid"),
            _signed(price_to_fixed_point(quote.ask), "ask"),
            _quantity(quote.bid_size, "bid_size"),
            _quantity(quote.ask_size, "ask_size"),
        )
    )


def _encode_bar(bar: Bar) -> bytes:
    # ``bar.timestamp`` is the availability time (ADR-002) and is exactly what
    # the storage layer persists; the interval completes the pair, so interval
    # start is not encoded separately — it is derivable and encoding both would
    # let the two disagree.
    return b"".join(
        (
            _signed(epoch_microseconds(bar.timestamp), "timestamp"),
            _unsigned(bar.interval // _MICROSECOND_UNIT, "interval_microseconds"),
            _signed(price_to_fixed_point(bar.open), "open"),
            _signed(price_to_fixed_point(bar.high), "high"),
            _signed(price_to_fixed_point(bar.low), "low"),
            _signed(price_to_fixed_point(bar.close), "close"),
            _quantity(bar.volume, "volume"),
        )
    )


_ENCODERS: Final[Mapping[RecordType, Callable[..., bytes]]] = {
    RecordType.TRADE: _encode_trade,
    RecordType.QUOTE: _encode_quote,
    RecordType.BAR: _encode_bar,
}

_EXPECTED_TYPES: Final[Mapping[RecordType, type]] = {
    RecordType.TRADE: Trade,
    RecordType.QUOTE: Quote,
    RecordType.BAR: Bar,
}


def schema_token(record_type: RecordType) -> str:
    """Return the derived schema token for a record type.

    Built from the field order rather than written by hand, so the token and
    the encoding cannot drift apart.
    """
    fields = SEMANTIC_FIELD_ORDER[record_type]
    return f"{record_type.value}/{':'.join(fields)}"


def canonical_record_bytes(record_type: RecordType, record: Trade | Quote | Bar) -> bytes:
    """Return one record's canonical semantic encoding.

    The **same** bytes the dataset hash is built from, exposed so that anything
    asking "are these two rows the same row?" answers it with the definition the
    dataset identity already uses. A second, separately written row-equality rule
    would be a second definition of sameness, and the two would drift.

    Vendor-neutral by construction rather than by a filter someone must remember
    to apply: ``instrument_symbol`` is not among the encoded fields, so a vendor
    alias cannot reach the result. The contract is *not* encoded either — it is a
    dataset-level fact in schema v3, carried by the manifest, so callers
    comparing rows across datasets must establish it separately.

    Raises:
        TypeError: if ``record_type`` is not exactly a :class:`RecordType`.
        SemanticEncodingError: if ``record`` is not that type's record, or holds
            a value outside the canonical encoding's range.
    """
    require_record_type(record_type)
    expected = _EXPECTED_TYPES[record_type]
    if type(record) is not expected:
        raise SemanticEncodingError(
            f"a {record_type.value} record is required; got {type(record).__name__}"
        )
    return _ENCODERS[record_type](record)


@final
class SemanticDatasetHasher:
    """Single-pass hasher over a dataset's canonical logical records.

    Feeds framed bytes to ``hashlib`` incrementally, so a large dataset never
    has to exist as one byte string. The row count is written as a **footer**
    rather than a header, which is what makes the pass genuinely single: the
    hasher counts what it was actually fed instead of trusting a length taken
    before iteration.
    """

    def __init__(self, *, record_type: RecordType, contract: FuturesContractId) -> None:
        self._record_type = require_record_type(record_type)
        self._contract = require_listed_contract(contract)
        self._expected_type = _EXPECTED_TYPES[self._record_type]
        self._digest = hashlib.sha256()
        self._rows = 0
        self._digest.update(_frame(SEMANTIC_ENCODING_FORMAT))
        self._digest.update(_frame(self._record_type.value))
        self._digest.update(_frame(schema_token(self._record_type)))
        self._digest.update(_frame(self._contract.canonical()))

    def update(self, record: Trade | Quote | Bar) -> None:
        """Add one record to the stream, in its position in the sequence."""
        if type(record) is not self._expected_type:
            raise SemanticEncodingError(
                f"this dataset holds {self._record_type.value} records; got {type(record).__name__}"
            )
        encoder = _ENCODERS[self._record_type]
        self._digest.update(encoder(record))
        self._rows += 1

    @property
    def row_count(self) -> int:
        """How many records have been fed so far."""
        return self._rows

    def finalize(self, *, expected_row_count: int | None = None) -> SemanticDatasetHash:
        """Close the stream and return the digest.

        Args:
            expected_row_count: if given, the number of records the caller
                believes it supplied. A mismatch raises rather than returning a
                valid-looking digest, so a generator that stops early cannot
                silently produce the hash of a truncated dataset.
        """
        if expected_row_count is not None and expected_row_count != self._rows:
            raise SemanticEncodingError(
                f"expected {expected_row_count} records but {self._rows} were encoded; "
                f"a truncated sequence must not produce a valid digest"
            )
        digest = self._digest.copy()
        digest.update(_unsigned(self._rows, "row_count"))
        return SemanticDatasetHash(value=digest.hexdigest())


def semantic_dataset_hash(
    *,
    record_type: RecordType,
    contract: FuturesContractId,
    records: Iterable[Trade | Quote | Bar],
    expected_row_count: int | None = None,
) -> SemanticDatasetHash:
    """Return the canonical semantic hash of an ordered record sequence."""
    hasher = SemanticDatasetHasher(record_type=record_type, contract=contract)
    for record in records:
        hasher.update(record)
    return hasher.finalize(expected_row_count=expected_row_count)


def dataset_time_bounds(
    records: Iterable[Trade | Quote | Bar],
) -> tuple[datetime | None, datetime | None]:
    """Return the earliest and latest canonical timestamp, or ``(None, None)``.

    A **full scan**, never the first and last record: storage preserves caller
    order and never sorts, so an unsorted artifact is entirely legal and
    ``records[0]``/``records[-1]`` would produce a manifest that is
    self-consistent and wrong about its own artifact.

    For bars the canonical timestamp is ``Bar.timestamp`` — the availability
    time (ADR-002), which is what the ``timestamp`` column stores — not
    ``interval_start``.
    """
    earliest: datetime | None = None
    latest: datetime | None = None
    for record in records:
        stamp = record.timestamp
        if earliest is None or stamp < earliest:
            earliest = stamp
        if latest is None or stamp > latest:
            latest = stamp
    return earliest, latest
