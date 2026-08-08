"""Application layer: use cases that orchestrate the lower layers.

This package owns no business rules. Validation, normalization, ordering,
numeric and temporal semantics, duplicate and conflict policy, and storage
encoding all live in their own layers; a use case here sequences them and
owns only the decisions that exist *between* layers — the transaction
boundary, the one-record-type-per-dataset policy, and read-back verification.
See ADR-007.
"""

from __future__ import annotations

from quant_research_terminal.application.import_dataset import (
    ApplicationError,
    ImportDatasetError,
    ImportDatasetResult,
    ImportDatasetUseCase,
    VerificationError,
)

__all__ = [
    "ApplicationError",
    "ImportDatasetError",
    "ImportDatasetResult",
    "ImportDatasetUseCase",
    "VerificationError",
]
