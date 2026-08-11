"""Enforces the layering rules from PROJECT_RULES.md as executable tests.

Dependency direction is an architectural invariant, and an invariant that is
only written down erodes quietly: one convenient import at a time, each
defensible on its own. Parsing the import graph turns the rule into something
CI can fail on.

Allowed direction::

    Providers -> Validation -> Normalization -> Domain -> Storage

The checks read import statements statically rather than importing the modules,
so a violation is reported as a boundary breach rather than surfacing later as
a circular-import error.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import quant_research_terminal

PACKAGE_ROOT = Path(quant_research_terminal.__file__).parent
ROOT_MODULE = "quant_research_terminal"

STORAGE = f"{ROOT_MODULE}.data"
DOMAIN = f"{ROOT_MODULE}.domain"
UI = f"{ROOT_MODULE}.ui"
DATA_IMPORT = f"{ROOT_MODULE}.data_import"
PROVIDERS = f"{DATA_IMPORT}.providers"
APPLICATION = f"{ROOT_MODULE}.application"

#: The one vendor-neutral module inside the providers package. The application
#: layer may import the provider *interface* from it; every other module under
#: ``providers`` is a concrete implementation the application must never touch.
PROVIDER_INTERFACE = f"{PROVIDERS}.provider"


def _module_name(path: Path) -> str:
    relative = path.relative_to(PACKAGE_ROOT).with_suffix("")
    parts = [part for part in relative.parts if part != "__init__"]
    return ".".join([ROOT_MODULE, *parts])


def _resolve_relative(module: str, node: ast.ImportFrom) -> str:
    """Return the absolute package a relative ``from . import x`` refers to.

    ``from .bar import Bar`` inside ``quant_research_terminal.domain.__init__``
    means ``quant_research_terminal.domain.bar``. Resolving it matters: the
    checks below match on absolute prefixes, so an unresolved relative import is
    an invisible import, and a boundary breach written as ``from ..data import
    x`` would pass every test in this file while violating the rule it claims to
    enforce.

    ``node.level`` counts leading dots. One dot means the module's own package,
    so ``level`` parts are dropped from the *package* path — which for a package
    ``__init__`` is the module name itself, since ``_module_name`` already
    strips ``__init__``.
    """
    parts = module.split(".")
    package = parts if _is_package_init(module) else parts[:-1]
    trimmed = package[: len(package) - (node.level - 1)] if node.level > 1 else package
    return ".".join([*trimmed, node.module]) if node.module else ".".join(trimmed)


def _is_package_init(module: str) -> bool:
    """Return whether ``module`` names a package rather than a plain module."""
    relative = Path(*module.split(".")[1:])
    return (PACKAGE_ROOT / relative / "__init__.py").is_file() or module == ROOT_MODULE


def _imports(path: Path, module: str) -> set[str]:
    """Return every module name imported by the file at ``path``.

    Both absolute and relative ``from`` imports are reported, as absolute
    names.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module if node.level == 0 else _resolve_relative(module, node)
            if not base:
                continue
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)

    return found


def _modules_under(prefix: str) -> list[tuple[str, set[str]]]:
    """Return ``(module_name, imported_names)`` for every module under ``prefix``."""
    return [
        (name, _imports(path, name))
        for path in sorted(PACKAGE_ROOT.rglob("*.py"))
        if (name := _module_name(path)) == prefix or name.startswith(f"{prefix}.")
    ]


def _violations(*, source_prefix: str, forbidden_prefix: str) -> list[str]:
    return [
        f"{module} imports {imported_name}"
        for module, imported in _modules_under(source_prefix)
        for imported_name in sorted(imported)
        if imported_name == forbidden_prefix or imported_name.startswith(f"{forbidden_prefix}.")
    ]


def test_the_package_is_actually_being_scanned() -> None:
    # Guards the tests below from silently passing on an empty module list.
    assert len(_modules_under(DATA_IMPORT)) >= 8
    assert len(_modules_under(PROVIDERS)) >= 4


def test_storage_does_not_import_data_import() -> None:
    assert _violations(source_prefix=STORAGE, forbidden_prefix=DATA_IMPORT) == []


def test_domain_does_not_import_data_import() -> None:
    assert _violations(source_prefix=DOMAIN, forbidden_prefix=DATA_IMPORT) == []


def test_domain_does_not_import_storage() -> None:
    # The domain layer is the foundation: it depends on nothing above it.
    assert _violations(source_prefix=DOMAIN, forbidden_prefix=STORAGE) == []


def test_ui_does_not_import_data_import() -> None:
    assert _violations(source_prefix=UI, forbidden_prefix=DATA_IMPORT) == []


def test_ui_does_not_import_storage() -> None:
    assert _violations(source_prefix=UI, forbidden_prefix=STORAGE) == []


def test_validation_does_not_import_providers() -> None:
    # RawRecord lives outside the providers package precisely so that
    # validation can consume provider output without importing vendor code.
    assert _violations(source_prefix=f"{DATA_IMPORT}.validation", forbidden_prefix=PROVIDERS) == []


def test_normalization_does_not_import_providers() -> None:
    assert (
        _violations(source_prefix=f"{DATA_IMPORT}.normalization", forbidden_prefix=PROVIDERS) == []
    )


def test_raw_record_does_not_import_providers() -> None:
    assert _violations(source_prefix=f"{DATA_IMPORT}.raw_record", forbidden_prefix=PROVIDERS) == []


def test_event_order_does_not_import_providers() -> None:
    assert _violations(source_prefix=f"{DATA_IMPORT}.event_order", forbidden_prefix=PROVIDERS) == []


def test_orchestration_does_not_import_providers() -> None:
    # pipeline.py coordinates stages that are handed to it; binding it to a
    # concrete provider would make the batch API untestable without a vendor.
    assert _violations(source_prefix=f"{DATA_IMPORT}.pipeline", forbidden_prefix=PROVIDERS) == []


