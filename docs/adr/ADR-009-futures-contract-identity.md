# ADR-009: Canonical identity of a listed futures contract

- Status: **Accepted for the domain model; persistence deliberately deferred.**
  The identity model, its guarantees, and the import-layer defect fixes below
  are implemented and tested. Persisting canonical identity is **not**
  implemented and requires a future storage-schema decision (see
  "Storage schema-v2 compatibility"). No part of this ADR is speculative: what
  is unresolved is named as unresolved.
- Date: 2026-08-08
- Related: ADR-003 (trade identity), ADR-005 (bar identity and conflicts),
  ADR-007 (application layer; reserved this subject as "Phase 2.0"),
  `docs/data-contracts.md`, `docs/data-import.md`, `docs/architecture.md`

> Numbering note: 001–005, 007, and 008 exist. ADR-006 is reserved by ADR-008's
> own numbering note for the storage error contract and remains unwritten, so
> this decision takes 009 rather than filling the gap with an unrelated subject.

## Context

The platform identified an instrument by a bare string — `instrument_symbol`
on `Trade`, `Quote`, and `Bar`; a `utf8` column in all three schema-v2 Parquet
schemas; a field on `ProviderRequest`; and the instrument component of bar and
quote duplicate identity. `Instrument` exists in the domain but is a Phase 1.1
descriptive placeholder that nothing consumes.

A bare string cannot distinguish the concepts futures research is built on:

| Concept | Example | Executable? |
| --- | --- | --- |
| Product / root | `ES` | **no** — an order names a delivery month |
| Listed contract | `ESM6`, `ESU6` | yes |
| Vendor alias | `ES1!`, a numeric `instrument_id` | not an identity at all |
| Synthetic series | continuous ES, back-adjusted ES | **no** — assembled after the fact |

Three failures follow, and all three are silent:

1. **`ES` can be used where `ESM6` is required.** Nothing distinguishes a
   product from a contract, so a position, fill, or roll can reference a
   concept that never traded.
2. **`ESM6` does not name a year.** `M6` abbreviates the delivery year to one
   digit; it denotes June 2006, 2016, 2026, or 2036 equally. Any rule that
   expands it — "the current decade", "the decade of the data", "the nearest
   expiry" — either reads the wall clock, making an identity depend on *when*
   it was computed, or reads the loaded rows, making it depend on *which* rows
   were loaded. Both destroy reproducibility, which is the platform's purpose.
3. **A continuous series is one string away from being executable.** A backtest
   that fills orders in a back-adjusted series reports prices no participant
   could have received.

The committed fixture `tests/fixtures/esm6_trades.csv` is `ESM6` with rows
dated **March 2024**. It is worth saying explicitly: that file establishes
nothing about the delivery year. March 2024 data is consistent with a June 2026
listing and equally consistent with a re-used symbol from June 2016. The
fixture's previous docstring asserted "June 2026"; that claim has been removed,
because it was exactly the inference this ADR forbids.

## Decision

### Canonical identity

```
Venue                  a market namespace token
  └── FuturesProduct   venue + product root          e.g. CME:ES
        └── FuturesContractId   product + delivery month + FULL year
```

```python
FuturesContractId(
    product=FuturesProduct(venue=Venue(code="CME"), root="ES"),
    contract_month=ContractMonth.JUNE,
    contract_year=2026,
)  # canonical: "CME:ES:M2026"
```

Three types, not one, because the distinction between a product and a contract
is the whole point: it is carried by the type system rather than by convention.
All three live in `domain/` and depend on nothing above it.

### What participates in equality and hashing

Exactly the three components above, transitively: venue, root, delivery month,
contract year. Nothing else. All three types are frozen, `extra="forbid"`,
`strict=True`, and `@final`.

The consequence that matters: an identity is stable for the life of the
contract. No specification change, vendor re-mapping, or provenance detail can
make a contract stop being equal to itself.

