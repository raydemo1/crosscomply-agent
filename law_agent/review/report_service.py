"""Create immutable decision-report records from a signed approval."""

from __future__ import annotations

from typing import Any

from law_agent.review.case_store import CaseStore
from law_agent.review.governance_store import (
    InMemoryGovernanceStore,
    PostgresGovernanceStore,
    ReportRecord,
)
from law_agent.review.object_store import MaterialObjectStore
from law_agent.review.report_data import (
    build_ai_review,
    build_legal_sources,
    build_remediation_details,
    manual_confirmations_for_report,
    selected_path_for_report,
)
from law_agent.review.reports import DecisionReportData, generate_decision_report


GovernanceStore = InMemoryGovernanceStore | PostgresGovernanceStore


def ensure_decision_report(
    *,
    case: dict[str, Any],
    case_store: CaseStore,
    enterprise_store: Any,
    governance_store: GovernanceStore,
    object_store: MaterialObjectStore,
) -> ReportRecord:
    """Return the report for the signed decision, creating it once if needed.

    The approval record is the source of truth for both the decision and the
    material/rule task it approved.  This keeps automatic webhook generation
    and the reviewer retry endpoint on exactly the same snapshot.
    """

    case_id = str(case["id"])
    approval = governance_store.get_latest_approval(case_id)
    if approval is None or approval.status == "pending":
        raise ValueError("飞书尚未产生最终审批决定")

    existing = governance_store.get_latest_report(case_id)
    if existing is not None:
        return existing

    approved_task = enterprise_store.get_task(approval.task_id)
    if approved_task is None:
        raise ValueError("审批记录绑定的审查任务不存在")
    snapshot = enterprise_store.get_material_snapshot(approved_task.material_snapshot_id)
    rule = enterprise_store.get_rule_snapshot(approved_task.rule_snapshot_id)
    if snapshot is None or rule is None:
        raise ValueError("审批任务绑定的材料或规则快照不存在")

    versions = [
        enterprise_store.get_material_version(version_id) for version_id in snapshot.version_ids
    ]
    snapshot_actions = approval.payload.get("report_actions_snapshot")
    actions = (
        [dict(item) for item in snapshot_actions if isinstance(item, dict)]
        if isinstance(snapshot_actions, list)
        else case_store.list_actions(case_id)
    )
    determination = dict(rule.determination)
    selected_path = selected_path_for_report(determination)
    report_data = DecisionReportData(
        case_number=case["case_number"],
        decision=approval.status,
        material_hashes=tuple(item.sha256 for item in versions if item is not None),
        rule_version=rule.ruleset_version,
        legal_sources=build_legal_sources(case, determination, selected_path),
        remediation_items=tuple(item["title"] for item in actions),
        approver=approval.approver_name or "未记录审批人",
        approved_at=approval.decided_at or approval.updated_at,
        case_title=case.get("title", ""),
        selected_path=selected_path,
        manual_confirmation_items=manual_confirmations_for_report(determination),
        ai_review=build_ai_review(case),
        remediation_details=build_remediation_details(actions),
    )
    artifact = generate_decision_report(report_data)
    stored = object_store.put_original(
        case_id=case_id,
        logical_name="decision-report",
        filename=f"{case['case_number']}.pdf",
        content_type="application/pdf",
        content=artifact.pdf_bytes,
    )
    return governance_store.create_report_record(
        case_id=case_id,
        approval_id=approval.id,
        object_key=stored.object_key,
        sha256=artifact.sha256,
        metadata={
            "case_number": case["case_number"],
            "rule_version": rule.ruleset_version,
            "material_snapshot_id": snapshot.id,
        },
    )