def test_providers_do_not_import_storage_directly() -> None:
    # Provider output is storage-independent; anything a provider needs from
    # the storage contract reaches it through the import contracts instead.
    assert _violations(source_prefix=PROVIDERS, forbidden_prefix=STORAGE) == []


def test_rule_modules_do_not_import_orchestration() -> None:
    # Rules must not depend on the layer that sequences them, or the two
    # become impossible to test apart.
    for module in ("validation", "normalization", "raw_record", "record_fields", "event_order"):
        assert (
            _violations(
                source_prefix=f"{DATA_IMPORT}.{module}",
                forbidden_prefix=f"{DATA_IMPORT}.pipeline",
            )
            == []
        )


def test_semantics_modules_depend_on_nothing_in_the_import_layer() -> None:
    # time_semantics, numeric_semantics, and instrument_semantics are the
    # authoritative leaf rules; they must stay free of every stage that
    # consumes them.
    for module in ("time_semantics", "numeric_semantics", "instrument_semantics"):
        _, imported = _modules_under(f"{DATA_IMPORT}.{module}")[0]
        internal = {name for name in imported if name.startswith(f"{DATA_IMPORT}.")}
        assert internal == set()


def test_relative_imports_are_resolved_by_the_boundary_checks() -> None:
    """The checks must see relative imports, or they can be evaded by syntax.

    ``domain/__init__.py`` imports its siblings relatively. If the import graph
    were built from absolute ``from`` statements only, those edges would be
    invisible and ``from ..data import x`` in that file would breach the
    domain-does-not-import-storage rule while every test here still passed.
    """
    domain_init = dict(_modules_under(DOMAIN))[DOMAIN]

    assert f"{DOMAIN}.futures_contract" in domain_init
    assert f"{DOMAIN}.futures_contract.FuturesContractId" in domain_init


# --------------------------------------------------------------------------
# Futures identity (Phase 2.0, ADR-009)
#
# Identity belongs at the lowest layer. It must be usable by replay,
# execution, and cataloguing later without any of them dragging in providers,
# import infrastructure, or storage.
# --------------------------------------------------------------------------

IDENTITY_MODULES = (f"{DOMAIN}.futures_contract", f"{DOMAIN}.contract_month")


def test_identity_modules_exist_and_are_scanned() -> None:
    for module in IDENTITY_MODULES:
        assert _modules_under(module), f"{module} was not scanned"


def test_identity_modules_depend_on_nothing_above_the_domain() -> None:
    # Enumerated rather than expressed as "not storage": the point is that
    # identity depends on *nothing* upward, so a layer added later is covered
    # without this test being remembered.
    for module in IDENTITY_MODULES:
        _, imported = _modules_under(module)[0]
        outward = {
            name
            for name in imported
            if name.startswith(f"{ROOT_MODULE}.") and not name.startswith(f"{DOMAIN}.")
        }
        assert outward == set(), f"{module} depends outward on {sorted(outward)}"


def test_identity_modules_do_not_read_the_wall_clock() -> None:
    """No current date, time, or timezone may influence an identity.

    A parser that resolves ``ESM6`` using the current decade produces a
    different identity in 2026 than in 2036 for the same input, which makes
    stored research irreproducible. The prohibition is structural: the modules
    do not import the machinery at all.
    """
    forbidden = {"datetime", "time", "calendar", "zoneinfo", "random", "uuid", "os", "locale"}

    for module in IDENTITY_MODULES:
        _, imported = _modules_under(module)[0]
        roots = {name.split(".")[0] for name in imported}
        assert roots & forbidden == set(), f"{module} imports {sorted(roots & forbidden)}"


# --------------------------------------------------------------------------
# Application layer (Phase 1.9, ADR-007)
#
# Application orchestrates downward — data_import, storage, domain — and is
# depended on only from above (UI, later). Nothing below it may know it
# exists, and it may not reach into vendor code or the UI.
# --------------------------------------------------------------------------


def test_the_application_package_is_actually_being_scanned() -> None:
    assert len(_modules_under(APPLICATION)) >= 3


def test_application_does_not_import_ui() -> None:
    assert _violations(source_prefix=APPLICATION, forbidden_prefix=UI) == []


def test_domain_does_not_import_application() -> None:
    assert _violations(source_prefix=DOMAIN, forbidden_prefix=APPLICATION) == []


def test_storage_does_not_import_application() -> None:
    assert _violations(source_prefix=STORAGE, forbidden_prefix=APPLICATION) == []


def test_data_import_does_not_import_application() -> None:
    # Covers the providers subpackage too: nothing below the application
    # layer may depend on it, or the dependency direction inverts.
    assert _violations(source_prefix=DATA_IMPORT, forbidden_prefix=APPLICATION) == []


# --------------------------------------------------------------------------
# Exchange calendars (Phase 2.1, ADR-010)
#
# Calendar semantics live in the domain; calendar *data* (TOML definitions and
# their loader) lives in the `calendars` package, which depends on the domain
# and on nothing else in the project. The domain must never depend back on the
# data package, or facts and semantics become circular.
# --------------------------------------------------------------------------

CALENDARS = f"{ROOT_MODULE}.calendars"

CALENDAR_DOMAIN_MODULES = (
    f"{DOMAIN}.exchange_calendar",
    f"{DOMAIN}.calendar_definition",
    f"{DOMAIN}.calendar_materialization",
)


def test_calendar_modules_exist_and_are_scanned() -> None:
    for module in (*CALENDAR_DOMAIN_MODULES, CALENDARS):
        assert _modules_under(module), f"{module} was not scanned"