### Identity is not specification

Tick size, point value, multiplier, currency, fee schedule, margin, settlement
method, trading hours, first notice date, and last trade date are properties
*of* a contract, not part of *which* contract it is. Several change during a
contract's life, so folding them into identity would break reflexivity across a
rule change.

**No specification type is introduced.** The repository holds no authoritative
source for contract specifications, and inventing one would be fake
completeness. Specification will need effective-dated, versioned semantics when
it arrives; that is its own decision.

### Identity is not provenance

Provider name, source file, source row, dataset id, vendor sequence, and import
id describe an *observation of* a contract. ADR-007 recorded that no provenance
survives into domain objects or Parquet. This ADR does **not** fix that, and
deliberately does not smuggle provenance into identity to make it survive
storage — that would make two observations of one contract compare unequal,
which is precisely backwards.

### Venue is part of identity, and is deliberately shallow

A product root is unique only *within* a market: two exchanges may each list a
root spelled `ES`. Venue is therefore part of identity, carried on the product
because a product is listed on exactly one venue.

`Venue.code` is an **opaque namespace token**. It is explicitly *not* an ISO
10383 MIC (operating or segment), not an exchange group's marketing name, not a
vendor venue or dataset code, and not a matching-engine identifier. Those are
genuinely different concepts, and this repository contains no authoritative
venue registry from which to distinguish them. Building a hierarchy from no
evidence would be fake precision.

The honest consequence: `"CME"` and `"XCME"` are two venues to this model,
because it has no basis for knowing they are one. Using one token per market
consistently is the operator's responsibility until a venue registry exists.

### Contract month

`ContractMonth` is a closed twelve-member `StrEnum` whose value is the
published code and whose name is the calendar month, with `month_number`,
`from_code`, and `from_month_number`. It is the single authority; no other
module defines a code table, verified by grep after implementation.

`from_code` is strict — no case folding, no whitespace tolerance — and
deliberately unlike `parse_trade_side`, which tolerates both. That tolerance is
right for a *vocabulary field* read back from storage; it is wrong for an
*identity component*. A delivery-month code is one character, so accepting
`"m"` would put a lower-case vendor spelling one step from canonical identity.

**Verification status, stated precisely.** `H`/March, `M`/June, `U`/September,
and `Z`/December were corroborated against a CME Group source. The other eight
codes could not be retrieved from a primary specification in the environment
where this was written and are adopted convention, not audited fact. This is
recorded in the module docstring too, because a wrong entry shifts every
affected contract by a month, silently.

### Contract year: always full, never inferred

`contract_year` is a four-digit `int` in **1000–9999**.

The bounds are derived from the serialization contract, not from a market-history
judgement: the canonical form writes the year in exactly four digits, so a year
outside that range has no canonical form. A "sensible" lower bound such as 1970
or an exchange's founding year would be an arbitrary market opinion encoded as a
type constraint; four-digit representability is not. The range also sits inside
`datetime`'s year range, so no contract year is unrepresentable to a future
calendar layer.

Abbreviated years are handled at exactly one explicit boundary:

```python
resolve_abbreviated_contract_year("6", cycle_start=2020)  # 2026
resolve_abbreviated_contract_year("6", cycle_start=2010)  # 2016
resolve_abbreviated_contract_year("6")  # TypeError
```

`cycle_start` has **no default**, so a caller cannot obtain a year without
stating which window it came from, and no call site can quietly acquire a
dependency on the current date. The window size is `10**len(code)` and
`cycle_start` must be an exact multiple of it, because a window starting at 2015
would make `"6"` mean 2016 or 2021 depending on the reading. One- and two-digit
codes are supported; other widths are rejected rather than guessed, because the
project has evidence for those two vendor conventions and none for any other.

Codes are matched with `[0-9]`, not `str.isdigit()`: the latter accepts `"٦"`,
which `int()` then converts happily, so a look-alike symbol would resolve to a
real year.

