"""Remediation-plan and remediation-evidence HTTP adapter."""

# FastAPI intentionally declares dependency providers in endpoint defaults.
# ruff: noqa: B008

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import Response

from law_agent.review.case_store import CaseStore, UserRecord
from law_agent.review.http.cases import ALLOWED_UPLOAD_SUFFIXES, MAX_UPLOAD_BYTES
from law_agent.review.http.schemas import (
    RemediationPlanRequest,
    RemediationSubmissionRequest,
    RemediationSubmissionReviewRequest,
    RemediationTaskUpdateRequest,
)
from law_agent.review.object_store import MaterialObjectStore


def register_remediation_routes(
    app: FastAPI,
    *,
    current_user: Callable[..., Any],
    reviewer_only: Callable[[UserRecord], None],
    store: Callable[[], CaseStore],
    originals: Callable[[], MaterialObjectStore],
    case_summary: Callable[[dict[str, Any]], dict[str, Any]],
    can_view: Callable[[UserRecord, dict[str, Any]], bool],
) -> None:
    router = APIRouter()

    def plan_viewable(user: UserRecord, plan: dict[str, Any]) -> bool:
        case = store().get_case(plan["case_id"])
        return case is not None and can_view(user, case)

    def task_viewable(user: UserRecord, task: dict[str, Any]) -> bool:
        if user.role in {"reviewer", "admin"} or task.get("assignee_id") == user.id:
            return True
        case = store().get_case(task["case_id"])
        return case is not None and case.get("created_by") == user.id

    def task_payload(task: dict[str, Any]) -> dict[str, Any]:
        payload = dict(task)
        assignee = store().get_user(task.get("assignee_id")) if task.get("assignee_id") else None
        payload["assignee"] = assignee.to_dict() if assignee else None
        if "submissions" in task:
            submissions: list[dict[str, Any]] = []
            for submission in task.get("submissions") or []:
                item = dict(submission)
                submitter = (
                    store().get_user(item.get("submitted_by"))
                    if item.get("submitted_by")
                    else None
                )
                reviewer = (
                    store().get_user(item.get("reviewed_by"))
                    if item.get("reviewed_by")
                    else None
                )
                item["submitted_by_user"] = submitter.to_dict() if submitter else None
                item["reviewed_by_user"] = reviewer.to_dict() if reviewer else None
                submissions.append(item)
            payload["submissions"] = submissions
            payload["latest_submission"] = submissions[-1] if submissions else None
        case = store().get_case(task["case_id"])
        if case:
            payload["case_title"] = case.get("title") or case.get("question")
            payload["case_question"] = case.get("question")
        return payload

    def plan_payload(plan: dict[str, Any]) -> dict[str, Any]:
        payload = dict(plan)
        case = store().get_case(plan["case_id"])
        if case:
            payload["case_title"] = case.get("title") or case.get("question")
            payload["case_question"] = case.get("question")
        tasks = [task_payload(item) for item in (plan.get("tasks") or [])]
        payload["tasks"] = tasks
        payload["counts"] = {
            "total": len(tasks),
            "open": sum(item.get("status") == "open" for item in tasks),
            "in_progress": sum(item.get("status") == "in_progress" for item in tasks),
            "pending_review": sum(item.get("status") == "pending_review" for item in tasks),
            "completed": sum(item.get("status") == "completed" for item in tasks),
            "overdue": sum(
                bool(item.get("due_date"))
                and item.get("status") != "completed"
                and item["due_date"] < datetime.now(UTC).date().isoformat()
                for item in tasks
            ),
        }
        return payload

    def record_event(
        plan_id: str,
        actor_id: str,
        *,
        event_type: str,
        task_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        store().add_remediation_event(
            plan_id,
            actor_id,
            event_type=event_type,
            task_id=task_id,
            payload=payload or {},
        )

    @router.post("/api/cases/{identifier}/remediation-plan")
    async def create_remediation_plan(
        identifier: str,
        payload: RemediationPlanRequest,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        reviewer_only(user)
        case = store().get_case(identifier)
        if case is None:
            raise HTTPException(status_code=404, detail="案件不存在")
        for task in payload.tasks:
            if task.assignee_id is not None and store().get_user(task.assignee_id) is None:
                raise HTTPException(status_code=422, detail="整改负责人不存在或已停用")
        try:
            plan = store().create_remediation_plan(
                identifier,
                user.id,
                tasks=[item.model_dump(mode="json") for item in payload.tasks],
                no_remediation_reason=payload.no_remediation_reason,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        store().add_event(
            identifier,
            user.id,
            event_type="remediation_plan_created",
            payload={"plan_id": plan["id"]},
        )
        record_event(plan["id"], user.id, event_type="plan_created")
        return plan_payload(plan)

    @router.get("/api/cases/{identifier}/remediation-plan")
    async def get_remediation_plan(
        identifier: str,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        plan = store().get_remediation_plan(identifier)
        if plan is None or not plan_viewable(user, plan):
            raise HTTPException(status_code=404, detail="整改计划不存在或无权访问")
        return plan_payload(plan)

    @router.post("/api/remediation-plans/{plan_id}/activate")
    async def activate_remediation_plan(
        plan_id: str,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        reviewer_only(user)
        try:
            plan = store().activate_remediation_plan(plan_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="整改计划不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        store().add_event(
            plan["case_id"],
            user.id,
            event_type="remediation_plan_activated",
            payload={"plan_id": plan_id},
        )
        record_event(plan_id, user.id, event_type="plan_activated")
        return plan_payload(plan)

    @router.post("/api/remediation-plans/{plan_id}/cancel")
    async def cancel_remediation_plan(
        plan_id: str,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        reviewer_only(user)
        try:
            plan = store().cancel_remediation_plan(plan_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="整改计划不存在") from exc
        store().add_event(
            plan["case_id"],
            user.id,
            event_type="remediation_plan_cancelled",
            payload={"plan_id": plan_id},
        )
        record_event(plan_id, user.id, event_type="plan_cancelled")
        return plan_payload(plan)

    @router.get("/api/remediations")
    async def list_remediations(
        scope: Literal["mine", "review"] = "mine",
        status: str | None = None,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        if scope == "review":
            reviewer_only(user)
            items = store().list_remediation_tasks(status=status)
        else:
            items = store().list_remediation_tasks(assignee_id=user.id, status=status)
        return {"items": [task_payload(item) for item in items], "total": len(items)}

    @router.get("/api/users/assignable")
    async def list_assignable_users(user: UserRecord = Depends(current_user)) -> dict[str, Any]:
        reviewer_only(user)
        items = [item.to_dict() for item in store().list_assignable_users()]
        return {"items": items, "total": len(items)}

    @router.get("/api/remediation-tasks/{task_id}")
    async def get_remediation_task(
        task_id: str,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        task = store().get_remediation_task(task_id)
        if task is None or not task_viewable(user, task):
            raise HTTPException(status_code=404, detail="整改任务不存在或无权访问")
        case = store().get_case(task["case_id"])
        return {"task": task_payload(task), "case": case_summary(case) if case else None}

    @router.patch("/api/remediation-tasks/{task_id}")
    async def update_remediation_task(
        task_id: str,
        payload: RemediationTaskUpdateRequest,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        reviewer_only(user)
        changes = payload.model_dump(exclude_unset=True, mode="json")
        if changes.get("assignee_id") is not None and store().get_user(changes["assignee_id"]) is None:
            raise HTTPException(status_code=422, detail="整改负责人不存在或已停用")
        try:
            task = store().update_remediation_task(task_id, **changes)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="整改任务不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        store().add_event(
            task["case_id"],
            user.id,
            event_type="remediation_task_updated",
            payload={"task_id": task_id},
        )
        record_event(task["plan_id"], user.id, event_type="task_updated", task_id=task_id)
        return task_payload(task)

    @router.post("/api/remediation-tasks/{task_id}/start")
    async def start_remediation_task(
        task_id: str,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        task = store().get_remediation_task(task_id)
        if task is None or task.get("assignee_id") != user.id:
            raise HTTPException(status_code=404, detail="整改任务不存在或无权访问")
        plan = store().get_remediation_plan(task["case_id"])
        if plan is None or plan["status"] != "active":
            raise HTTPException(status_code=409, detail="整改计划尚未激活")
        try:
            updated = store().start_remediation_task(task_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        store().add_event(
            task["case_id"],
            user.id,
            event_type="remediation_task_started",
            payload={"task_id": task_id},
        )
        record_event(task["plan_id"], user.id, event_type="task_started", task_id=task_id)
        return task_payload(updated)

    @router.post("/api/remediation-tasks/{task_id}/evidence")
    async def upload_remediation_evidence(
        task_id: str,
        file: UploadFile = File(...),
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        task = store().get_remediation_task(task_id)
        if task is None or task.get("assignee_id") != user.id:
            raise HTTPException(status_code=404, detail="整改任务不存在或无权访问")
        filename = Path(file.filename or "remediation-evidence").name
        suffix = Path(filename).suffix.lower()
        if suffix not in ALLOWED_UPLOAD_SUFFIXES:
            raise HTTPException(status_code=422, detail={"code": "unsupported_file_type"})
        raw = await file.read()
        if not raw:
            raise HTTPException(status_code=422, detail={"code": "empty_file"})
        if len(raw) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=422, detail={"code": "file_too_large"})
        stored = originals().put_original(
            case_id=task["case_id"],
            logical_name=f"remediation-{task_id}",
            filename=filename,
            content_type=file.content_type or "application/octet-stream",
            content=raw,
        )
        return {
            "kind": "file",
            "label": filename,
            "object_key": stored.object_key,
            "content_type": file.content_type or "application/octet-stream",
            "sha256": stored.sha256,
            "byte_size": stored.byte_size,
        }

    @router.post("/api/remediation-tasks/{task_id}/submissions")
    async def submit_remediation_task(
        task_id: str,
        payload: RemediationSubmissionRequest,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        task = store().get_remediation_task(task_id)
        if task is None or task.get("assignee_id") != user.id:
            raise HTTPException(status_code=404, detail="整改任务不存在或无权访问")
        if not payload.evidence:
            raise HTTPException(status_code=422, detail="提交整改必须附带至少一项证据")
        for item in payload.evidence:
            if item.kind == "link" and (not item.uri or not re.match(r"^https?://", item.uri)):
                raise HTTPException(status_code=422, detail="外部链接必须使用 HTTP(S) 地址")
            if item.kind == "file" and not item.object_key:
                raise HTTPException(status_code=422, detail="文件证据缺少对象存储键")
        try:
            submission = store().create_remediation_submission(
                task_id,
                submitted_by=user.id,
                note=payload.note.strip(),
                evidence=[item.model_dump(mode="json") for item in payload.evidence],
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        store().add_event(
            task["case_id"],
            user.id,
            event_type="remediation_submitted",
            payload={"task_id": task_id, "submission_id": submission["id"]},
        )
        record_event(
            task["plan_id"],
            user.id,
            event_type="submission_created",
            task_id=task_id,
            payload={"submission_id": submission["id"]},
        )
        return submission

    @router.post("/api/remediation-submissions/{submission_id}/review")
    async def review_remediation_submission(
        submission_id: str,
        payload: RemediationSubmissionReviewRequest,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        reviewer_only(user)
        submission = store().get_remediation_submission(submission_id)
        if submission is None:
            raise HTTPException(status_code=404, detail="整改提交不存在")
        task = store().get_remediation_task(submission["task_id"])
        if task is None:
            raise HTTPException(status_code=404, detail="整改任务不存在")
        try:
            reviewed = store().review_remediation_submission(
                submission_id,
                decision=payload.decision,
                reviewed_by=user.id,
                review_note=payload.review_note,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        store().add_event(
            task["case_id"],
            user.id,
            event_type="remediation_submission_reviewed",
            payload={
                "task_id": task["id"],
                "submission_id": submission_id,
                "decision": payload.decision,
            },
        )
        record_event(
            task["plan_id"],
            user.id,
            event_type="submission_reviewed",
            task_id=task["id"],
            payload={"submission_id": submission_id, "decision": payload.decision},
        )
        return reviewed

    @router.get("/api/remediation-evidence/{evidence_id}/download")
    async def download_remediation_evidence(
        evidence_id: str,
        user: UserRecord = Depends(current_user),
    ) -> Response:
        evidence = None
        task_for_evidence: dict[str, Any] | None = None
        for task in store().list_remediation_tasks():
            full = store().get_remediation_task(task["id"])
            for submission in (full or {}).get("submissions", []):
                for item in submission.get("evidence", []):
                    if item["id"] == evidence_id:
                        evidence = item
                        task_for_evidence = full
                        break
        if evidence is None or task_for_evidence is None or not task_viewable(user, task_for_evidence):
            raise HTTPException(status_code=404, detail="整改证据不存在或无权访问")
        if not evidence.get("object_key"):
            raise HTTPException(status_code=409, detail="该证据不是可下载文件")
        try:
            content = originals().get_original(evidence["object_key"])
        except Exception as exc:
            raise HTTPException(status_code=503, detail="整改证据暂时无法下载") from exc
        if evidence.get("sha256") and hashlib.sha256(content).hexdigest() != evidence["sha256"]:
            raise HTTPException(status_code=409, detail="整改证据哈希校验失败")
        return Response(
            content=content,
            media_type=evidence.get("content_type") or "application/octet-stream",
        )

    app.include_router(router)


__all__ = ["register_remediation_routes"]
