# Quant Research Terminal

Phase 0 foundation for a local Windows desktop application supporting professional,
reproducible quantitative research. The current build intentionally contains only the
application shell and project structure—no market-data adapters, strategies, backtest
engine, execution simulator, options, optimization, or AI features.

## Requirements

- Windows 10/11
- Python 3.11 or newer

## Setup and run

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
quant-research-terminal
```

Alternative launch:

```powershell
python -m quant_research_terminal
```

## Verification

```powershell
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check .
python -m mypy src tests
```

See `PROJECT_CHARTER.md`, `docs/architecture.md`, `docs/roadmap.md`, and
`docs/adr/ADR-001-project-foundation.md` for scope and architectural decisions.

## Deliberate limitations

The current foundation intentionally excludes data importers, market-data vendors,
execution simulation, strategies, options, Monte Carlo, optimization, and prop-firm
evaluation. Those areas remain future work and are not implemented in this phase.