def test_domain_does_not_import_the_calendars_data_package() -> None:
    assert _violations(source_prefix=DOMAIN, forbidden_prefix=CALENDARS) == []


def test_calendars_package_depends_only_on_the_domain() -> None:
    for module, imported in _modules_under(CALENDARS):
        outward = {
            name
            for name in imported
            if name.startswith(f"{ROOT_MODULE}.")
            and not name.startswith(f"{DOMAIN}.")
            and name != CALENDARS
            and not name.startswith(f"{CALENDARS}.")
        }
        assert outward == set(), f"{module} depends outward on {sorted(outward)}"


#: Phase 2.2 rollover modules (ADR-011). Listed for the canary below; the
#: sweep itself is token-based so a module added later is covered by default.
ROLLOVER_DOMAIN_MODULES = (
    f"{DOMAIN}.rollover",
    f"{DOMAIN}.rollover_definition",
    f"{DOMAIN}.rollover_materialization",
    f"{DOMAIN}.contract_lifecycle",
    f"{DOMAIN}.continuous_series",
)

#: Name tokens marking a module whose subject is time or instrument lifetime.
#: Every such module is held to the no-ambient-state and no-wall-clock rules.
#: ``time`` is in the list because ``domain/time.py`` owns the canonical UTC
#: validation *and* ``epoch_microseconds``, the instant encoding both content
#: hashes are built from. It is the single most wall-clock-sensitive module in
#: the package, and it matched none of the subject tokens.
_TIME_SENSITIVE_NAME_TOKENS = (
    "calendar",
    "rollover",
    "lifecycle",
    "continuous",
    "time",
    # ``domain/replay.py`` owns the availability-time semantics the entire
    # anti-look-ahead story rests on. A wall-clock read or an ambient-state
    # dependency there would make a replay's own notion of "now" depend on when
    # it was run, which is the failure this sweep exists to catch.
    "replay",
    # ``domain/dataset_identity.py`` owns the canonical semantic encoding: the
    # one module whose entire purpose is producing the same bytes on every
    # machine, forever. It matched none of the subject tokens above, so without
    # this it would have been covered by no determinism guard at all.
    "dataset",
)


def _calendar_modules() -> list[tuple[str, set[str]]]:
    """Every time-sensitive module, discovered by sweep rather than enumeration.

    A hard-coded module list covers exactly the files that exist on the day it
    is written; a `calendars/registry.py` or a new `domain/roll_*.py` added
    later would escape it. The sweep takes the whole `calendars` package plus
    every domain module whose name mentions one of the time-sensitive subjects,
    so additions are covered by default.
    """
    found = _modules_under(CALENDARS)
    for module, imported in _modules_under(DOMAIN):
        basename = module.rsplit(".", 1)[-1]
        if any(token in basename for token in _TIME_SENSITIVE_NAME_TOKENS):
            found.append((module, imported))
    return found


def test_the_calendar_sweep_finds_the_known_modules() -> None:
    names = {module for module, _ in _calendar_modules()}
    for module in (*CALENDAR_DOMAIN_MODULES, CALENDARS, f"{CALENDARS}.loader"):
        assert module in names, f"{module} escaped the calendar sweep"


def test_the_sweep_covers_the_shared_time_module() -> None:
    """``domain/time.py`` owns the instant encoding both content hashes use.

    It matched none of the subject tokens, so the module most in need of the
    no-wall-clock guarantee was the one module not covered by it.
    """
    names = {module for module, _ in _calendar_modules()}
    assert f"{DOMAIN}.time" in names


def test_the_sweep_also_finds_the_rollover_modules() -> None:
    """Regression for the sweep's own blind spot.

    None of the Phase 2.2 module names contain "calendar", so the Phase 2.1
    sweep would have covered none of them — the ambient-state ban and the
    wall-clock scan would have silently stopped applying to new code. That is
    ADR-010's defect D9 re-opening, and this canary is what keeps it shut.
    """
    names = {module for module, _ in _calendar_modules()}
    for module in ROLLOVER_DOMAIN_MODULES:
        assert module in names, f"{module} escaped the time-sensitive module sweep"


def test_rollover_modules_depend_on_nothing_above_the_domain() -> None:
    for module in ROLLOVER_DOMAIN_MODULES:
        _, imported = _modules_under(module)[0]
        outward = {
            name
            for name in imported
            if name.startswith(f"{ROOT_MODULE}.") and not name.startswith(f"{DOMAIN}.")
        }
        assert outward == set(), f"{module} depends outward on {sorted(outward)}"


def test_the_sweep_covers_the_dataset_identity_module() -> None:
    """Canary for the module the phase's determinism claims rest on."""
    names = {module for module, _ in _calendar_modules()}
    assert f"{DOMAIN}.dataset_identity" in names


# --------------------------------------------------------------------------
# Dataset catalog (Phase 2.3, ADR-012)
#
# The catalog composes the domain's identity types with the storage layer's
# files. It sits above both and is imported by neither.
#
# Its rule set is deliberately *not* the time-sensitive one: publishing a
# manifest requires ``os.replace``, which that sweep bans. Randomness, UUIDs,
# and wall-clock reads stay forbidden, because those are what would make an
# immutable identity stop being reproducible.
# --------------------------------------------------------------------------

CATALOG = f"{ROOT_MODULE}.catalog"

CATALOG_MODULES = (
    CATALOG,
    f"{CATALOG}.comparison",
    f"{CATALOG}.errors",
    f"{CATALOG}.index",
    f"{CATALOG}.lineage",
    f"{CATALOG}.manifest",
    f"{CATALOG}.migration",
    f"{CATALOG}.provenance",
    f"{CATALOG}.store",
)


def test_the_catalog_package_is_actually_being_scanned() -> None:
    names = {module for module, _ in _modules_under(CATALOG)}
    for module in CATALOG_MODULES:
        assert module in names, f"{module} escaped the catalog scan"


