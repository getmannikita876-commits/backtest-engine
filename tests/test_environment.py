"""Environment and reproducibility regression tests (Phase 1.8C, ADR-008).

These pin the *declared environment*, not application behaviour. Each test
guards a defect the post-storage foundation audit reproduced on a clean
Windows machine:

* ``tzdata`` was required but undeclared, so ``ZoneInfo("UTC")`` raised,
  PyArrow scalar conversion failed, and Polars **panicked at the Rust level**
  while materializing the repository's own stored timestamps;
* Ruff linted for Python 3.11 while the package required 3.12, and README
  and ADR-001 advertised a version that would not install;
* the package carried no ``py.typed`` marker, so its strict typing was
  invisible to consumers.

CI runs this file on both Windows and Ubuntu, so a runner image that happens
to satisfy a dependency implicitly can no longer hide a missing declaration.
"""

from __future__ import annotations

import importlib.metadata
import importlib.resources
import sys
import tomllib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl
import pyarrow.parquet as pq  # type: ignore[import-untyped]

from quant_research_terminal.data import write_trades
from quant_research_terminal.domain.models import Trade, TradeSide

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _pyproject() -> dict[str, object]:
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)


# ==========================================================================
# Timezone database availability
# ==========================================================================


def test_zoneinfo_utc_resolves() -> None:
    # The defect: on a stock Windows install with tzdata undeclared, this
    # single line raised ZoneInfoNotFoundError — for the key "UTC" itself.
    assert str(ZoneInfo("UTC")) == "UTC"


def test_tzdata_is_declared_as_a_runtime_dependency() -> None:
    # Declared, not merely installed: an implicit system copy on the CI image
    # must not stand in for the declaration a clean machine needs.
    config = _pyproject()
    project = config["project"]
    assert isinstance(project, dict)
    dependencies = project["dependencies"]
    assert isinstance(dependencies, list)
    assert any(requirement.startswith("tzdata") for requirement in dependencies), (
        "tzdata must be a runtime dependency; see ADR-008"
    )


def test_tzdata_distribution_is_installed() -> None:
    # importlib.metadata raises PackageNotFoundError if absent.
    assert importlib.metadata.version("tzdata")


# ==========================================================================
# Arrow / Polars materialization of the repository's own output
# ==========================================================================


def _write_sample(path: Path) -> list[datetime]:
    stamps = [
        datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC),
        datetime(2024, 1, 2, 12, 0, 0, 123456, tzinfo=UTC),
        datetime(2024, 1, 2, 12, 0, 1, tzinfo=UTC) + timedelta(microseconds=999_999),
    ]
    write_trades(
        path,
        [
            Trade(
                timestamp=stamp,
                instrument_symbol="ES",
                price=Decimal("5000.25"),
                size=Decimal("1"),
                side=TradeSide.BUY,
            )
            for stamp in stamps
        ],
    )
    return stamps


def test_pyarrow_scalar_conversion_materializes_exact_utc(tmp_path: Path) -> None:
    # The defect: as_py() resolves the schema's zone name through zoneinfo and
    # raised ZoneInfoNotFoundError. The repository's internal read path avoids
    # as_py() by design (defence in depth, docs/data-contracts.md); this test
    # asserts the *declared environment* makes the ordinary path work too.
    path = tmp_path / "trades.parquet"
    stamps = _write_sample(path)

    column = pq.read_table(path).column("timestamp")
    materialized = [scalar.as_py() for scalar in column]

    assert materialized == stamps
    assert all(value.utcoffset() == timedelta(0) for value in materialized)


def test_polars_materializes_exact_utc_values(tmp_path: Path) -> None:
    # The defect: scalar access raised ZoneInfoNotFoundError and to_dicts()
    # escalated to a Rust panic (pyo3_runtime.PanicException) — not a
    # catchable, diagnosable failure. Nothing here catches anything: with the
    # environment correctly declared, no exception and no panic may occur.
    path = tmp_path / "trades.parquet"
    stamps = _write_sample(path)

    frame = pl.read_parquet(path)

    scalars = [frame["timestamp"][index] for index in range(len(stamps))]
    assert scalars == stamps

    dictionaries = frame.to_dicts()
    assert [row["timestamp"] for row in dictionaries] == stamps
    assert all(row["timestamp"].utcoffset() == timedelta(0) for row in dictionaries)


# ==========================================================================
# Python version consistency
# ==========================================================================


def test_interpreter_matches_the_declared_requirement() -> None:
    # requires-python is ">=3.12,<3.13": exactly one supported minor version.
    # 3.13 must not appear here before being explicitly tested (ADR-008).
    assert sys.version_info[:2] == (3, 12)


def test_tooling_targets_agree_with_requires_python() -> None:
    # The audited defect: Ruff linted for py311 while the package required
    # 3.12, so 3.12-only idioms went unchecked. Any future drift between the
    # declared requirement and a tool target must fail here, not survive in
    # prose.
    config = _pyproject()
    project = config["project"]
    tool = config["tool"]
    assert isinstance(project, dict)
    assert isinstance(tool, dict)

    assert project["requires-python"] == ">=3.12,<3.13"

    ruff = tool["ruff"]
    assert isinstance(ruff, dict)
    assert ruff["target-version"] == "py312"

    mypy = tool["mypy"]
    assert isinstance(mypy, dict)
    assert mypy["python_version"] == "3.12"


# ==========================================================================
# Typed package marker
# ==========================================================================


def test_py_typed_marker_is_present_in_the_installed_package() -> None:
    # PEP 561: without this marker, consumers' type checkers treat the
    # package as untyped regardless of its annotations. resources.files works
    # for both editable and wheel installs, so this also guards against a
    # packaging configuration that silently drops the file.
    marker = importlib.resources.files("quant_research_terminal").joinpath("py.typed")
    assert marker.is_file()


# ==========================================================================
# Constraints file integrity
# ==========================================================================


def test_constraints_file_exists_and_pins_exact_versions() -> None:
    # The reproducibility mechanism is a pip constraints file (ADR-008). Every
    # non-comment line must be an exact "name==version" pin: a range here
    # would silently reopen the resolution nondeterminism the file exists to
    # close. It must also contain no local paths or editable references.
    constraints = PYPROJECT.parent / "constraints.txt"
    assert constraints.is_file(), "constraints.txt is required; see ADR-008"

    lines = [
        line.strip()
        for line in constraints.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert lines, "constraints.txt must pin at least the runtime dependencies"
    for line in lines:
        assert "==" in line, f"not an exact pin: {line!r}"
        assert not line.startswith("-e"), f"editable reference in constraints: {line!r}"
        assert "://" not in line, f"URL or local reference in constraints: {line!r}"

    pinned = {line.split("==")[0].lower().replace("_", "-") for line in lines}
    assert "tzdata" in pinned
