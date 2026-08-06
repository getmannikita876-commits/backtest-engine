# ADR-005: Bar identity and conflict semantics

- Status: **Accepted**
- Date: 2026-08-06
- Related: ADR-002 (bar availability time), ADR-003 (trade identity),
  `docs/data-import.md`, `docs/data-contracts.md`

## Context

Duplicate detection identified a bar by the tuple of its record type plus
every required field — instrument, timestamp, interval, **and OHLCV**. That
definition made two records for the same instrument and period with different
values look like two distinct records, so both were silently accepted:

```
rows submitted : 2   (same instrument, same period, volumes 5 and 9)
rows accepted  : 2
issues         : none
success flag   : True
```

An independent audit confirmed this empirically. The validator caught the
harmless case — two *identical* bars, where either copy preserves the data
exactly — and stayed silent on the harmful one, where the source contradicts
itself about what happened in a period. Accepting both double-counts volume in
every aggregate and leaves two bars at one position on the timeline whose
relative order carries no meaning, silently corrupting every downstream
statistic.

The root cause is a conflation of two concepts. A bar is a **summary keyed by
its period**: its identity is *which period it describes*, and its OHLCV
values are *claims about* that period, not part of what it is. Folding the
claims into the identity made disagreement indistinguishable from distinctness.

## Decision

### Bar identity is the period

A bar is identified by:

```
(instrument_symbol, interval_start, interval)
```

The import record carries availability time in `timestamp`, and
`interval_start` is exactly `timestamp - interval` (ADR-002). Because
`interval` is part of the identity, `(instrument_symbol, timestamp, interval)`
and `(instrument_symbol, interval_start, interval)` define precisely the same
equivalence classes; the implementation compares the record's own fields and
performs no datetime arithmetic, which cannot overflow inside a validator that
must never raise.

OHLCV values are **not** identity.

### Exact duplicate and conflict are distinct concepts

Two records sharing a bar identity are two claims about one bar:

| Case | Definition | Code | Severity | Outcome |
| --- | --- | --- | --- | --- |
| Exact duplicate | every value field agrees | `duplicate_row` | WARNING | one copy survives, chosen by `DuplicatePolicy` |
| Conflict | any value field differs | `conflicting_bar` | ERROR | **no copy survives** |

Exact-duplicate behaviour is unchanged: `REJECT`, `KEEP_FIRST`, and
`KEEP_LAST` keep their existing semantics, and collapsing identical copies is
lossless by construction.

### Conflicts reject every member of the group

The `conflicting_bar` error is attributed to **every record in the conflicting
group, including the first occurrence**, so none survives row rejection and no
`DuplicatePolicy` value can resurrect one.

Neither copy is retained because retaining either is a guess. The validation
layer has no evidence about which source is authoritative — "later" is not
"corrected": the second record may be a vendor revision, a double-fetch of a
stale file, or a merge error, and the layer cannot tell these apart. Silently
preferring later data would convert an observable contradiction into an
invisible, data-dependent bias. A rejected conflict is visible in the report
(`success=False`, the rows counted in `rejected_rows`, the differing fields
named in the message) and resolvable by the operator, who does know the
provenance; a guessed winner is neither.

A group containing a conflict receives conflict errors only — no
`duplicate_row` warnings, even for members that agree with each other. The
group's diagnosis is the contradiction, and a warning promising that "one copy
will be discarded by policy" would be false there. Identical copies within a
conflicting group do not vote: agreement between two copies is evidence of
duplication, not of correctness.

A value that cannot even be compared — a signalling `NaN`, which raises on
comparison — is treated as differing. It cannot be shown to agree, the
malformed row is separately rejected by the value validator, and the
well-formed copy must not pass unexamined merely because its rival was
malformed.

### What this ADR does not change

- **Trades remain non-deduplicated.** ADR-003's interim policy stands
  unchanged: trades have no attribute-based identity, `record_identity`
  returns `None` for them, and no policy can discard one. Trade identity
  remains an open contract question.
- **Quote semantics are unchanged.** A quote's identity remains every required
  field, so a repeated quote identity is always an exact duplicate and a quote
  conflict is unconstructible. Whether quotes deserve a narrower identity
  (instrument + timestamp) is explicitly out of scope; no defect requires it.
- **No schema change.** Identity and conflict are import-validation concepts;
  the domain models, storage schema, and `SCHEMA_VERSION` are untouched.

## Alternatives considered

**Keep the later record (last-write-wins).** Rejected. It assumes revisions
arrive in order and that later means corrected — neither is established for
archived exports, where a stale file fetched twice makes the *earlier* copy no
more or less authoritative than the later one. It also makes results depend on
file concatenation order, which is exactly the kind of hidden
order-sensitivity this platform exists to eliminate.

**Keep the earlier record.** Rejected for the same reason in mirror image.

**Majority vote within the group.** Rejected: two identical stale copies would
outvote one correction. Copy count measures duplication, not truth.

**Report the conflict as a WARNING and let policy resolve it.** Rejected: a
warning does not reject rows and does not fail a batch, so a conflicted
interval would silently resolve by policy — the current defect with a log line
attached.

**Make conflict resolution a configurable policy (e.g. `PREFER_LATEST`).**
Deferred, not rejected. A future revision-aware contract — vendor revision
flags, file-level provenance — could justify an explicit, documented
resolution policy. It must arrive as its own decision with vendor evidence,
not as a default.

## Consequences

Positive:

- A self-contradictory source can no longer pass validation silently; volume
  can no longer be double-counted by a repeated-but-revised bar.
- Duplicate handling and conflict handling are now separate, named concepts
  with separate codes, severities, and outcomes.
- Identity remains defined in exactly one place (`record_identity`), consumed
  by both detection and policy.

Negative:

- A batch containing one revised bar now fails (`success=False`) where it
  previously "succeeded"; operators must resolve conflicts upstream. This is
  the intended trade: the failure is visible, the previous acceptance was not.
- Losing *both* copies of a conflicted interval is deliberate data refusal.
  The rejected rows remain in the source file; nothing is destroyed.