def test_domain_does_not_import_the_catalog() -> None:
    assert _violations(source_prefix=DOMAIN, forbidden_prefix=CATALOG) == []


def test_storage_does_not_import_the_catalog() -> None:
    assert _violations(source_prefix=STORAGE, forbidden_prefix=CATALOG) == []


def test_data_import_does_not_import_the_catalog() -> None:
    assert _violations(source_prefix=DATA_IMPORT, forbidden_prefix=CATALOG) == []


def test_the_application_layer_does_not_import_the_catalog() -> None:
    """No ambient cross-batch state anywhere in the import path.

    If ``ImportDatasetUseCase`` could consult the catalog, an import's outcome
    would depend on which unrelated datasets happen to sit in the local catalog —
    the same input and configuration would stop producing the same result. Phase
    2.4's comparison is an explicit later call, never something that happens
    during import.
    """
    assert _violations(source_prefix=APPLICATION, forbidden_prefix=CATALOG) == []


# --------------------------------------------------------------------------
# Dataset lineage (Phase 2.4, ADR-013)
# --------------------------------------------------------------------------

#: The complete public surface of the lineage and comparison modules.
#:
#: An **allowlist**, not a denylist of suspicious words. A denylist only catches
#: the names its author thought of: `newest_successor`, `head_of`, `tip`, and
#: `winning_revision` would all sail past a list of "latest"/"preferred"/"merge"
#: substrings while doing precisely the forbidden thing. Pinning the exact
#: surface instead means **any** new public function fails this test until
#: somebody adds it here deliberately — which is the moment to ask whether it
#: selects a winner, merges data, or redirects a pinned identity.
#:
#: A branching correction DAG has no natural "latest", and a linear chain must
#: not be allowed to imply one. A future caller pins an exact ``ManifestHash`` or
#: applies an explicit policy of its own; nothing here may choose for it.
EXPECTED_LINEAGE_API: dict[str, frozenset[str]] = {
    f"{DOMAIN}.dataset_lineage": frozenset(
        {
            "RelationProvenance",
            "RelationProvenanceKind",
            "SupersedesRelation",
            "SupersedesRelationHash",
            "build_supersedes_relation",
            "canonical_supersedes_bytes",
            "compute_supersedes_relation_hash",
            "operator_declared",
            "relation_to_json_bytes",
            "require_relation_provenance_kind",
            "supersedes_canonical_payload",
        }
    ),
    f"{CATALOG}.lineage": frozenset(
        {
            "LineageGraph",
            "LineageIndex",
            "LineageIndexEntry",
            "empty_lineage_index",
            "find_cycle",
            "lineage_directory",
            "lineage_index_from_json_bytes",
            "lineage_index_path",
            "load_lineage_graph",
            "load_lineage_index",
            "publish_supersedes_relation",
            "published_relations",
            "read_relation",
            "rebuild_lineage_index",
            "relation_from_json_bytes",
            "relation_path",
            "unpublished_relation_partials",
            "validate_supersedes_relation",
            "verify_lineage_acyclic",
            "verify_lineage_index",
            "write_lineage_index",
        }
    ),
    f"{CATALOG}.comparison": frozenset(
        {
            "BarKey",
            "BarKeyComparison",
            "DatasetComparison",
            "bar_key",
            "compare_datasets",
        }
    ),
}

#: Method names on the graph that would turn an audit tool into a policy.
FORBIDDEN_GRAPH_CONCEPTS = (
    "latest",
    "current",
    "preferred",
    "active",
    "newest",
    "head",
    "tip",
    "winner",
    "winning",
    "best",
    "choose",
    "select",
    "resolve",
    "merge",
    "reconcile",
    "apply",
    "revision",
    "prefer",
)


def test_the_lineage_modules_are_actually_being_scanned() -> None:
    names = {module for module, _ in _modules_under(ROOT_MODULE)}
    for module in (f"{DOMAIN}.dataset_lineage", f"{CATALOG}.lineage", f"{CATALOG}.comparison"):
        assert module in names, f"{module} escaped the scan"


def test_the_lineage_domain_module_depends_on_nothing_above_the_domain() -> None:
    _, imported = _modules_under(f"{DOMAIN}.dataset_lineage")[0]
    outward = {
        name
        for name in imported
        if name.startswith(f"{ROOT_MODULE}.") and not name.startswith(f"{DOMAIN}.")
    }
    assert outward == set(), f"dataset_lineage depends outward on {sorted(outward)}"


def test_the_lineage_domain_module_is_covered_by_the_determinism_sweep() -> None:
    """Canary: the relation hash must not acquire a clock or a random source."""
    names = {module for module, _ in _calendar_modules()}
    assert f"{DOMAIN}.dataset_lineage" in names


def _public_api(module: str) -> frozenset[str]:
    """Return the public top-level names a module defines.

    Parsed from the source rather than imported, so a name is caught by its
    definition rather than by whatever the module happens to re-export.
    """
    path = PACKAGE_ROOT / Path(*module.split(".")[1:]).with_suffix(".py")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return frozenset(
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and not node.name.startswith("_")
    )


@pytest.mark.parametrize("module", sorted(EXPECTED_LINEAGE_API))
def test_the_lineage_public_surface_is_exactly_what_was_reviewed(module: str) -> None:
    """An allowlist, so a new public name must be added here deliberately.

    A denylist of suspicious substrings only catches the names its author
    imagined; ``newest_successor`` or ``head_of`` would pass one while selecting
    a winner. Pinning the surface makes every addition a decision.
    """
    assert _public_api(module) == EXPECTED_LINEAGE_API[module]


