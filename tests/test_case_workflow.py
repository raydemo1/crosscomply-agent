import pytest

from law_agent.review.workflow import (
    CASE_TRANSITIONS,
    ApprovalDecision,
    CaseStatus,
    next_status_after_review,
    validate_case_transition,
)


def test_review_never_finishes_case_without_feishu_approval() -> None:
    assert next_status_after_review(has_missing_information=False) == "pending_feishu_approval"
    assert next_status_after_review(has_missing_information=True) == "needs_info"


def test_only_feishu_can_write_terminal_approval_statuses() -> None:
    for status in ("approved", "conditionally_approved", "rejected"):
        with pytest.raises(ValueError, match="飞书审批事件"):
            validate_case_transition(
                current="pending_feishu_approval",
                target=status,
                authority="local",
            )


@pytest.mark.parametrize(
    ("decision", "status"),
    [
        ("approved", "approved"),
        ("conditionally_approved", "conditionally_approved"),
        ("rejected", "rejected"),
        ("withdrawn", "rejected"),
    ],
)
def test_feishu_decision_maps_to_terminal_status(
    decision: ApprovalDecision,
    status: CaseStatus,
) -> None:
    assert (
        validate_case_transition(
            current="pending_feishu_approval",
            target=status,
            authority="feishu",
            approval_decision=decision,
        )
        == status
    )


def test_terminal_approval_status_is_irreversible() -> None:
    assert CASE_TRANSITIONS["approved"] == frozenset()
    with pytest.raises(ValueError, match="不能将案件"):
        validate_case_transition(
            current="approved",
            target="review_running",
            authority="local",
        )


def test_run_failure_can_be_retried_or_sent_back_for_information() -> None:
    assert CASE_TRANSITIONS["run_failed"] == frozenset({"review_running", "needs_info"})
