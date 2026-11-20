"""Transfer register and legal review of changes against an approved case."""

# ruff: noqa: B008

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field, StringConstraints

from law_agent.review.case_store import UserRecord, utc_now
from law_agent.review.http.schemas import IntakePayload
from law_agent.review.portfolio import approved_context
from law_agent.review.transfer_store import APPROVED, CLOSED, latest_approved_case

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
Note = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1600)]

SCOPE_FIELDS = {
    "business_activity": "业务活动",
    "overseas_recipient": "境外接收方",
    "vendor_name": "供应商",
    "destination_region": "接收地",
    "processing_purpose": "处理目的",
    "cross_border_transfer": "涉及出境",
    "data_types": "数据类别",
    "contains_personal_information": "涉及个人信息",
    "sensitive_personal_info": "涉及敏感个人信息",
    "important_data_status": "重要数据情况",
    "ciio_status": "关键信息基础设施运营者身份",
    "annual_non_sensitive_count": "非敏感个人信息人数",
    "annual_sensitive_count": "敏感个人信息人数",
    "count_period": "统计期间",
    "transfer_mechanism": "申报的出境机制",
    "contract_status": "合同状态",
}


class RegisterTransfer(BaseModel):
    case_id: str
    title: ShortText
    business_entity: ShortText
    next_review_on: date
    counts_toward_total: bool = True
    exclusion_note: str = Field(default="", max_length=1600)


class RescheduleTransfer(BaseModel):
    next_review_on: date


class StartReview(BaseModel):
    baseline_case_id: str
    business_entity: ShortText
    change_note: Note


class ConfirmImpact(BaseModel):
    case_id: str
    task_id: str
    outcome: Literal["scope_changed", "scope_unchanged"]
    note: Note


def scope_diff(
    before: dict[str, Any], after: dict[str, Any], old_entity: str, new_entity: str,
) -> list[dict[str, Any]]:
    changes = []
    if old_entity != new_entity:
        changes.append({"field": "业务主体", "before": old_entity, "after": new_entity})
    old, new = (IntakePayload.model_validate(value).model_dump() for value in (before, after))
    for key, label in SCOPE_FIELDS.items():
        left, right = old[key], new[key]
        if isinstance(left, list) and isinstance(right, list) and set(left) == set(right):
            continue
        if left != right:
            changes.append({"field": label, "before": left, "after": right})
    return changes


def create_transfer_review(row, baseline_id, *, business_entity, change_note, user, transfers, cases, enterprise, governance):
    _, _, snapshot = approved_context(baseline_id, cases=cases, enterprise=enterprise, governance=governance)
    if cases.get_user(row["owner_id"]) is None:
        raise ValueError("业务负责人账号不可用，请联系管理员")
    prepared = IntakePayload.model_validate(snapshot.intake).model_dump(mode="json")
    prepared.update(followup_answers={}, notes="", business_entity=business_entity)
    return transfers.start_review(
        row, baseline_id, business_entity=business_entity, note=change_note, actor_id=user.id,
        case_values={
            "title": f"{row['title']} · 变更复审",
            "question": f"请审查「{row['title']}」的本次变化及其对出境合规的影响。\n"
                        f"业务主体：{business_entity}\n复审原因：{change_note}",
            "intake": prepared, "material_text": "", "created_by": row["owner_id"], "owner_id": row["owner_id"],
        },
    )


