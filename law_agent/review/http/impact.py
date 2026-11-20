"""Review and disposition of governed regulation changes."""

# ruff: noqa: B008

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from law_agent.review.http.transfers import Note, create_transfer_review
from law_agent.review.impact import REASON_LABELS
from law_agent.review.transactions import case_transaction
from law_agent.review.transfer_store import latest_approved_case

ImpactStatus = Literal["open", "dismissed", "rereview_started", "resolved"]
Severity = Literal["blocking", "upcoming", "informational"]


class DismissImpact(BaseModel):
    note: Note


class RereviewImpact(BaseModel):
    change_note: Note | None = None


def register_impact_routes(app, *, current_user, reviewer_only, admin_only):
    router = APIRouter()
    impact = app.state.impact
    records = impact.records

    @router.get("/api/impact-flags")
    def flags(status: ImpactStatus | None = "open", severity: Severity | None = None, user=Depends(current_user)):
        items = impact.visible_flags(status=status, severity=severity, owner_id=user.id if user.role == "requester" else None)
        if user.role == "requester":
            for item in items:
                activity = impact.transfers.get(item["activity_id"]) if item["activity_id"] else None
                if activity and item["activity_title"] and activity["owner_id"] != user.id:
                    item.update(activity_id=None, activity_title=None)
        return {"items": items}

    @router.get("/api/knowledge/change-events")
    def changes(limit: Annotated[int, Query(ge=1, le=200)] = 50, user=Depends(current_user)):
        reviewer_only(user)
        return {"items": records.changes(limit)}

    @router.post("/api/knowledge/change-events/{identifier}/recompute")
    def recompute(identifier: str, user=Depends(current_user)):
        admin_only(user)
        event = records.get_change(identifier)
        if not event:
            raise HTTPException(status_code=404, detail="找不到法规变更事件")
        if event["impact_status"] == "computed":
            raise HTTPException(status_code=409, detail="已完成影响分析")
        return impact.compute_result(event)

    def flag_for_action(identifier):
        row = records.get_flag(identifier)
        if not row:
            raise HTTPException(status_code=404, detail="未找到影响提示")
        return row

    @router.post("/api/impact-flags/{identifier}/dismiss")
    def dismiss(identifier: str, payload: DismissImpact, user=Depends(current_user)):
        reviewer_only(user)
        flag_for_action(identifier)
        try:
            return records.dispose(identifier, actor_id=user.id, note=payload.note)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/api/impact-flags/{identifier}/rereview", status_code=201)
    def rereview(identifier: str, payload: RereviewImpact, user=Depends(current_user)):
        reviewer_only(user)
        row = flag_for_action(identifier)
        event = records.get_change(row["change_event_id"])
        articles = "、".join(row["article_nos"])
        note = payload.change_note or f"法规变更：《{event['detail']['title']}》{articles} · {REASON_LABELS[row['reason']]}"[:1600]

        def start(flag):
            case_id = flag["case_id"]
            activity_id = impact.transfers.for_case(case_id)
            if not activity_id:
                raise HTTPException(status_code=409, detail={"code": "register_transfer_first", "message": "请先登记为持续出境业务"})
            activity = impact.transfers.get(activity_id)
            if not activity or latest_approved_case(activity, impact.transfers.reviews(activity_id)) != case_id:
                raise ValueError("此案件不是当前审批基线，请查看出境业务")
            case = impact.cases.get_case(case_id)
            task = impact.current_task(case, activity_id)
            if task is None or task.id != flag["task_id"]:
                raise ValueError("案件审查版本已更新，请刷新")
            designation = impact.transfers.review_for_case(case_id) or activity
            return create_transfer_review(
                activity, case_id, business_entity=designation["business_entity"], change_note=note,
                user=user, transfers=impact.transfers, cases=impact.cases,
                enterprise=impact.enterprise, governance=impact.governance,
            )

        try:
            with case_transaction(impact.cases, impact.enterprise, row["case_id"]):
                return records.dispose(identifier, actor_id=user.id, note=note, start=start)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    app.include_router(router)
