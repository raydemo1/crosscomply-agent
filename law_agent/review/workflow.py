"""Deterministic case lifecycle for the enterprise approval workflow."""

from __future__ import annotations

from typing import Literal

CaseStatus = Literal[
    "draft",
    "needs_info",
    "pending_review",
    "review_running",
    "pending_feishu_approval",
    "approved",
    "conditionally_approved",
    "rejected",
    "run_failed",
]
TransitionAuthority = Literal["local", "feishu"]
ApprovalDecision = Literal[
    "approved",
    "conditionally_approved",
    "rejected",
    "withdrawn",
]

CASE_TRANSITIONS: dict[CaseStatus, frozenset[CaseStatus]] = {
    "draft": frozenset({"needs_info", "pending_review"}),
    "needs_info": frozenset({"draft", "pending_review"}),
    "pending_review": frozenset({"needs_info", "review_running"}),
    "review_running": frozenset({"needs_info", "pending_feishu_approval", "run_failed"}),
    "pending_feishu_approval": frozenset({"approved", "conditionally_approved", "rejected"}),
    "approved": frozenset(),
    "conditionally_approved": frozenset(),
    "rejected": frozenset(),
    "run_failed": frozenset({"review_running", "needs_info"}),
}

_FEISHU_TERMINAL_STATUS: dict[ApprovalDecision, CaseStatus] = {
    "approved": "approved",
    "conditionally_approved": "conditionally_approved",
    "rejected": "rejected",
    "withdrawn": "rejected",
}


def next_status_after_review(*, has_missing_information: bool) -> CaseStatus:
    """Route an AI review to human approval or back to information collection."""

    return "needs_info" if has_missing_information else "pending_feishu_approval"


def validate_case_transition(
    *,
    current: CaseStatus,
    target: CaseStatus,
    authority: TransitionAuthority,
    approval_decision: ApprovalDecision | None = None,
) -> CaseStatus:
    """Validate lifecycle authority and return the accepted target status."""

    if target == current:
        return target
    if target not in CASE_TRANSITIONS[current]:
        raise ValueError(f"不能将案件从 {current} 变更为 {target}")
    if target in {"approved", "conditionally_approved", "rejected"}:
        if authority != "feishu" or approval_decision is None:
            raise ValueError("审批终态只能由飞书审批事件写入")
        expected = _FEISHU_TERMINAL_STATUS[approval_decision]
        if target != expected:
            raise ValueError(f"飞书审批决定 {approval_decision} 不能映射为案件状态 {target}")
    return target