@pytest.mark.parametrize("module", sorted(EXPECTED_LINEAGE_API))
def test_no_public_lineage_name_expresses_selection_or_reconciliation(module: str) -> None:
    """Graph navigation is informational; it never picks a winner or merges.

    Public names only. A private ``_resolve_artifact`` resolves a *path* and is
    nobody's revision policy, and the exact public surface is pinned by the test
    above in any case — so this checks the names a caller can actually reach,
    where these words really would mean what they say.
    """
    offenders = [
        name
        for name in _public_api(module)
        for concept in FORBIDDEN_GRAPH_CONCEPTS
        if concept in name.lower()
    ]
    assert offenders == [], f"{module} exposes selection-flavoured names: {offenders}"


def test_the_catalog_namespace_exposes_no_selection_or_merge_entry_point() -> None:
    """The package surface a caller actually reaches for."""
    from quant_research_terminal import catalog

    offenders = [
        name
        for name in catalog.__all__
        for concept in ("latest", "current", "preferred", "merge", "reconcile", "newest", "best")
        if concept in name.lower()
    ]
    assert offenders == [], f"a selection or merge entry point appeared: {offenders}"


def test_lineage_does_not_reach_for_replay_execution_or_strategy() -> None:
    """None of these exist. The assertion is that none appears later either."""
    for module in (f"{DOMAIN}.dataset_lineage", f"{CATALOG}.lineage", f"{CATALOG}.comparison"):
        for absent in ("replay", "execution", "strategy", "portfolio", "backtest"):
            assert (
                _violations(source_prefix=module, forbidden_prefix=f"{ROOT_MODULE}.{absent}") == []
            )
        assert _violations(source_prefix=module, forbidden_prefix=UI) == []
        assert _violations(source_prefix=module, forbidden_prefix=PROVIDERS) == []


def test_the_schema_version_was_not_bumped_for_lineage() -> None:
    """Trade and quote lack persisted event identity, and that is accepted.

    Inventing a vendor sequence column to improve correction matching would put
    one vendor's semantics into the canonical market-data schema.
    """
    from quant_research_terminal.data.contracts import (
        SCHEMA_VERSION,
        SUPPORTED_SCHEMA_VERSIONS,
    )

    assert SCHEMA_VERSION == 3
    assert SUPPORTED_SCHEMA_VERSIONS == frozenset({2, 3})


def test_the_catalog_does_not_import_ui_or_providers() -> None:
    assert _violations(source_prefix=CATALOG, forbidden_prefix=UI) == []
    assert _violations(source_prefix=CATALOG, forbidden_prefix=PROVIDERS) == []


def test_the_catalog_does_not_import_replay_execution_or_strategy() -> None:
    """None of these exist. The assertion is that none appears later either."""
    for absent in ("replay", "execution", "strategy", "portfolio", "backtest"):
        assert _violations(source_prefix=CATALOG, forbidden_prefix=f"{ROOT_MODULE}.{absent}") == []


def test_the_catalog_introduces_no_randomness_or_wall_clock() -> None:
    """An identity that varies per run is not an identity."""
    forbidden = {"random", "uuid", "secrets"}
    for module, imported in _modules_under(CATALOG):
        roots = {name.split(".")[0] for name in imported}
        assert roots & forbidden == set(), f"{module} imports {sorted(roots & forbidden)}"


def test_the_catalog_never_reads_the_wall_clock() -> None:
    banned_calls = {"now", "today", "utcnow", "fromtimestamp", "monotonic"}
    covered = {module for module, _ in _modules_under(CATALOG)}
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        module = _module_name(path)
        if module not in covered:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in banned_calls
            ):
                raise AssertionError(
                    f"{module} calls .{node.func.attr}() at line {node.lineno}; a dataset "
                    f"identity must not depend on when it was computed"
                )


#: Identity-critical modules that neither sweep reaches.
#:
#: ``data/artifact_hash.py`` computes the *physical* identity and lives under
#: ``data``, which the time-sensitive sweep does not walk; ``domain/common.py``
#: is now home to the re-validating ``model_copy`` that every frozen identity
#: model inherits. Both matched no subject token, so the modules holding half
#: the phase's guarantees were covered by no determinism guard at all — the same
#: blind spot ADR-010's D9 opened, in a different package.
IDENTITY_CRITICAL_MODULES = (
    f"{STORAGE}.artifact_hash",
    f"{DOMAIN}.common",
)


def test_identity_critical_modules_are_actually_present() -> None:
    """A canary: a renamed module must fail loudly, not stop being checked."""
    names = {module for module, _ in _modules_under(ROOT_MODULE)}
    for module in IDENTITY_CRITICAL_MODULES:
        assert module in names, f"{module} is missing; the guard below now checks nothing"


def test_identity_critical_modules_introduce_no_randomness_or_ambient_state() -> None:
    forbidden = {"random", "uuid", "secrets", "locale", "time"}
    for module in IDENTITY_CRITICAL_MODULES:
        _, imported = _modules_under(module)[0]
        roots = {name.split(".")[0] for name in imported}
        assert roots & forbidden == set(), f"{module} imports {sorted(roots & forbidden)}"


def test_identity_critical_modules_never_read_the_wall_clock() -> None:
    banned_calls = {"now", "today", "utcnow", "fromtimestamp", "monotonic"}
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        if _module_name(path) not in IDENTITY_CRITICAL_MODULES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in banned_calls
            ):
                raise AssertionError(
                    f"{_module_name(path)} calls .{node.func.attr}() at line {node.lineno}; "
                    f"an artifact's identity must not depend on when it was computed"
                )


def test_the_manifest_module_holds_no_filesystem_path_type() -> None:
    """Structural check that location cannot leak into immutable identity."""
    _, imported = _modules_under(f"{CATALOG}.manifest")[0]
    assert "pathlib" not in {name.split(".")[0] for name in imported}


