"""Shared preflight for the case detail and Feishu approval submission."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from law_agent.review.enterprise_store import ReviewTask
from law_agent.review.workflow import (
    review_has_missing_information,
    review_needs_source_verification,
)

ApprovalReadinessCode = Literal[
    "ready", "submitted", "decided", "review_pending", "review_failed", "source_review",
    "fact_review", "evidence_review", "actions_open", "stale_review", "transfer_impact",
]


@dataclass(frozen=True)
class ApprovalReadiness:
    code: ApprovalReadinessCode
    detail: str = ""
    action_ids: list[str] = field(default_factory=list)


def assess_approval_readiness(
    *, case: dict[str, Any], task: ReviewTask | None, plan: dict[str, Any] | None,
    material_snapshot_id: str | None, intake_snapshot_id: str | None,
    approval_exists: bool, transfer_impact_required: bool,
) -> ApprovalReadiness:
    status = case["status"]
    if status in {"approved", "conditionally_approved", "rejected"}:
        return ApprovalReadiness("decided")
    if status == "run_failed":
        return ApprovalReadiness("review_failed", "审查运行失败，请查看流程记录。")
    payloads = [case.get("response") or {}, (task.result or {}) if task else {}]
    if status == "pending_source_verification" or any(map(review_needs_source_verification, payloads)):
        detail = next((gap for payload in payloads
                       for gap in ((payload.get("review_result") or {}).get("legal_basis_gaps") or [])), "")
        return ApprovalReadiness("source_review", detail or "法律依据仍待核验。")
    if task and task.status == "succeeded" and (
        (material_snapshot_id is not None and task.material_snapshot_id != material_snapshot_id)
        or (intake_snapshot_id is not None and task.intake_snapshot_id != intake_snapshot_id)
    ):
        return ApprovalReadiness("stale_review", "最新送审版本尚未完成审查。")
    questions = [
        question for payload in payloads
        for question in ((payload.get("review_result") or {}).get("missing_information") or [])
        + ((payload.get("semantic_grounding") or {}).get("missing_facts") or [])
    ]
    if questions or (task and task.status == "waiting_input"):
        pending_question = (task.agent_state or {}).get("pending_question") if task else None
        return ApprovalReadiness("fact_review", questions[0] if questions else pending_question or "有业务事实等待申报人补充。")
    if any(map(review_has_missing_information, payloads)):
        return ApprovalReadiness("evidence_review", "审查仍有事实或证据缺口。")
    blockers = [
        item for item in (plan or {}).get("tasks") or []
        if item.get("is_current", True)
        and item.get("phase") == "pre_approval"
        and item.get("blocking")
        and item.get("status") != "completed"
    ]
    if blockers and status in {"needs_info", "pending_feishu_approval"}:
        return ApprovalReadiness("actions_open", blockers[0]["title"], [item["id"] for item in blockers])
    if status != "pending_feishu_approval":
        return ApprovalReadiness("review_pending", "案件尚未完成审查。")
    if task is None or task.status != "succeeded":
        return ApprovalReadiness("review_pending", "案件没有已完成的审查任务。")
    if approval_exists:
        return ApprovalReadiness("submitted")
    if transfer_impact_required:
        return ApprovalReadiness("transfer_impact", "请先在关联业务中确认本次变更的影响。")
    return ApprovalReadiness("ready")
