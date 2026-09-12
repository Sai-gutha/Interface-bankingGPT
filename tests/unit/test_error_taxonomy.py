"""The handling table is complete and explicit for every public taxonomy code."""

from computer_use.errors import (
    AUTOMATION_TAXONOMY,
    AutomationCode,
    ErrorCategory,
    EvidenceKind,
    classify,
)


def test_every_code_has_exactly_one_taxonomy_entry() -> None:
    assert set(AUTOMATION_TAXONOMY) == set(AutomationCode)
    assert all(entry.code == code for code, entry in AUTOMATION_TAXONOMY.items())


def test_business_outcomes_are_legitimate_and_never_retried() -> None:
    for code in (
        AutomationCode.MEMBER_NOT_FOUND,
        AutomationCode.INVALID_INPUT,
        AutomationCode.ACCOUNT_NOT_FOUND,
    ):
        entry = classify(code)
        assert entry.category == ErrorCategory.BUSINESS_OUTCOME
        assert entry.legitimate_result is True
        assert entry.retry_allowed is False
        assert entry.human_recommended is False
        assert entry.evidence == (EvidenceKind.EVENT, EvidenceKind.OBSERVED_STATE)


def test_recoverable_conditions_allow_bounded_retry_but_are_not_results() -> None:
    for code in (
        AutomationCode.TRANSIENT_TIMEOUT,
        AutomationCode.KNOWN_DIALOG,
        AutomationCode.TEMPORARY_LOAD_FAILURE,
    ):
        entry = classify(code)
        assert entry.category == ErrorCategory.RECOVERABLE_RUNTIME
        assert entry.retry_allowed is True
        assert entry.legitimate_result is False


def test_requested_hard_failures_never_retry() -> None:
    for code in (
        AutomationCode.TARGET_NOT_FOUND,
        AutomationCode.AMBIGUOUS_TARGET,
        AutomationCode.CHECKPOINT_FAILED,
        AutomationCode.SESSION_EXPIRED,
        AutomationCode.PERMISSION_DENIED,
        AutomationCode.POLICY_VIOLATION,
    ):
        entry = classify(code)
        assert entry.category == ErrorCategory.HARD_FAILURE
        assert entry.retry_allowed is False
        assert entry.legitimate_result is False
        assert EvidenceKind.SCREENSHOT in entry.evidence


def test_locator_failures_request_locator_attempt_evidence() -> None:
    for code in (AutomationCode.TARGET_NOT_FOUND, AutomationCode.AMBIGUOUS_TARGET):
        assert EvidenceKind.LOCATOR_ATTEMPTS in classify(code).evidence