def test_rollover_modules_do_not_import_the_calendars_data_package() -> None:
    # Rollover consumes the calendar *domain* types; the TOML definitions and
    # their loader are data the caller supplies, not something rollover reaches
    # for. Importing them would make a domain rule depend on shipped facts.
    for module in ROLLOVER_DOMAIN_MODULES:
        assert _violations(source_prefix=module, forbidden_prefix=CALENDARS) == []


def test_calendar_modules_do_not_import_ambient_state_machinery() -> None:
    # The calendar legitimately imports datetime and zoneinfo — it is the one
    # domain area whose subject *is* time — so the identity-module blanket ban
    # does not apply. What stays banned is machinery for ambient, run-varying
    # state: randomness, process identity, locale, and environment access.
    forbidden = {"random", "uuid", "os", "locale", "time"}
    for module, imported in _calendar_modules():
        roots = {name.split(".")[0] for name in imported}
        assert roots & forbidden == set(), f"{module} imports {sorted(roots & forbidden)}"


def test_calendar_modules_never_read_the_wall_clock() -> None:
    """`datetime.now`, `datetime.today`, `date.today`, and `datetime.utcnow`
    must not appear in any calendar module: a calendar answer that depends on
    when it was computed is irreproducible by construction. This is a source
    scan rather than an import check because the `datetime` module itself is
    legitimately imported."""
    banned_calls = {"now", "today", "utcnow", "fromtimestamp"}
    covered = {module for module, _ in _calendar_modules()}
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        module = _module_name(path)
        if module not in covered:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in banned_calls
            ):
                raise AssertionError(
                    f"{module} calls .{node.func.attr}() at line {node.lineno}; "
                    f"calendar code must never read the wall clock"
                )


def test_application_does_not_import_provider_implementations() -> None:
    # The application accepts any MarketDataProvider through the interface
    # module and must never bind to a vendor. Everything under ``providers``
    # except the interface module itself is a concrete implementation.
    violations = [
        violation
        for violation in _violations(source_prefix=APPLICATION, forbidden_prefix=PROVIDERS)
        if not violation.endswith(f"imports {PROVIDER_INTERFACE}")
        and f"imports {PROVIDER_INTERFACE}." not in violation
    ]
    assert violations == []


# --------------------------------------------------------------------------
# Deterministic replay (Phase 3, ADR-014)
#
# Replay composes the domain's market records with the catalog's verified
# artifacts. It sits above both and is imported by neither, and it must stay
# free of every concern the phase deliberately excludes: market state, strategy
# callbacks, execution, a mutable clock, seeking, and revision selection.
# --------------------------------------------------------------------------

REPLAY = f"{ROOT_MODULE}.replay"
DOMAIN_REPLAY = f"{DOMAIN}.replay"

REPLAY_MODULES = (
    REPLAY,
    f"{REPLAY}.errors",
    f"{REPLAY}.prepare",
    f"{REPLAY}.stream",
)


