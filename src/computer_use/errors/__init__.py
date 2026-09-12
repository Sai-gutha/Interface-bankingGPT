"""Typed automation outcomes and runtime failures."""

from computer_use.errors.taxonomy import (
    AUTOMATION_TAXONOMY,
    AutomationCode,
    ErrorCategory,
    EvidenceKind,
    TaxonomyEntry,
    classify,
)

__all__ = [
    "AUTOMATION_TAXONOMY",
    "AutomationCode",
    "ErrorCategory",
    "EvidenceKind",
    "TaxonomyEntry",
    "classify",
]