### Canonical serialization

`VENUE:ROOT:<CODE><YYYY>`, e.g. `CME:ES:M2026`.

- `:` is excluded from every field's character set, so splitting is unambiguous.
- The delivery field is fixed-width, so no locale-sensitive number formatting is
  involved.
- Nothing reads a set, a dict, the wall clock, or the hash seed. Verified: the
  full canonical universe plus a validator's error text is **byte-identical**
  across five `PYTHONHASHSEED` values in separate processes.
- It cannot be confused with a vendor symbol. No vendor writes `CME:ES:M2026`,
  so an alias never parses as canonical identity and canonical identity is never
  mistaken for an alias.

`parse()` is the **exact inverse** of `canonical()` — it accepts everything
`canonical()` can emit and nothing else. A parser wider than its serializer is a
second, looser definition of identity, and that is the route by which a vendor
spelling becomes canonical. Rejected accordingly: lower case, padding,
abbreviated years, wrong field counts, non-ASCII digits, and every vendor alias.

### No normalization, anywhere

Values are **rejected, never repaired**. `"es"`, `" ES"`, and `"ES "` are errors,
not inputs to be tidied. Normalizing is how two source spellings silently become
one instrument — and, worse, how one spelling silently becomes a *different*
instrument than the operator wrote. Rejection is loud, local, and fixable at the
call site.

Venue and root are `[A-Z0-9]` with a length bound and **at least one ASCII
letter**. The letter rule is not cosmetic: Databento identifies instruments by a
numeric, vendor-local `instrument_id`, and the provider layer already documents
that emitting one as a symbol "would corrupt instrument identity across the whole
platform". Requiring a letter makes that structurally impossible rather than
dependent on a reviewer noticing.

### No ordering

`FuturesContractId` defines no comparison operators. Ordering listed contracts
is meaningful only *within a product* and only *chronologically by delivery
period* — and a lexicographic order over the canonical string is not that order.
A caller needing a deterministic sequence sorts by `canonical()`; a caller
needing roll order builds `(contract_year, contract_month.month_number)` for
contracts sharing one product. Defining `__lt__` would let the wrong one be
reached for silently. Ordering was not invented because nothing needs it yet.

### The continuous-versus-executable guard

```python
require_listed_contract(value) -> FuturesContractId
```

A type check, not a flag check: a flag can be set wrongly, a type cannot be
forged. A future continuous-series identity must be its **own type** and will
fail here without this function knowing it exists. Continuous futures, roll
policies, and back-adjustment are **not implemented** and are not in this phase.

The check is `type(value) is`, not `isinstance` — exact rather than pedantic.
`FuturesContractId` is `@final`, so it has no legitimate subtype and Liskov has
nothing to preserve; and because `@final` is enforced by the type checker only,
`isinstance` let a runtime subclass through the one guard that exists to stop
it. That was reproduced, not theorised (defect D4 below).

### Import-layer defects found and fixed

Auditing how identity is *used* surfaced defects that the identity model alone
would not have fixed. All four were reproduced before being fixed, and all four
have regression tests.

**D1 — instrument-symbol coercion admitted a conflicting bar (an ADR-005 hole).**
Normalization built domain objects with
`str(record.value("instrument_symbol"))` while `record_identity` compared the
**raw** decoded value. A record carrying `None` and one carrying `"None"`
therefore had different *import* identities but the same *domain* instrument:

```
rows submitted : 2   (same instrument, same period, volumes 5 and 9)
rows accepted  : 2
issues         : none
success flag   : True
```

That is exactly the silent volume double-counting ADR-005 exists to prevent,
reached through the instrument field instead of the period fields. Also
reproduced for `123`/`"123"`, `True`/`"True"`, and `['ES']`.