def register_transfer_routes(
    app: FastAPI, *, current_user: Any, cases: Any, enterprise: Any, governance: Any,
    transfers: Any, can_view_case: Any, reviewer_only: Any, impacts: Any,
) -> None:
    router = APIRouter(prefix="/api/transfer-activities")

    def visible_activity(identifier: str, user: UserRecord) -> dict[str, Any]:
        row = transfers.get(identifier)
        if row is None or (user.role == "requester" and row["owner_id"] != user.id):
            raise HTTPException(status_code=404, detail="出境业务不存在或无权访问")
        return row

    def baseline_context(case_id: str) -> tuple[dict[str, Any], Any, dict[str, Any]]:
        try:
            case, approval, snapshot = approved_context(case_id, cases=cases(), enterprise=enterprise(), governance=governance())
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return case, approval, snapshot.intake

    def activity_payload(row: dict[str, Any], reviews: list[dict[str, Any]]) -> dict[str, Any]:
        current_id = latest_approved_case(row, reviews)
        case, approval, intake = baseline_context(current_id)
        current = next((review for review in reviews if review["case_id"] == current_id), None)
        owner = cases().get_user(row["owner_id"])
        active = next((review for review in reviews if review["status"] not in CLOSED), None)
        has_impact = bool(impacts.flags(status="open", case_id=current_id, task_id=approval.task_id))
        if not has_impact and active and active["status"] == "pending_feishu_approval":
            task = enterprise().get_latest_task(active["case_id"])
            has_impact = bool(task and impacts.flags(status="open", case_id=active["case_id"], task_id=task.id))
        return {
            "id": row["id"], "title": row["title"],
            "owner_name": owner.display_name if owner else "负责人不可用",
            "business_entity": current["business_entity"] if current else row["business_entity"],
            "recipient": intake.get("overseas_recipient") or intake.get("vendor_name") or "",
            "destination": intake.get("destination_region") or "",
            "data_types": intake.get("data_types") or [],
            "next_review_on": row["next_review_on"],
            "approval": {"case_id": current_id, "title": case["title"], "status": approval.status,
                         "decided_at": approval.decided_at},
            "active_status": active["status"] if active else None,
            "regulation_impact_open": has_impact,
        }

    @router.get("")
    def list_activities(user: UserRecord = Depends(current_user)) -> dict[str, Any]:
        items = [activity_payload(row, transfers.reviews(row["id"])) for row in transfers.list(user)]
        return {"items": items}

    @router.get("/eligible-cases")
    def eligible_cases(user: UserRecord = Depends(current_user)) -> dict[str, Any]:
        items = []
        for case in cases().list_cases(user, statuses=APPROVED):
            owner_id = case.get("owner_id") or case["created_by"]
            if (user.role == "requester" and owner_id != user.id) or transfers.for_case(case["id"]):
                continue
            try:
                _, approval, _ = baseline_context(case["id"])
            except HTTPException as exc:
                if exc.status_code == 409:
                    continue
                raise
            items.append({"id": case["id"], "title": case["title"], "decided_at": approval.decided_at})
        return {"items": items}

    @router.post("", status_code=201)
    def register(payload: RegisterTransfer, user: UserRecord = Depends(current_user)) -> dict[str, str]:
        case = cases().get_case(payload.case_id)
        if case is None or not can_view_case(user, case):
            raise HTTPException(status_code=404, detail="案件不存在或无权访问")
        owner_id = case.get("owner_id") or case["created_by"]
        if user.role == "requester" and user.id != owner_id:
            raise HTTPException(status_code=403, detail="请由当前业务负责人登记")
        if not payload.counts_toward_total:
            reviewer_only(user)
            if not payload.exclusion_note.strip():
                raise HTTPException(status_code=422, detail="请填写不计入主体累计的原因")
        baseline_context(payload.case_id)
        if transfers.for_case(payload.case_id):
            raise HTTPException(status_code=409, detail="这个案件已经登记过")
        try:
            identifier = transfers.register(
                origin_case_id=payload.case_id, owner_id=owner_id, title=payload.title,
                business_entity=payload.business_entity, next_review_on=payload.next_review_on,
                counts_toward_total=payload.counts_toward_total,
                exclusion_note="" if payload.counts_toward_total else payload.exclusion_note.strip(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"id": identifier}

    @router.get("/{identifier}")
    def get_activity(identifier: str, user: UserRecord = Depends(current_user)) -> dict[str, Any]:
        row = visible_activity(identifier, user)
        reviews = transfers.reviews(identifier)
        result = activity_payload(row, reviews)
        entities = {review["case_id"]: review["business_entity"] for review in reviews}
        entities[row["origin_case_id"]] = row["business_entity"]
        history = []
        for review in reviews:
            case = cases().get_case(review["case_id"])
            if case is None or not can_view_case(user, case):
                continue
            task = enterprise().get_latest_task(case["id"])
            approval = governance().get_latest_approval(case["id"])
            if approval and approval.status != "pending":
                task = enterprise().get_task(approval.task_id)
            snapshot = enterprise().get_intake_snapshot(task.intake_snapshot_id) if task else None
            impact = review["impact"]
            if task is None or (impact or {}).get("task_id") != task.id:
                impact = None
            reviewer = cases().get_user(impact["reviewer_id"]) if impact else None
            _, _, baseline = baseline_context(review["baseline_case_id"])
            history.append({
                **review, "impact": {**impact, "reviewer_name": reviewer.display_name if reviewer else None} if impact else None,
                "changes": scope_diff(baseline, snapshot.intake if snapshot else case["intake"],
                                      entities[review["baseline_case_id"]], review["business_entity"]),
                "task_id": task.id if task else None,
                "can_confirm": bool(user.role != "requester" and not approval and not impact
                                    and case["status"] == "pending_feishu_approval" and task and task.status == "succeeded"),
                "can_edit_draft": case["status"] == "draft" and user.id == (case.get("owner_id") or case["created_by"]),
            })
        return {**result, "origin_case_id": row["origin_case_id"], "reviews": history}

    @router.patch("/{identifier}")
    def reschedule(
        identifier: str, payload: RescheduleTransfer, user: UserRecord = Depends(current_user),
    ) -> dict[str, date]:
        visible_activity(identifier, user)
        transfers.reschedule(identifier, payload.next_review_on)
        return {"next_review_on": payload.next_review_on}

    @router.post("/{identifier}/reviews", status_code=201)
    def start_review(
        identifier: str, payload: StartReview, user: UserRecord = Depends(current_user),
    ) -> dict[str, str]:
        row = visible_activity(identifier, user)
        baseline_id = latest_approved_case(row, transfers.reviews(identifier))
        if baseline_id != payload.baseline_case_id:
            raise HTTPException(status_code=409, detail="最近一次审批已更新，请刷新后发起复审")
        try:
            case_id = create_transfer_review(
                row, baseline_id, business_entity=payload.business_entity, change_note=payload.change_note,
                user=user, transfers=transfers, cases=cases(), enterprise=enterprise(), governance=governance(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"case_id": case_id, "owner_id": row["owner_id"]}

    @router.post("/{identifier}/impact")
    def confirm_impact(
        identifier: str, payload: ConfirmImpact, user: UserRecord = Depends(current_user),
    ) -> dict[str, bool]:
        reviewer_only(user)
        visible_activity(identifier, user)
        review = transfers.review_for_case(payload.case_id)
        if review is None or review["activity_id"] != identifier:
            raise HTTPException(status_code=404, detail="此活动下找不到该复审案件")
        case = cases().get_case(payload.case_id)
        task = enterprise().get_latest_task(payload.case_id)
        if (case is None or case["status"] != "pending_feishu_approval" or task is None
                or task.status != "succeeded" or task.id != payload.task_id
                or governance().get_latest_approval(payload.case_id) is not None):
            raise HTTPException(status_code=409, detail="案件版本或审批状态已变化，请刷新后重试")
        try:
            transfers.record_impact(payload.case_id, {
                "task_id": task.id, "outcome": payload.outcome, "note": payload.note,
                "reviewer_id": user.id, "confirmed_at": utc_now(),
            })
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"confirmed": True}

    app.include_router(router)