def _public_module_names(module: str) -> frozenset[str]:
    """Return every public top-level name a module *defines*.

    Broader than :func:`_public_api`, which sees only classes and functions.
    A replay module's surface can also grow a module constant or a ``type``
    alias, and either is just as reachable by a caller — a ``RECORD_PRIORITY``
    table or a ``ReplayClock`` alias would sail past a class-and-function scan
    while doing precisely the forbidden thing.
    """
    path = PACKAGE_ROOT / Path(*module.split(".")[1:])
    path = path / "__init__.py" if path.is_dir() else path.with_suffix(".py")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            found.add(node.name)
        elif isinstance(node, ast.TypeAlias) and isinstance(node.name, ast.Name):
            found.add(node.name.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found.add(node.target.id)
        elif isinstance(node, ast.Assign):
            found.update(target.id for target in node.targets if isinstance(target, ast.Name))
    return frozenset(name for name in found if not name.startswith("_"))


#: The complete public surface of every replay module.
#:
#: An **allowlist**, for the ADR-013 reason: a denylist of suspicious substrings
#: only catches the names its author imagined. ``resume_from``, ``fast_forward``,
#: ``EVENT_PRIORITY``, and ``wall_clock_pace`` would each pass a keyword filter
#: while introducing exactly what Phase 3 refuses. Pinning the surface makes any
#: new public name fail this test until somebody adds it here deliberately —
#: which is the moment to ask whether it paces a clock, seeks, keeps market
#: state, reconciles datasets, or invents a causal order.
EXPECTED_REPLAY_API: dict[str, frozenset[str]] = {
    DOMAIN_REPLAY: frozenset(
        {
            "ReplayConfig",
            "ReplayEvent",
            "ReplayFrame",
            "ReplayPayload",
            "ReplayRange",
            "frame_event_sort_key",
        }
    ),
    f"{REPLAY}.errors": frozenset(
        {
            "AmbiguousReplayOverlapError",
            "DuplicateReplaySemanticDatasetError",
            "NonMonotonicReplaySourceError",
            "ReplayArtifactVerificationError",
            "ReplayError",
            "ReplayInvariantError",
            "UnsupportedReplaySchemaError",
        }
    ),
    f"{REPLAY}.prepare": frozenset({"PreparedReplay", "prepare_replay"}),
    f"{REPLAY}.stream": frozenset(
        {
            "PreparedRow",
            "PreparedSource",
            "build_prepared_source",
            "frame_timeline",
            "validate_source_monotonicity",
        }
    ),
}

#: Concepts that must not appear in a replay name during this phase.
#:
#: The allowlist above is the real guard; this is the readable statement of
#: *why* a new name would be refused, and it fails on the package's own
#: ``__all__`` too, which is the surface a caller actually reaches for.
FORBIDDEN_REPLAY_CONCEPTS = (
    "clock",
    "seek",
    "checkpoint",
    "restore",
    "resume",
    "rewind",
    "jump",
    "advance",
    "sleep",
    "pace",
    "speed",
    "tick",
    "latest",
    "current",
    "newest",
    "preferred",
    "merge",
    "reconcile",
    "marketstate",
    "strategy",
    "callback",
    "order",
    "fill",
    "portfolio",
    "position",
    "priority",
)


def test_the_replay_packages_are_actually_being_scanned() -> None:
    names = {module for module, _ in _modules_under(ROOT_MODULE)}
    for module in (*REPLAY_MODULES, DOMAIN_REPLAY):
        assert module in names, f"{module} escaped the scan"


def test_the_pure_replay_module_depends_on_nothing_above_the_domain() -> None:
    _, imported = _modules_under(DOMAIN_REPLAY)[0]
    outward = {
        name
        for name in imported
        if name.startswith(f"{ROOT_MODULE}.") and not name.startswith(f"{DOMAIN}.")
    }
    assert outward == set(), f"domain.replay depends outward on {sorted(outward)}"


def test_the_domain_does_not_import_the_replay_orchestration_package() -> None:
    assert _violations(source_prefix=DOMAIN, forbidden_prefix=REPLAY) == []


@pytest.mark.parametrize("layer", [STORAGE, DATA_IMPORT, CATALOG, APPLICATION, UI, CALENDARS])
def test_no_layer_beneath_or_beside_replay_imports_it(layer: str) -> None:
    assert _violations(source_prefix=layer, forbidden_prefix=REPLAY) == []


def test_replay_does_not_import_ui_providers_import_or_application() -> None:
    for forbidden in (UI, PROVIDERS, DATA_IMPORT, APPLICATION):
        assert _violations(source_prefix=REPLAY, forbidden_prefix=forbidden) == []


def test_replay_does_not_import_execution_strategy_or_portfolio() -> None:
    """None of these exist. The assertion is that none appears later either."""
    for absent in ("execution", "strategy", "portfolio", "backtest", "market_state", "research"):
        assert _violations(source_prefix=REPLAY, forbidden_prefix=f"{ROOT_MODULE}.{absent}") == []


def test_replay_does_not_import_lineage_navigation_or_comparison() -> None:
    """Exact manifest pins are replayed as themselves; no successor is consulted.

    Structural rather than a policy: the package cannot follow a supersedes edge
    it never imports, and cannot reconcile two datasets with a comparison it
    never reaches for.
    """
    for forbidden in (f"{CATALOG}.lineage", f"{CATALOG}.comparison"):
        assert _violations(source_prefix=REPLAY, forbidden_prefix=forbidden) == []
        assert _violations(source_prefix=DOMAIN_REPLAY, forbidden_prefix=forbidden) == []
    assert _violations(source_prefix=REPLAY, forbidden_prefix=f"{DOMAIN}.dataset_lineage") == []


def test_replay_does_not_require_a_calendar_or_a_roll_schedule() -> None:
    """Base market-data replay derives no trading date and switches no contract."""
    for forbidden in (
        CALENDARS,
        f"{DOMAIN}.exchange_calendar",
        f"{DOMAIN}.calendar_definition",
        f"{DOMAIN}.calendar_materialization",
        f"{DOMAIN}.rollover",
        f"{DOMAIN}.rollover_definition",
        f"{DOMAIN}.rollover_materialization",
        f"{DOMAIN}.continuous_series",
        f"{DOMAIN}.contract_lifecycle",
    ):
        assert _violations(source_prefix=REPLAY, forbidden_prefix=forbidden) == []
        assert _violations(source_prefix=DOMAIN_REPLAY, forbidden_prefix=forbidden) == []


def test_the_pure_replay_module_is_covered_by_the_determinism_sweep() -> None:
    """Canary: availability-time semantics must not acquire a clock.

    ``domain/replay.py`` matched none of the original subject tokens, which is
    the same blind spot ADR-010's D9 opened and ADR-012 re-opened in another
    package. ``replay`` was added to the token list; this keeps it there.
    """
    names = {module for module, _ in _calendar_modules()}
    assert DOMAIN_REPLAY in names


#: Machinery whose whole purpose is ambient, run-varying state.
#:
#: The union of the two pre-existing sweeps in this file (identity-critical at
#: ``test_identity_critical_modules_introduce_no_randomness_or_ambient_state``,
#: calendar at ``test_calendar_modules_do_not_import_ambient_state_machinery``),
#: because replay has no reason to be laxer than either and every reason to be
#: stricter: it is the layer that decides *when* a consumer learns something.
#:
#: ``os`` is banned here although the catalog legitimately needs it for
#: ``os.replace``. Replay is read-only — it publishes nothing — so it has no such
#: need, and taking one would be the change worth failing the build over.
REPLAY_FORBIDDEN_IMPORTS = frozenset({"random", "uuid", "secrets", "time", "locale", "os"})

#: Calls that read a clock or pause. A superset of the two lists already in this
#: file: the earlier ones omitted ``time.time`` and its relatives entirely, so
#: ``import time`` plus ``time.time()`` passed every guard while the identical
#: code in ``domain/dataset_identity.py`` failed the build.
WALL_CLOCK_CALLS = frozenset(
    {
        "now",
        "today",
        "utcnow",
        "utcfromtimestamp",
        "fromtimestamp",
        "monotonic",
        "monotonic_ns",
        "perf_counter",
        "perf_counter_ns",
        "process_time",
        "sleep",
        "time",
        "time_ns",
        "localtime",
        "gmtime",
        "mktime",
    }
)


def test_the_replay_ban_is_no_weaker_than_the_bans_that_predate_it() -> None:
    """A canary against the guard itself regressing.

    The first version of this sweep silently dropped ``time``, ``locale``, and
    ``os`` from the denylist that the identity and calendar sweeps already
    applied — so the modules that actually produce the timeline were held to a
    *lower* standard than the ones that hash it. Pinning the relationship means a
    future edit cannot quietly reopen that gap.
    """
    identity_ban = {"random", "uuid", "secrets", "locale", "time"}
    calendar_ban = {"random", "uuid", "os", "locale", "time"}

    assert REPLAY_FORBIDDEN_IMPORTS >= identity_ban
    assert REPLAY_FORBIDDEN_IMPORTS >= calendar_ban
    assert WALL_CLOCK_CALLS >= {"now", "today", "utcnow", "fromtimestamp", "monotonic"}
    assert "time" in WALL_CLOCK_CALLS


def test_replay_introduces_no_randomness_or_ambient_state() -> None:
    for module, imported in [*_modules_under(REPLAY), *_modules_under(DOMAIN_REPLAY)]:
        roots = {name.split(".")[0] for name in imported}
        offenders = roots & REPLAY_FORBIDDEN_IMPORTS
        assert offenders == set(), f"{module} imports {sorted(offenders)}"


def test_replay_never_reads_the_wall_clock_or_sleeps() -> None:
    """An AST scan, not a substring grep.

    Phase 2.4 found that a text probe matched the modules' own docstrings
    *documenting the absence* of the thing being probed, and reported four
    defects that did not exist. A detector that cannot tell code from a comment
    denying that code will eventually hide a real one, so this reads call nodes.
    """
    banned = WALL_CLOCK_CALLS
    covered = {module for module, _ in [*_modules_under(REPLAY), *_modules_under(DOMAIN_REPLAY)]}
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        module = _module_name(path)
        if module not in covered:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute):
                assert node.func.attr not in banned, (
                    f"{module} calls .{node.func.attr}() at line {node.lineno}; replay time "
                    f"is the frame's availability time and never the machine's"
                )
            if isinstance(node.func, ast.Name):
                assert node.func.id not in banned, (
                    f"{module} calls {node.func.id}() at line {node.lineno}"
                )


