# ADR-008: Environment reproducibility policy

- Status: **Accepted**
- Date: 2026-08-06
- Related: ADR-001 (and its 2026-08-06 addendum), `docs/data-contracts.md`,
  `constraints.txt`, `.gitattributes`

> Numbering note: ADR-006 (storage error contract) and ADR-007 (application
> layer) were proposed by the post-storage foundation audit and are reserved
> for those subjects; this ADR takes the number the audit assigned to
> environment reproducibility.

## Context

The platform's stated purpose is reproducible research, but the post-storage
foundation audit found the environment itself was the least reproducible part
of the repository:

1. **`tzdata` was required but undeclared.** The storage schema declares
   `timestamp[us, tz=UTC]`; materializing such a value through PyArrow's
   `as_py()` or through Polars resolves the zone name via `zoneinfo`, which
   needs an IANA database. A stock Windows install has none. Reproduced
   empirically on this machine before the fix: `ZoneInfo("UTC")` raised
   `ZoneInfoNotFoundError`, PyArrow scalar conversion failed, Polars scalar
   access failed, and Polars `to_dicts()` **panicked at the Rust level**
   (`pyo3_runtime.PanicException`) — a failure mode that is not a catchable
   ordinary exception and points nowhere near the missing dependency. The
   repository's own read path survived only because it deliberately rebuilds
   timestamps from microsecond counts (see `docs/data-contracts.md`); that
   workaround is defence in depth, not an excuse for an incomplete
   environment.
2. **Version claims disagreed.** `requires-python` demanded 3.12 while Ruff
   linted for 3.11 and README/ADR-001 advertised 3.11 — instructions that
   would not even install.
3. **Dependency ranges only.** Two clean installs weeks apart could resolve
   different versions of polars, pyarrow, or pydantic with no record of which,
   so "the same commit" did not mean "the same behaviour".
4. **No line-ending policy** in a Windows-primary repository with Ubuntu CI.
5. **No `py.typed` marker**, so the package's strict typing was invisible to
   consumers.

## Decision

### tzdata is a runtime dependency

`tzdata>=2024.1` is declared unconditionally, on every platform. It is
runtime, not dev-only, because materializing stored timestamps is core
runtime behaviour, not a test convenience. It is unconditional, not
Windows-only, because `zoneinfo` prefers the system database where one exists
— the package is a deterministic fallback, never a platform-specific special
case, and the dependency set stays identical across platforms. It is
calendar-versioned data with no API surface, so no upper bound is meaningful.
The storage layer's explicit UTC reconstruction is retained unchanged.

### One supported Python version: 3.12

`requires-python = ">=3.12,<3.13"` is the single source of truth, and every
tool target agrees with it: Ruff `target-version = "py312"`, MyPy
`python_version = "3.12"`, CI matrix `['3.12']`, README and ADR-001 corrected.
3.11 is not widened back; 3.13 is excluded until explicitly tested. A
regression test asserts the interpreter, the declared requirement, and the
tool targets agree, so the next drift fails CI instead of surviving in prose.

### Reproducible installation via a pip constraints file

`constraints.txt` at the repository root pins the exact version of every
package in the supported environment. Installation is:

```
python -m pip install -e ".[dev]" -c constraints.txt
```

Why this mechanism and not a lock tool:

- The project installs with plain pip today. A constraints file is native to
  pip, adds **zero new tools**, and changes nothing about how the project is
  built or packaged. Adopting Poetry, PDM, Hatch-env, or uv for this alone
  would be a dependency-manager migration made casually — exactly what this
  phase forbids.
- A constraints file is **cross-platform by construction**: it installs
  nothing itself, it only caps whatever pip decides to install. A package that
  exists on one platform only (colorama, a Windows-only pytest dependency) is
  an inert line elsewhere, where a frozen requirements list would fail.
- Regeneration: any developer, via
  `python -m pip freeze --exclude-editable > constraints.txt` from a clean
  constrained install, followed by header restoration, diff review, and a full
  test run. Upgrades are therefore deliberate, reviewed commits.
- CI consumes it directly (`-c constraints.txt` in the install step), so the
  gate runs against the same resolved versions as developers.
- Runtime and dev dependencies are pinned together, deliberately: the project
  ships one supported environment, and it is the one the tests actually run
  in. Splitting the pin sets buys nothing until a deployment target exists
  that must exclude dev tools.

**Honest scope.** These are *version* pins, not *hash* pins. They make
resolution deterministic and auditable; they do not authenticate downloaded
artifacts, freeze wheels per platform, or protect against an index serving
different content for the same version. Hash-level, per-platform locking
(pip's `--require-hashes`, or a lock tool if one is ever adopted for its own
merits) is future work and must arrive as its own decision. Claims of "full
reproducibility" are not made: the guarantee is *same commit → same resolved
package versions*.

### Line endings: LF in the repository and in the working tree

`.gitattributes` declares `* text=auto eol=lf` with explicit per-type entries;
Windows shell scripts (`.ps1`, `.bat`, `.cmd`) are the one CRLF exception, and
Parquet/archive/image formats are marked `binary`. Every tracked file already
stored LF in the index, so the policy was introduced **without rewriting a
single blob** — verified by running a renormalization dry-run that changed
zero bytes. Uniform LF makes committed fixtures byte-identical on Windows and
Ubuntu, which matters because storage tests hash file contents. Consequences
for developers are documented in the file itself: explicit attributes override
`core.autocrlf`, existing working trees keep their endings until files are
re-checked out, and the skew is invisible to `git status`.

### Typed package marker

`src/quant_research_terminal/py.typed` is added and verified present in the
built wheel (Hatchling packages it by default; a regression test checks the
installed package exposes it), so downstream consumers' type checkers use the
package's annotations instead of treating it as untyped.

## Alternatives considered

**Declare tzdata only on Windows (`sys_platform == 'win32'`).** Rejected: it
makes the dependency set platform-dependent for no saving — the package is
tiny — and leaves non-Windows platforms one stripped container image away from
the same Rust panic.

**Catch `PanicException` around Polars calls.** Rejected outright: it hides a
missing dependency behind a caught crash, the panic is not a stable API, and
the correct data is unavailable either way.

**Adopt a locking tool (uv, pip-tools, Poetry).** Deferred. Each brings real
benefits (hash pinning, per-platform resolution) and a real cost (a new tool
in every developer and CI workflow). The constraints file captures most of the
value at none of the cost; a lock tool should be adopted, if ever, for its own
merits in its own ADR.

**Pin dependencies directly in `pyproject.toml` (`==` ranges).** Rejected:
it conflates the library's compatibility claim with the environment's resolved
state, and makes every routine upgrade a metadata change to the package
itself.

## Consequences

Positive:

- A clean 3.12 install on a stock Windows machine now yields a working
  environment — including timestamp materialization through PyArrow and
  Polars — with no implicit reliance on system timezone data or on whatever
  the CI runner image happens to contain.
- Same commit → same resolved versions, on both CI platforms and developer
  machines; upgrades become visible, reviewable diffs.
- Version claims can no longer drift silently: a test enforces agreement.

Negative:

- `constraints.txt` must be regenerated deliberately when upgrading; pip will
  refuse resolutions that conflict with it, which is the point but adds a
  step.
- Version pins without hashes still trust the package index; stated openly
  above rather than papered over.
