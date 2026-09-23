"""Case-scoped text revision proposals and working drafts."""

# FastAPI dependency providers are declared in endpoint defaults.
# ruff: noqa: B008

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from law_agent.review.case_store import CaseStore, UserRecord
from law_agent.review.enterprise_store import InMemoryEnterpriseStore, PostgresEnterpriseStore
from law_agent.review.revisions import (
    InMemoryRevisionStore,
    PostgresRevisionStore,
    RevisionConflict,
    RevisionError,
    generate_revision_draft,
    locate_target,
    sha256,
)


class GenerateRevisionRequest(BaseModel):
    material_version_id: str
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)


class DecideRevisionRequest(BaseModel):
    decision: Literal["accepted", "rejected"]
    expected_version: int = Field(ge=1)
    replacement: str | None = None
    note: str | None = None


def register_revision_routes(
    app: FastAPI, *, current_user: Callable[..., Any],
    reviewer_only: Callable[[UserRecord], None], store: Callable[[], CaseStore],
    enterprise: Callable[[], InMemoryEnterpriseStore | PostgresEnterpriseStore],
    revisions: Callable[[], InMemoryRevisionStore | PostgresRevisionStore],
    can_view: Callable[[UserRecord, dict[str, Any]], bool],
) -> None:
    router = APIRouter()

    def visible_case(case_id: str, user: UserRecord) -> dict[str, Any]:
        case = store().get_case(case_id)
        if case is None or not can_view(user, case):
            raise HTTPException(status_code=404, detail="案件不存在或无权访问")
        return case

    def review_result(case: dict[str, Any]) -> dict[str, Any]:
        result = (case.get("response") or {}).get("review_result")
        if not isinstance(result, dict) or not result.get("review_result_id"):
            raise HTTPException(status_code=409, detail="案件尚无可修改的审查结果")
        return result

    def current_text(material_version_id: str, case_id: str) -> tuple[str, int]:
        material = enterprise().get_material_version(material_version_id)
        if material is None or material.case_id != case_id or material.parsed_text is None:
            raise HTTPException(status_code=404, detail="冻结材料不存在")
        accepted = revisions().current_draft(material_version_id)
        return (accepted["result_text"], accepted["result_version"]) if accepted else (material.parsed_text, 0)

    @router.post("/api/cases/{case_id}/issues/{issue_id}/revision-proposals")
    async def generate(
        case_id: str, issue_id: str, payload: GenerateRevisionRequest,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        reviewer_only(user)
        case = visible_case(case_id, user)
        result = review_result(case)
        issue = next((item for item in result.get("issues", []) if item.get("id") == issue_id), None)
        if issue is None:
            raise HTTPException(status_code=404, detail="审查问题不存在")
        if issue.get("kind") == "missing_information":
            raise HTTPException(status_code=422, detail="缺失信息类问题需要补充材料证据，不能通过修改文字解决")
        target = next((item for item in issue.get("material_evidence", [])
                       if item.get("material_version_id") == payload.material_version_id
                       and item.get("start_offset") == payload.start_offset
                       and item.get("end_offset") == payload.end_offset), None)
        if target is None:
            raise HTTPException(status_code=422, detail="目标原文必须来自该问题已核实的材料依据")
        completion = next((event for event in reversed(store().list_events(case_id))
                           if event.get("event_type") == "review_completed"), None)
        completed_task_id = (completion.get("payload") or {}).get("task_id") if completion else None
        task = enterprise().get_task(completed_task_id) if completed_task_id else enterprise().get_latest_task(case_id)
        latest_task = enterprise().get_latest_task(case_id)
        if completed_task_id and latest_task and latest_task.id != completed_task_id and latest_task.status != "failed":
            raise HTTPException(status_code=409, detail="案件正在重新审查，请等待最新结果后再生成修改提案")
        snapshot = enterprise().get_material_snapshot(task.material_snapshot_id) if task else None
        if snapshot is None or payload.material_version_id not in snapshot.version_ids:
            raise HTTPException(status_code=409, detail="目标材料不属于本次审查冻结快照")
        material = enterprise().get_material_version(payload.material_version_id)
        if material is None or material.case_id != case_id or material.parsed_text is None:
            raise HTTPException(status_code=404, detail="冻结材料不存在")
        quote = target["quote"]
        if material.parsed_text[payload.start_offset:payload.end_offset] != quote:
            raise HTTPException(status_code=409, detail="材料依据与冻结原文不一致")
        base_text, base_version = current_text(payload.material_version_id, case_id)
        citation_refs = set(issue.get("supporting_citation_refs") or [])
        citations = [
            {key: citation.get(key) for key in ("citation_ref", "title", "article_no", "full_article_text", "source_url")}
            for citation in result.get("citations", [])
            if isinstance(citation, dict) and citation.get("citation_ref") in citation_refs
        ]
        prior_feedback = [
            proposal["decision_note"] for proposal in revisions().list(case_id)
            if proposal["issue_id"] == issue_id
            and proposal["source_material_version_id"] == material.id
            and proposal["status"] == "rejected"
            and proposal.get("decision_note")
        ][-5:]
        try:
            start, end = locate_target(base_text, quote)
            draft = await run_in_threadpool(
                generate_revision_draft, issue=issue, target_quote=quote,
                base_text=base_text, citations=citations, prior_feedback=prior_feedback,
            )
        except RevisionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RevisionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            proposal = revisions().create({
                "case_id": case_id, "source_review_result_id": result["review_result_id"],
                "issue_id": issue_id, "source_material_version_id": material.id,
                "target_quote": quote, "target_start": start, "target_end": end,
                "base_version": base_version, "base_sha256": sha256(base_text),
                "proposed_text": draft.proposed_text, "rationale": draft.rationale,
                "open_points_json": draft.open_points,
                "citation_refs_json": issue.get("supporting_citation_refs", []),
                "created_by": user.id,
            })
        except RevisionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        store().add_event(case_id, user.id, event_type="revision_proposal_created", payload={"proposal_id": proposal["id"], "issue_id": issue_id})
        return proposal

    @router.get("/api/cases/{case_id}/revision-proposals")
    async def list_proposals(case_id: str, user: UserRecord = Depends(current_user)) -> dict[str, Any]:
        visible_case(case_id, user)
        items = revisions().list(case_id)
        return {"items": items, "total": len(items)}

    @router.post("/api/revision-proposals/{proposal_id}/decision")
    async def decide(
        proposal_id: str, payload: DecideRevisionRequest,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        reviewer_only(user)
        proposal = revisions().get(proposal_id)
        if proposal is None:
            raise HTTPException(status_code=404, detail="修改提案不存在")
        case = visible_case(proposal["case_id"], user)
        result = review_result(case)
        base_text, _ = current_text(proposal["source_material_version_id"], proposal["case_id"])
        try:
            updated = revisions().decide(
                proposal_id, decision=payload.decision,
                expected_version=payload.expected_version,
                current_review_result_id=result["review_result_id"],
                current_base_text=base_text, replacement=payload.replacement,
                note=payload.note, actor_id=user.id,
            )
        except RevisionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RevisionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        store().add_event(proposal["case_id"], user.id, event_type=f"revision_proposal_{payload.decision}", payload={"proposal_id": proposal_id})
        return updated

    @router.get("/api/cases/{case_id}/working-draft")
    async def working_draft(
        case_id: str, material_version_id: str,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        visible_case(case_id, user)
        text, version = current_text(material_version_id, case_id)
        latest = revisions().current_draft(material_version_id)
        return {"source_material_version_id": material_version_id, "version": version,
                "text": text, "sha256": sha256(text),
                "latest_proposal_id": latest["id"] if latest else None}

    app.include_router(router)