@pytest.mark.parametrize(
    "source",
    [
        "import datetime\nx = datetime.datetime.now()\n",
        "import time\nx = time.time()\n",
        "import time\nx = time.monotonic()\n",
        "import time\ntime.sleep(1)\n",
        "import datetime\nx = datetime.date.today()\n",
    ],
)
def test_the_wall_clock_scan_would_actually_catch_a_violation(source: str) -> None:
    """A canary for the detector itself, not for the code it guards.

    ``time.time()`` is in this list because the first version of this scan missed
    it. The banned-call set was copied from the older sweeps, which never listed
    ``time`` — so ``import time`` followed by ``time.time()`` passed every guard
    the phase added, in the two modules that actually produce the timeline.
    """
    tree = ast.parse(source)
    offenders = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert offenders & WALL_CLOCK_CALLS, f"the scan would not catch: {source!r}"


@pytest.mark.parametrize("module", sorted(EXPECTED_REPLAY_API))
def test_the_replay_public_surface_is_exactly_what_was_reviewed(module: str) -> None:
    assert _public_module_names(module) == EXPECTED_REPLAY_API[module]


@pytest.mark.parametrize("module", sorted(EXPECTED_REPLAY_API))
def test_no_public_replay_name_expresses_a_deliberately_excluded_concept(module: str) -> None:
    offenders = [
        name
        for name in _public_module_names(module)
        for concept in FORBIDDEN_REPLAY_CONCEPTS
        if concept in name.lower()
    ]
    assert offenders == [], f"{module} exposes an out-of-phase concept: {offenders}"


def test_the_replay_namespace_exposes_no_clock_seek_or_selection_entry_point() -> None:
    """The package surface a caller actually reaches for."""
    from quant_research_terminal import replay

    offenders = [
        name
        for name in replay.__all__
        for concept in FORBIDDEN_REPLAY_CONCEPTS
        if concept in name.lower()
    ]
    assert offenders == [], f"an excluded entry point appeared: {offenders}"


def test_the_replay_namespace_exports_exactly_what_it_defines() -> None:
    """``__all__`` cannot drift from the modules it re-exports."""
    from quant_research_terminal import replay

    defined = set().union(*EXPECTED_REPLAY_API.values())
    assert set(replay.__all__) == defined
    assert list(replay.__all__) == sorted(replay.__all__)


def test_the_prepared_replay_offers_no_reset_seek_or_clock_method() -> None:
    from quant_research_terminal.replay import PreparedReplay

    surface = {name for name in dir(PreparedReplay) if not name.startswith("_")}
    assert surface == {"config", "frames"}


def test_the_schema_version_was_not_bumped_for_replay() -> None:
    """Replay consumes canonical artifacts; it required no new persisted field."""
    from quant_research_terminal.data.contracts import (
        SCHEMA_VERSION,
        SUPPORTED_SCHEMA_VERSIONS,
    )

    assert SCHEMA_VERSION == 3
    assert SUPPORTED_SCHEMA_VERSIONS == frozenset({2, 3})


def test_replay_contains_no_record_type_priority_table() -> None:
    """An adversarial check for the shape a fake causal order would take.

    A mapping from record type to a rank — however it is spelled — would be a
    fabricated exchange sequence. The frame key is built from a manifest digest
    and an ordinal precisely so that no such table has anywhere to live.
    """
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        if _module_name(path) not in {*REPLAY_MODULES, DOMAIN_REPLAY}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keyed = [
                key.attr
                for key in node.keys
                if isinstance(key, ast.Attribute)
                and isinstance(key.value, ast.Name)
                and key.value.id == "RecordType"
            ]
            assert not keyed, f"{_module_name(path)} keys a mapping by RecordType at {node.lineno}"
