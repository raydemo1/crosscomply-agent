"""Reviewer-only field evaluation drafts and quality statistics."""

# ruff: noqa: B008

from datetime import date, datetime, time
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from law_agent.review.dates import BUSINESS_TIMEZONE
from law_agent.review.eval_draft_store import DraftValidationError, DuplicateDraft
from law_agent.review.eval_feedback import (
    candidate_signals,
    draft_inputs,
    source_ids,
    validate_draft,
)

DraftStatus = Literal["draft", "ready", "exported", "discarded"]


class EditDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(default="", max_length=40_000)
    material_text: str = ""
    intake: dict[str, Any] = Field(default_factory=dict)
    reviewer_correction: str = Field(default="", max_length=40_000)
    rubric: dict[str, Any] = Field(default_factory=dict)
    selection_reason: str = Field(default="", max_length=10_000)
    reference_basis: list[str] = Field(default_factory=list)
    deidentified: bool = False


def _public(row):
    return {key: value for key, value in row.items() if key != "original_entities"}


def register_eval_draft_routes(app, *, current_user, reviewer_only):
    router = APIRouter()
    records = app.state.eval_draft_store
    impact = app.state.impact

    def signals_for(case_id):
        return candidate_signals(case_id, cases=impact.cases, governance=impact.governance, impact=impact.records)

    @router.get("/api/cases/{case_id}/eval-signals")
    def get_signals(case_id: str, user=Depends(current_user)):
        reviewer_only(user)
        if not impact.cases.get_case(case_id):
            raise HTTPException(status_code=404, detail="案件不存在")
        return {"items": signals_for(case_id)}

    @router.post("/api/cases/{case_id}/eval-drafts", status_code=201)
    def create(case_id: str, user=Depends(current_user)):
        reviewer_only(user)
        case = impact.cases.get_case(case_id)
        if not case:
            raise HTTPException(status_code=404, detail="案件不存在")
        task = impact.enterprise.get_latest_task(case_id, status="succeeded")
        if not task or not (task.result or {}).get("review_result"):
            raise HTTPException(status_code=409, detail="没有可转为评测用例的已完成审查")
        try:
            inputs = draft_inputs(case, task, enterprise=impact.enterprise, transfers=impact.transfers)
            row = records.create(actor_id=user.id, case_id=case_id, task_id=task.id, signals=signals_for(case_id), **inputs)
        except DuplicateDraft as exc:
            raise HTTPException(status_code=409, detail={"message": str(exc), "draft_id": exc.identifier}) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _public(row)

    @router.get("/api/eval-drafts")
    def drafts(status: DraftStatus | None = None, user=Depends(current_user)):
        reviewer_only(user)
        return {"items": [{key: row[key] for key in ("id", "case_id", "task_id", "status", "updated_at", "exported_case_id")}
                          for row in records.list(status)]}

    @router.get("/api/eval-drafts/{identifier}")
    def detail(identifier: str, user=Depends(current_user)):
        reviewer_only(user)
        row = records.get(identifier)
        if row is None:
            raise HTTPException(status_code=404, detail="评测草稿不存在")
        return _public(row)

    def change(identifier, action, user, **kwargs):
        reviewer_only(user)
        try:
            return _public(records.change(identifier, action, user.id, **kwargs))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="评测草稿不存在") from exc
        except DraftValidationError as exc:
            raise HTTPException(status_code=422, detail={"message": str(exc), "errors": exc.errors}) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.patch("/api/eval-drafts/{identifier}")
    def edit(identifier: str, payload: EditDraft, user=Depends(current_user)):
        return change(identifier, "edit", user, values=payload.model_dump(exclude_unset=True))

    @router.post("/api/eval-drafts/{identifier}/ready")
    def ready(identifier: str, user=Depends(current_user)):
        def validate(row):
            return validate_draft(row, source_ids(app.state.knowledge_corpus))[1]

        return change(identifier, "ready", user, validate=validate)

    @router.post("/api/eval-drafts/{identifier}/reopen")
    def reopen(identifier: str, user=Depends(current_user)):
        return change(identifier, "reopen", user)

    @router.post("/api/eval-drafts/{identifier}/discard")
    def discard(identifier: str, user=Depends(current_user)):
        return change(identifier, "discard", user)

    @router.get("/api/governance/quality")
    def quality(since: date = date(1970, 1, 1), user=Depends(current_user)):
        reviewer_only(user)
        return records.quality(datetime.combine(since, time.min, BUSINESS_TIMEZONE))

    app.include_router(router)
