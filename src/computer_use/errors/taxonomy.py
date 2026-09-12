"""Single policy table for business outcomes, recovery, escalation, and evidence."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class ErrorCategory(StrEnum):
    BUSINESS_OUTCOME = "business_outcome"
    RECOVERABLE_RUNTIME = "recoverable_runtime"
    HARD_FAILURE = "hard_failure"


class EvidenceKind(StrEnum):
    EVENT = "event"
    OBSERVED_STATE = "observed_state"
    SCREENSHOT = "screenshot"
    EXPECTED_STATE = "expected_state"
    LOCATOR_ATTEMPTS = "locator_attempts"


class AutomationCode(StrEnum):
    # Expected domain results.
    MEMBER_NOT_FOUND = "MEMBER_NOT_FOUND"
    INVALID_INPUT = "INVALID_INPUT"
    ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"

    # Conditions for which a bounded, artifact-declared recovery may run.
    TRANSIENT_TIMEOUT = "TRANSIENT_TIMEOUT"
    KNOWN_DIALOG = "KNOWN_DIALOG"
    TEMPORARY_LOAD_FAILURE = "TEMPORARY_LOAD_FAILURE"

    # Unsafe or non-deterministically recoverable terminal failures.
    TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
    AMBIGUOUS_TARGET = "AMBIGUOUS_TARGET"
    CHECKPOINT_FAILED = "CHECKPOINT_FAILED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    POLICY_VIOLATION = "POLICY_VIOLATION"

    # System-level terminal codes needed by the complete replay contract.
    INVALID_ARTIFACT = "INVALID_ARTIFACT"
    OBSERVATION_FAILED = "OBSERVATION_FAILED"
    PRECONDITION_FAILED = "PRECONDITION_FAILED"
    SUCCESS_CONDITION_FAILED = "SUCCESS_CONDITION_FAILED"
    MISSING_OUTPUTS = "MISSING_OUTPUTS"
    UNSUPPORTED_TARGET = "UNSUPPORTED_TARGET"
    UNEXPECTED_DIALOG = "UNEXPECTED_DIALOG"
    SURFACE_ERROR = "SURFACE_ERROR"
    UNRECOVERABLE_ERROR = "UNRECOVERABLE_ERROR"
    IRREVERSIBLE_ACTION_REQUIRES_APPROVAL = "IRREVERSIBLE_ACTION_REQUIRES_APPROVAL"


class TaxonomyEntry(BaseModel):
    """Machine-readable handling policy for one stable automation code."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    code: AutomationCode
    category: ErrorCategory
    retry_allowed: bool
    legitimate_result: bool
    human_recommended: bool
    evidence: tuple[EvidenceKind, ...]


def _entry(
    code: AutomationCode,
    category: ErrorCategory,
    *,
    retry: bool = False,
    result: bool = False,
    human: bool = False,
    evidence: tuple[EvidenceKind, ...],
) -> TaxonomyEntry:
    return TaxonomyEntry(
        code=code,
        category=category,
        retry_allowed=retry,
        legitimate_result=result,
        human_recommended=human,
        evidence=evidence,
    )


_BUSINESS_EVIDENCE = (EvidenceKind.EVENT, EvidenceKind.OBSERVED_STATE)
_RUNTIME_EVIDENCE = (EvidenceKind.EVENT, EvidenceKind.OBSERVED_STATE)
_FAILURE_EVIDENCE = (
    EvidenceKind.EVENT,
    EvidenceKind.OBSERVED_STATE,
    EvidenceKind.SCREENSHOT,
    EvidenceKind.EXPECTED_STATE,
)

AUTOMATION_TAXONOMY: dict[AutomationCode, TaxonomyEntry] = {
    code: _entry(
        code,
        ErrorCategory.BUSINESS_OUTCOME,
        result=True,
        evidence=_BUSINESS_EVIDENCE,
    )
    for code in (
        AutomationCode.MEMBER_NOT_FOUND,
        AutomationCode.INVALID_INPUT,
        AutomationCode.ACCOUNT_NOT_FOUND,
    )
}
AUTOMATION_TAXONOMY.update(
    {
        code: _entry(
            code,
            ErrorCategory.RECOVERABLE_RUNTIME,
            retry=True,
            evidence=(
                *_RUNTIME_EVIDENCE,
                *((EvidenceKind.SCREENSHOT,) if code == AutomationCode.KNOWN_DIALOG else ()),
            ),
        )
        for code in (
            AutomationCode.TRANSIENT_TIMEOUT,
            AutomationCode.KNOWN_DIALOG,
            AutomationCode.TEMPORARY_LOAD_FAILURE,
        )
    }
)
AUTOMATION_TAXONOMY.update(
    {
        code: _entry(
            code,
            ErrorCategory.HARD_FAILURE,
            human=code
            in {
                AutomationCode.AMBIGUOUS_TARGET,
                AutomationCode.CHECKPOINT_FAILED,
                AutomationCode.SESSION_EXPIRED,
                AutomationCode.PERMISSION_DENIED,
                AutomationCode.IRREVERSIBLE_ACTION_REQUIRES_APPROVAL,
            },
            evidence=(
                *_FAILURE_EVIDENCE,
                *(
                    (EvidenceKind.LOCATOR_ATTEMPTS,)
                    if code
                    in {
                        AutomationCode.TARGET_NOT_FOUND,
                        AutomationCode.AMBIGUOUS_TARGET,
                    }
                    else ()
                ),
            ),
        )
        for code in AutomationCode
        if code not in AUTOMATION_TAXONOMY
    }
)


def classify(code: AutomationCode | str) -> TaxonomyEntry:
    """Return handling policy, rejecting unknown strings instead of guessing semantics."""

    return AUTOMATION_TAXONOMY[AutomationCode(code)]