Fixed by giving the rule a single home — `data_import/instrument_semantics.py`,
a leaf module alongside `time_semantics` and `numeric_semantics` — adding an
`InstrumentSymbolValidator` and the `invalid_instrument_symbol` issue code,
making `record_identity` silent for unusable symbols, and removing the `str()`
coercion from normalization.

**D2 — `model_copy` forged unvalidated identities.** Pydantic's `model_copy`
skips validation, so `identity.model_copy(update={"contract_year": 6})`
produced an object that passed the executable guard and whose `canonical()`
emitted `"CME:ES:M0006"` — a string its own `parse()` rejects. A serializer
emitting what its parser refuses means a catalogue key written today cannot be
read back tomorrow. `update={"contract_year": True}` produced year 1 the same
way, and an unrelated `tick_size` key entered the model despite `extra="forbid"`.
Fixed by re-validating in an overridden `model_copy` on the identity types.

Deliberately **not** defended against, so the guarantee is not overstated:
`model_construct` and `object.__setattr__`. Both are explicit, documented
bypasses that announce themselves at the call site.

**D3 — a `str` subclass split one instrument in two.** A `str` subclass may
override `__eq__`/`__hash__`. Two records carrying such a value compared unequal
in the import layer while Pydantic stored ordinary equal strings in the domain —
so two conflicting bars for one period were accepted together, the same failure
as D1 one level down. Notably, the same hostile subclass applied to a *price*
field failed **safe** (it became a conflict and both rows were rejected); on an
*identity* field it failed unsafe. Fixed by having duplicate identity and
normalization both take the instrument value through one function that returns
the exact `str` the domain will hold, making disagreement between the two
impossible rather than unlikely.

**D4 — a runtime subclass passed the executable guard.** Described above.

### Architecture

Identity is in `domain/` and imports nothing above it. Boundary tests were
extended to assert that, and to assert that the identity modules import no
`datetime`, `time`, `calendar`, `zoneinfo`, `random`, `uuid`, `os`, or `locale`
— making "no wall clock in identity" structural rather than a claim.

The boundary tests were also fixed to **resolve relative imports**. They
previously parsed absolute `from` imports only, so `domain/__init__.py`'s
relative sibling imports were invisible to the import graph and a breach
written as `from ..data import x` would have passed every architecture test.

## Storage schema-v2 compatibility

**Schema v2 cannot represent canonical identity, and this phase does not change
it.** Established empirically by running the real Phase 1.9 vertical slice over
the committed fixture and inspecting the resulting Parquet file:

```
columns : ['timestamp', 'instrument_symbol', 'price', 'size', 'side']
type    : instrument_symbol -> string
values  : {'ESM6'}
version : schema_version = 2
```

Reconstructing `CME:ES:M2026` from `"ESM6"` would require:

- a **venue**, which the file does not contain in any column or metadata field;
- a **product/root boundary**, obtainable only by guessing a vendor's symbol
  syntax, which differs between vendors;
- a **full year**, which `6` does not carry and which the row timestamps do not
  supply.

So the smallest concrete incompatible example is the repository's own fixture.
Any automatic upgrade would have to invent a venue and guess a decade, which
would silently give a file written yesterday a different meaning today —
prohibited outright.

**Therefore:** `SCHEMA_VERSION` stays at 2. Column names, types, fixed-point
encoding, quantity encoding, timestamp encoding, and metadata semantics are
untouched. Canonical identity exists in the domain; persistence remains
legacy-symbol-only. Two tests in the vertical-slice suite assert this limit so
it is checkable rather than merely written down.

Full persistence needs a schema v3 that stores identity components as separate
columns (or a canonical string plus a venue), together with a migration that
requires the operator to **declare** the venue and the contract year per
dataset rather than inferring either. That is a deliberate architectural
decision with a migration plan, and it is not this one.

## Application, provider, and duplicate-identity compatibility

- **Application layer:** unchanged. The vertical slice still runs end to end and
  still stores only the legacy symbol; nothing pretends otherwise.
