"""Case annotations anchored to an exact frozen material span."""

# FastAPI dependency providers are declared in endpoint defaults.
# ruff: noqa: B008

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from law_agent.review.annotations import (
    InMemoryAnnotationStore,
    PostgresAnnotationStore,
    analyze_selection,
)
from law_agent.review.case_store import CaseStore, UserRecord
from law_agent.review.enterprise_store import InMemoryEnterpriseStore, PostgresEnterpriseStore


class AnnotationTarget(BaseModel):
    material_version_id: str
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)


class HumanAnnotationRequest(AnnotationTarget):
    finding: str = Field(min_length=1, max_length=4000)


class FollowupRequest(AnnotationTarget):
    question: str = Field(min_length=1, max_length=1000)


class AnnotationDecision(BaseModel):
    decision: Literal["confirmed", "rejected"]
    expected_version: int = Field(ge=1)


def register_annotation_routes(
    app: FastAPI, *, current_user: Callable[..., Any],
    reviewer_only: Callable[[UserRecord], None], store: Callable[[], CaseStore],
    enterprise: Callable[[], InMemoryEnterpriseStore | PostgresEnterpriseStore],
    annotations: Callable[[], InMemoryAnnotationStore | PostgresAnnotationStore],
    can_view: Callable[[UserRecord, dict[str, Any]], bool],
) -> None:
    router = APIRouter()

    def visible_case(case_id: str, user: UserRecord) -> dict[str, Any]:
        case = store().get_case(case_id)
        if case is None or not can_view(user, case):
            raise HTTPException(status_code=404, detail="案件不存在或无权访问")
        return case

    def review_context(case: dict[str, Any], target: AnnotationTarget) -> tuple[str, str, dict[str, Any]]:
        result = (case.get("response") or {}).get("review_result")
        if not isinstance(result, dict) or not result.get("review_result_id"):
            raise HTTPException(status_code=409, detail="案件尚无可批注的审查结果")
        completion = next((event for event in reversed(store().list_events(case["id"]))
                           if event.get("event_type") == "review_completed"), None)
        task_id = (completion.get("payload") or {}).get("task_id") if completion else None
        task = enterprise().get_task(task_id) if task_id else enterprise().get_latest_task(case["id"])
        snapshot = enterprise().get_material_snapshot(task.material_snapshot_id) if task else None
        if snapshot is None or target.material_version_id not in snapshot.version_ids:
            raise HTTPException(status_code=409, detail="所选材料不属于本次审查的冻结快照")
        material = enterprise().get_material_version(target.material_version_id)
        if material is None or material.case_id != case["id"] or material.parsed_text is None:
            raise HTTPException(status_code=404, detail="冻结材料不存在")
        if target.end_offset > len(material.parsed_text) or target.end_offset - target.start_offset > 2000:
            raise HTTPException(status_code=422, detail="请选择不超过 2000 字的原文")
        quote = material.parsed_text[target.start_offset:target.end_offset]
        if not quote.strip():
            raise HTTPException(status_code=422, detail="请选择有内容的原文")
        return quote, material.parsed_text, result

    @router.get("/api/cases/{case_id}/review-materials")
    async def review_materials(case_id: str, user: UserRecord = Depends(current_user)) -> dict[str, Any]:
        case = visible_case(case_id, user)
        result = (case.get("response") or {}).get("review_result")
        if not isinstance(result, dict) or not result.get("review_result_id"):
            return {"items": []}
        completion = next((event for event in reversed(store().list_events(case_id))
                           if event.get("event_type") == "review_completed"), None)
        task_id = (completion.get("payload") or {}).get("task_id") if completion else None
        task = enterprise().get_task(task_id) if task_id else enterprise().get_latest_task(case_id)
        snapshot = enterprise().get_material_snapshot(task.material_snapshot_id) if task else None
        items = []
        if snapshot:
            for version_id in snapshot.version_ids:
                material = enterprise().get_material_version(version_id)
                if material and material.case_id == case_id and material.parsed_text is not None:
                    items.append({"id": material.id, "logical_name": material.logical_name,
                                  "filename": material.filename, "version_number": material.version_number,
                                  "parsed_text": material.parsed_text})
        return {"items": items}

    @router.get("/api/cases/{case_id}/annotations")
    async def list_annotations(case_id: str, user: UserRecord = Depends(current_user)) -> dict[str, Any]:
        case = visible_case(case_id, user)
        result_id = ((case.get("response") or {}).get("review_result") or {}).get("review_result_id")
        items = [item for item in annotations().list(case_id) if item["review_result_id"] == result_id]
        if user.role not in {"reviewer", "admin"}:
            items = [item for item in items if item["status"] == "confirmed"]
        return {"items": items}

    @router.post("/api/cases/{case_id}/annotations")
    async def create_human(case_id: str, payload: HumanAnnotationRequest,
                           user: UserRecord = Depends(current_user)) -> dict[str, Any]:
        reviewer_only(user)
        case = visible_case(case_id, user)
        quote, _text, result = review_context(case, payload)
        if not payload.finding.strip():
            raise HTTPException(status_code=422, detail="批注内容不能为空")
        item = annotations().create({
            "case_id": case_id, "review_result_id": result["review_result_id"],
            "material_version_id": payload.material_version_id,
            "start_offset": payload.start_offset, "end_offset": payload.end_offset,
            "quote": quote, "source": "human", "question": "",
            "finding": payload.finding.strip(), "recommendation": "", "citation_refs": [],
            "insufficient_evidence": False, "created_by": user.id,
        })
        store().add_event(case_id, user.id, event_type="annotation_created", payload={"annotation_id": item["id"]})
        return item

    @router.post("/api/cases/{case_id}/annotation-followups")
    async def followup(case_id: str, payload: FollowupRequest,
                       user: UserRecord = Depends(current_user)) -> dict[str, Any]:
        reviewer_only(user)
        case = visible_case(case_id, user)
        quote, text, result = review_context(case, payload)
        if not payload.question.strip():
            raise HTTPException(status_code=422, detail="追审问题不能为空")
        groups = (case.get("response") or {}).get("citation_groups") or []
        citations = [citation for group in groups for citation in group.get("citations", [])
                     if citation.get("can_cite_clause") and citation.get("citation_ref")
                     and (citation.get("full_article_text") or "").strip()]
        citations = [{"citation_ref": item["citation_ref"], "title": item.get("title"),
                      "article_no": item.get("article_no"),
                      "full_article_text": (item.get("full_article_text") or "")[:4000]}
                     for item in citations[:12]]
        context = text[max(0, payload.start_offset - 2500):min(len(text), payload.end_offset + 2500)]
        try:
            analysis = await run_in_threadpool(
                analyze_selection, quote=quote, context=context,
                question=payload.question, citations=citations,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        current = visible_case(case_id, user)
        if (current.get("response") or {}).get("review_result", {}).get("review_result_id") != result["review_result_id"]:
            raise HTTPException(status_code=409, detail="审查结果已更新，请重新发起追审")
        item = annotations().create({
            "case_id": case_id, "review_result_id": result["review_result_id"],
            "material_version_id": payload.material_version_id,
            "start_offset": payload.start_offset, "end_offset": payload.end_offset,
            "quote": quote, "source": "model", "question": payload.question.strip(),
            "finding": analysis.finding, "recommendation": analysis.recommendation,
            "citation_refs": analysis.citation_refs,
            "insufficient_evidence": analysis.insufficient_evidence, "created_by": user.id,
        })
        store().add_event(case_id, user.id, event_type="annotation_followup_created", payload={"annotation_id": item["id"]})
        return item

    @router.post("/api/annotations/{annotation_id}/decision")
    async def decide(annotation_id: str, payload: AnnotationDecision,
                     user: UserRecord = Depends(current_user)) -> dict[str, Any]:
        reviewer_only(user)
        item = annotations().get(annotation_id)
        if item is None or item["source"] != "model":
            raise HTTPException(status_code=404, detail="模型批注不存在")
        case = visible_case(item["case_id"], user)
        result_id = ((case.get("response") or {}).get("review_result") or {}).get("review_result_id")
        if item["review_result_id"] != result_id:
            raise HTTPException(status_code=409, detail="案件已重新审查，旧追审批注不能再确认")
        try:
            updated = annotations().decide(annotation_id, payload.decision,
                                           payload.expected_version, user.id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        store().add_event(item["case_id"], user.id, event_type=f"annotation_{payload.decision}",
                          payload={"annotation_id": annotation_id})
        return updated

    app.include_router(router)
