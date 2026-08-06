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

## What is not implemented

Replay, execution, strategies, portfolio, options, Monte Carlo, prop-firm
evaluation, DuckDB, dataset partitioning or catalogue, provider registry, live
or historical vendor APIs, credential handling, and experiment tracking.

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

