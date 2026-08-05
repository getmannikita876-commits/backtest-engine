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

import quant_research_terminal

PACKAGE_ROOT = Path(quant_research_terminal.__file__).parent
ROOT_MODULE = "quant_research_terminal"

STORAGE = f"{ROOT_MODULE}.data"
DOMAIN = f"{ROOT_MODULE}.domain"
UI = f"{ROOT_MODULE}.ui"
DATA_IMPORT = f"{ROOT_MODULE}.data_import"
PROVIDERS = f"{DATA_IMPORT}.providers"


def _module_name(path: Path) -> str:
    relative = path.relative_to(PACKAGE_ROOT).with_suffix("")
    parts = [part for part in relative.parts if part != "__init__"]
    return ".".join([ROOT_MODULE, *parts])


def _imports(path: Path) -> set[str]:
    """Return every module name imported by the file at ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)

    return found


def _modules_under(prefix: str) -> list[tuple[str, set[str]]]:
    """Return ``(module_name, imported_names)`` for every module under ``prefix``."""
    return [
        (name, _imports(path))
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
    # time_semantics and numeric_semantics are the authoritative leaf rules;
    # they must stay free of every stage that consumes them.
    for module in ("time_semantics", "numeric_semantics"):
        _, imported = _modules_under(f"{DATA_IMPORT}.{module}")[0]
        internal = {name for name in imported if name.startswith(f"{DATA_IMPORT}.")}
        assert internal == set()
