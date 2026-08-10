# Quant Research Terminal

A local Windows desktop application supporting professional, reproducible
quantitative research on futures.

The build is a **data foundation**, not a backtester. There is no backtest
engine, replay engine, execution simulator, strategy API, options support,
optimization, or AI feature, and none is planned before the foundation is
correct.

## What is implemented

| Area | State |
| --- | --- |
| Immutable domain models (`Trade`, `Quote`, `Bar`, `TradeSide`) | implemented |
| Storage contracts, fixed-point encoding, schema v2 | implemented |
| **Parquet read/write round-trip** | implemented |
| Import validation, normalization, ordering | implemented |
| End-to-end import use case (provider → validated Parquet, verified by read-back) | implemented |
| CSV provider | implemented |
| Databento archived delimited export decoding | implemented |
| ThetaData archived export decoding | **experimental, unverified** |
| Futures contract identity in the **domain** (`FuturesContractId`) | implemented |
| Futures contract identity in **storage** | **not implemented** — needs schema v3 |
| Exchange-calendar engine (definitions → materialized UTC windows → resolver) | implemented |
| CME equity-index (ES/NQ) calendar facts | **verified for trading dates 2023-05-22 … 2023-12-29 only** |

## Futures instrument identity

A specific listed contract is `FuturesContractId` — a venue, a product root, a
delivery month, and a **full four-digit year**, canonically `CME:ES:M2026`. See
`docs/adr/ADR-009-futures-contract-identity.md`.

- `ES` is a product and is **not executable**; only a `FuturesContractId`
  passes `require_listed_contract`. A continuous or back-adjusted series must be
  its own type and fails that guard.
- **`ESM6` is never expanded to 2026 by inference.** The one function that
  expands an abbreviated year requires an explicit decade and has no default, so
  no identity can depend on the current date. This holds for the committed
  `ESM6` fixture too, whose rows are dated March 2024 and which establishes
  nothing about the delivery year.
- Values are **rejected, never normalized**: `"es"` and `" ES"` are errors, so
  two spellings cannot silently become one instrument.
- Identity excludes specification (tick size, multiplier, currency, fees,
  expiry dates) and provenance. Neither is modelled.

**Storage still persists only the legacy symbol string.** Schema v2 has one
`utf8` `instrument_symbol` column, which carries no venue and no full year, so
canonical identity cannot be reconstructed from a stored file without guessing.
`SCHEMA_VERSION` remains 2 and existing files are unchanged in meaning.

## Exchange calendar

The calendar answers, for a UTC instant: exchange state (trading / halt /
maintenance / closed), the **calendar-assigned** `TradingDate`, the current
window's bounds, the next transition, and the pinned calendar identity
(`CalendarId`, `CalendarVersion`, materialized content hash). See
`docs/adr/ADR-010-exchange-calendar.md`.

The **mechanics** and the **verified facts** are different things:

- Mechanics (implemented, generic): declarative TOML definitions validated by
  strict models, a deterministic materializer producing explicit half-open
  `[start, end)` UTC windows, typed rejection of DST-nonexistent and
  DST-ambiguous rule boundaries, tzdb probes that fail materialization loudly
  on a divergent zone database, a content hash pinning the materialized
  schedule, and a resolver that performs no timezone arithmetic at query time.
- Facts (`CME_EQUITY_INDEX` v1): the CME Globex EQUITIES schedule —
  Sun–Fri 17:00–16:00 CT with the 15:15–15:30 CT daily halt and all six 2023
  holiday exceptions — **verified against official CME publications for
  trading dates 2023-05-22 through 2023-12-29 and no further**. Outside that
  range the calendar refuses to answer rather than extrapolating; extending it
  is evidence work, recorded in `docs/calendar-evidence.md`.

A trading date is assigned by the calendar, never derived from a timestamp:
the Sunday-evening session before Memorial Day 2023 belongs to trading date
**Tuesday** 2023-05-30, per CME's own schedule.

## What is not implemented

Replay, execution, strategies, portfolio, options, Monte Carlo, prop-firm
evaluation, DuckDB, dataset partitioning or catalogue, provider registry, live
or historical vendor APIs, credential handling, and experiment tracking.

Also not implemented: research session segmentation (RTH/ETH labels over the
calendar), instrument→calendar mapping, calendar facts outside the verified
2023 range, rollover, continuous futures and back-adjustment, instrument
specifications, any vendor symbol-to-identity alias registry, and persistence
of canonical instrument identity.

Storage writes and reads single-record-type Parquet files by path. There is no
catalogue, no partitioning, and no performance claim.

## Requirements

- Windows 10/11
- Python 3.12 (`requires-python = ">=3.12,<3.13"`; 3.11 will not install,
  3.13 is untested and deliberately excluded)

## Setup and run

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]" -c constraints.txt
quant-research-terminal
```

`constraints.txt` pins the exact version of every package in the supported
environment, so two installs from the same commit resolve identically. See
`docs/adr/ADR-008-environment-reproducibility.md` for the policy and the
regeneration procedure.

Alternative launch:

```powershell
python -m quant_research_terminal
```

## Verification

```powershell
python -m pip install -e ".[dev]" -c constraints.txt
python -m pytest
python -m ruff check .
python -m mypy src tests
```

## Environment report

When reporting a defect or recording an experiment environment, capture:

```powershell
python -c "import sys, platform; print(sys.version); print(platform.platform())"
python -m pip freeze
python -c "import zoneinfo; print(zoneinfo.ZoneInfo('UTC'))"
git rev-parse HEAD
```

The third line verifies the IANA timezone database is available — required for
materializing the storage layer's UTC timestamps through PyArrow or Polars.
The Git command is optional context, not a runtime dependency; the application
never invokes Git.

See `PROJECT_CHARTER.md`, `docs/architecture.md`, `docs/roadmap.md`, and
`docs/adr/ADR-001-project-foundation.md` for scope and architectural decisions.

## Deliberate limitations

- **No vendor acquisition.** Providers decode *archived* files only. Nothing
  authenticates or opens a socket, because a live API cannot be deterministic
  and determinism is the point.
- **The ThetaData decoder is unverified.** It was written without inspecting a
  real export; every vendor assumption is registered in `docs/data-import.md`
  and none is confirmed. `provider.verification_status` reports this.
- **Storage is a file API, not a database.** One record type per file, no
  catalogue, no partitioning, no query layer.
- **Reproducibility machinery does not exist yet.** There is no experiment
  tracker, seed management, or dataset versioning beyond the storage schema
  version.