- **Providers:** unchanged. No decoder was rewritten, no network call, key, or
  vendor SDK behaviour was added, and ThetaData's `experimental-unverified`
  status is untouched. Provider symbols remain **aliases**; mapping an alias to
  canonical identity is explicit caller-supplied data. No alias table ships
  here — a registry with no callers would be premature and would invite the
  ambient wiring ADR-007 rejected.
- **Duplicate/bar identity:** ADR-005's bar identity and conflict rules and
  ADR-003's no-trade-deduplication rule are unchanged in substance and are
  re-asserted by tests. The only change is that a record whose instrument field
  names nothing now has no identity, which strictly *strengthens* both.
- **Known limitation, asserted rather than hidden:** the import layer still
  treats `" ESM6 "` and `"ESM6"` as two instruments. Merging them would be the
  silent identity change this layer must not perform; canonical identity is the
  replacement, not a tightened string.

## Alternatives considered

**Put the full year in identity by parsing the vendor symbol.** Rejected: no
vendor symbol carries a full year, so this is inference wearing a parser's
clothes.

**Infer the decade from the data's timestamps.** Rejected: it makes identity
depend on which rows were loaded, and the fixture is the counterexample — March
2024 data is consistent with both June 2026 and June 2016.

**Omit venue from identity.** Rejected: two exchanges can list the same root, so
identities would collide across venues with no way to tell.

**Model venue as a MIC or a closed exchange enum.** Rejected as fake precision:
the repository has no authoritative registry, and MIC, exchange group, and
vendor venue code are different concepts that a guess would conflate.

**Normalize input (upper-case, trim) instead of rejecting it.** Rejected: it
silently merges spellings, and the merge is invisible in the result.

**Make canonical identity a subclass of `str`.** Rejected: it would compare
equal to raw vendor text, so an alias could enter an identity set unnoticed.

**Ship an alias registry or a vendor symbol parser.** Deferred. `ESM6`, `ESM26`,
`ES M6`, and `ESM2026` are all real-world spellings; encoding one as *the*
parser would be fake completeness. Alias resolution belongs with the Dataset
Catalog, where a mapping can be recorded as reviewed configuration.

**Give `FuturesContractId` ordering.** Rejected: see "No ordering".

**Migrate storage to schema v3 now.** Rejected: see above. A schema change to
make a design cleaner, rather than to serve an accepted migration plan, is
exactly what the storage contract forbids.

**Tighten `Trade.instrument_symbol` in the domain to reject blank or padded
values.** Deferred as a recorded blocked decision. It would change what an
already-written schema-v2 file means on read-back, which is a storage decision,
not an import-layer one — and the field is a legacy vendor-symbol slot that a
future v3 migration will supersede. The import pipeline is hardened instead, so
no new dataset can acquire an unusable symbol.

## Consequences

Positive:

- A product can no longer masquerade as an executable contract, and a synthetic
  series has no path to becoming one.
- No identity anywhere depends on the wall clock, the timezone, the locale, or
  the hash seed; canonical serialization is byte-identical across processes.
- Four reproduced defects are fixed, one of which (D1) was a live hole in
  ADR-005's conflict semantics that silently double-counted volume.
- Rollover, Exchange Calendar, Dataset Catalog, Replay, and Execution all have a
  concrete type to reference, and Execution has a guard to call.

Negative:

- Canonical identity does not survive persistence, so two representations of an
  instrument coexist — the canonical type in the domain and the legacy string in
  storage and on domain events. This is stated everywhere it matters rather than
  papered over, but it is real, and it lasts until schema v3.
- Strict validation means callers must supply canonical spellings; `"es"` and
  `" ES"` are errors. That is the intended trade.
- Eight of the twelve month codes remain unverified against a primary source.

Deferred to their own phases, and untouched here: Exchange Calendar and trading
sessions; rollover, continuous construction, and back-adjustment; the Dataset
Catalog and full provenance; Replay; Execution; instrument specifications.
