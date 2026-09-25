"""Case intake, material, snapshot, and review-task HTTP adapter."""

# FastAPI intentionally declares dependency providers in endpoint defaults.
# ruff: noqa: B008

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from law_agent.config import load_llm_config
from law_agent.kb.enrichment import PostgresEnrichmentStore
from law_agent.review.case_store import CaseStore, UserRecord
from law_agent.review.enterprise_store import InMemoryEnterpriseStore, PostgresEnterpriseStore
from law_agent.review.facts import extract_facts_with_deepseek
from law_agent.review.http.schemas import (
    AgentInputRequest,
    CaseCreateRequest,
    CaseStatusRequest,
    CaseUpdateRequest,
    IntakePayload,
    MaterialSnapshotRequest,
)
from law_agent.review.object_store import MaterialObjectStore
from law_agent.review.schemas import ReviewFacts
from law_agent.review.workflow import CaseStatus, validate_case_transition

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
ALLOWED_UPLOAD_SUFFIXES = {
    ".txt",
    ".md",
    ".markdown",
    ".pdf",
    ".docx",
    ".html",
    ".htm",
    ".json",
    ".csv",
}


def _file_parse_hint(filename: str, exc: BaseException) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    lower = message.lower()
    suffix = Path(filename).suffix.lower()
    if "docling parser requires" in lower:
        return f"无法使用 Docling 解析 {filename}：未安装 docling 或模型文件缺失。"
    if "mineru" in lower:
        return f"无法使用 MinerU 解析 {filename}：未安装 mineru CLI。"
    if "non-zip" in lower:
        return f"{filename} 不是有效的 DOCX 文件，请另存为 .docx 后重试。"
    if suffix in {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"} and (
        "could not load document" in lower
        or "data format error" in lower
        or "conversion failed" in lower
    ):
        return f"{filename} 无法加载：文件为空、损坏或受密码保护。"
    return f"无法解析文件 {filename}：{message}"


def _intake_from_extraction(facts: ReviewFacts) -> IntakePayload:
    """Prefill what the Agent read from the material; anything unread stays unknown."""

    return IntakePayload(
        business_activity=facts.business_activity or "",
        data_types=list(facts.data_types),
        contains_personal_information=facts.contains_personal_information,
        sensitive_personal_info=facts.sensitive_personal_info,
        cross_border_transfer=facts.cross_border_transfer,
        overseas_recipient=facts.overseas_recipient or "",
        processing_purpose=facts.processing_purpose or "",
        legal_basis_or_consent=facts.legal_basis_or_consent or "",
    )


async def material_from_upload(file: UploadFile) -> tuple[str, str]:
    filename = Path(file.filename or "uploaded-material").name
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_UPLOAD_SUFFIXES:
        raise HTTPException(
            status_code=422,
            detail={"code": "unsupported_file_type", "filename": filename},
        )
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=422, detail={"code": "empty_file", "filename": filename})
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=422,
            detail={"code": "file_too_large", "filename": filename},
        )

    from law_agent.review.materials import material_from_file

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / filename
        path.write_bytes(raw)
        try:
            material = material_from_file(path)
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "file_parse_failed",
                    "filename": filename,
                    "message": _file_parse_hint(filename, exc),
                },
            ) from exc
    if not material.material_text.strip():
        raise HTTPException(
            status_code=422,
            detail={"code": "empty_extraction", "filename": filename},
        )
    return material.material_text, filename


def register_case_routes(
    app: FastAPI,
    *,
    current_user: Callable[..., Any],
    reviewer_only: Callable[[UserRecord], None],
    store: Callable[[], CaseStore],
    enterprise: Callable[[], InMemoryEnterpriseStore | PostgresEnterpriseStore],
    originals: Callable[[], MaterialObjectStore],
    case_payload: Callable[[dict[str, Any]], dict[str, Any]],
    case_summary: Callable[[dict[str, Any]], dict[str, Any]],
    can_view: Callable[[UserRecord, dict[str, Any]], bool],
) -> None:
    router = APIRouter()

    def queue_review(
        identifier: str,
        user: UserRecord,
        case: dict[str, Any],
        material_snapshot: Any,
        intake_snapshot: Any,
    ) -> dict[str, Any]:
        """Validate frozen inputs, queue the Agent task and move the case into review."""
        versions = [
            enterprise().get_material_version(version_id)
            for version_id in material_snapshot.version_ids
        ]
        if any(
            version is None
            or version.parse_status != "ready"
            or not (version.parsed_text or "").strip()
            for version in versions
        ):
            raise HTTPException(status_code=409, detail="快照中存在尚未完成通用解析的材料")
        llm_config = load_llm_config()
        task = enterprise().enqueue_review_task(
            case_id=identifier,
            material_snapshot_id=material_snapshot.id,
            intake_snapshot_id=intake_snapshot.id,
            model_id=llm_config.model or "not-configured",
            data_boundary_summary={
                "base_url": llm_config.base_url,
                "deployment": os.getenv(
                    "CROSSCOMPLY_MODEL_BOUNDARY",
                    "enterprise-approved-api",
                ),
            },
        )
        if task.status not in {"queued", "running", "waiting_input"}:
            raise HTTPException(
                status_code=409,
                detail="当前冻结输入已有终态任务；请更新材料或申请人事实后重新运行",
            )
        validate_case_transition(
            current=case["status"],
            target="review_running",
            authority="local",
        )
        store().update_case(identifier, owner_id=user.id, status="review_running")
        store().add_event(
            identifier,
            user.id,
            event_type="review_queued",
            from_status=case["status"],
            to_status="review_running",
            payload={"task_id": task.id, "material_snapshot_id": material_snapshot.id},
        )
        return {"task_id": task.id, "status": task.status}

    @router.post("/api/cases")
    async def create_case_endpoint(
        request: Request,
        user: UserRecord = Depends(current_user),
        title: str | None = Form(default=None),
        question: str | None = Form(default=None),
        material_text: str = Form(default=""),
        material_source: str | None = Form(default=None),
        intake_json: str = Form(default="{}"),
        rerank_mode: str = Form(default="off"),
        file: UploadFile | None = File(default=None),
    ) -> dict[str, Any]:
        if (
            file is None
            and question is None
            and "application/json" in request.headers.get("content-type", "").lower()
        ):
            try:
                payload = CaseCreateRequest.model_validate(await request.json())
            except ValidationError as exc:
                raise HTTPException(status_code=422, detail=exc.errors()) from exc
        else:
            if file is not None:
                material_text, material_source = await material_from_upload(file)
            if not question or not material_text.strip():
                raise HTTPException(status_code=422, detail="question and material_text are required")
            try:
                payload = CaseCreateRequest(
                    title=title,
                    question=question,
                    material_text=material_text,
                    material_source=material_source,
                    intake=IntakePayload.model_validate_json(intake_json or "{}"),
                    rerank_mode=rerank_mode,
                )
            except ValidationError as exc:
                raise HTTPException(status_code=422, detail=exc.errors()) from exc
        item = store().create_case(
            title=payload.title,
            question=payload.question,
            material_text=payload.material_text,
            material_source=payload.material_source,
            intake=payload.intake.model_dump(mode="json"),
            rerank_mode=payload.rerank_mode,
            created_by=user.id,
            owner_id=user.id,
        )
        store().add_event(item["id"], user.id, event_type="case_created", to_status="draft")
        return case_payload(item)

    @router.post("/api/intake-extraction")
    async def extract_intake(
        question: str = Form(default=""),
        material_text: str = Form(default=""),
        files: list[UploadFile] = File(default=[]),
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        """Read the material before a case exists, so the user answers only what blocks a conclusion."""

        texts = [material_text.strip()] if material_text.strip() else []
        for upload in files:
            filename = Path(upload.filename or "uploaded-material").name
            suffix = Path(filename).suffix.lower()
            if suffix not in ALLOWED_UPLOAD_SUFFIXES:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "unsupported_file_type", "filename": filename},
                )
            raw = await upload.read()
            if not raw or len(raw) > MAX_UPLOAD_BYTES:
                code = "empty_file" if not raw else "file_too_large"
                raise HTTPException(status_code=422, detail={"code": code, "filename": filename})
            with tempfile.TemporaryDirectory() as tmpdir:
                path = Path(tmpdir) / filename
                path.write_bytes(raw)
                try:
                    from law_agent.review.materials import material_from_file

                    parsed = material_from_file(path)
                except (FileNotFoundError, RuntimeError, ValueError) as exc:
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "code": "parse_failed",
                            "filename": filename,
                            "message": _file_parse_hint(filename, exc),
                        },
                    ) from exc
            if not (parsed.material_text or "").strip():
                raise HTTPException(
                    status_code=422,
                    detail={"code": "empty_parsed_text", "filename": filename},
                )
            texts.append(parsed.material_text)
        combined = "\n\n".join(texts).strip()
        if not combined:
            raise HTTPException(status_code=422, detail="请先提供待审查材料")
        facts = await run_in_threadpool(extract_facts_with_deepseek, combined, question or None)
        return {
            "intake": _intake_from_extraction(facts).model_dump(mode="json"),
            "missing": [{"key": "material_fact", "reason": item} for item in facts.missing_information],
        }

    @router.post("/api/cases/{identifier}/materials")
    async def upload_material(
        identifier: str,
        logical_name: str = Form(...),
        file: UploadFile = File(...),
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        case = store().get_case(identifier)
        if case is None or not can_view(user, case):
            raise HTTPException(status_code=404, detail="案件不存在或无权访问")
        if case["status"] not in {"draft", "needs_info"}:
            raise HTTPException(status_code=403, detail="当前案件状态不允许补充材料")
        filename = Path(file.filename or "uploaded-material").name
        suffix = Path(filename).suffix.lower()
        if suffix not in ALLOWED_UPLOAD_SUFFIXES:
            raise HTTPException(status_code=422, detail={"code": "unsupported_file_type"})
        raw = await file.read()
        if not raw or len(raw) > MAX_UPLOAD_BYTES:
            code = "empty_file" if not raw else "file_too_large"
            raise HTTPException(status_code=422, detail={"code": code})
        stored = originals().put_original(
            case_id=identifier,
            logical_name=logical_name,
            filename=filename,
            content_type=file.content_type or "application/octet-stream",
            content=raw,
        )
        parse_status = "failed"
        parsed_text: str | None = None
        parser: str | None = None
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / filename
            path.write_bytes(raw)
            try:
                from law_agent.review.materials import material_from_file

                parsed = material_from_file(path)
                parsed_text = parsed.material_text
                parser = "law_agent.review.materials"
                parse_status = "ready" if parsed_text.strip() else "failed"
            except (FileNotFoundError, RuntimeError, ValueError):
                pass
        version = enterprise().create_material_version(
            case_id=identifier,
            logical_name=logical_name,
            filename=filename,
            content_type=file.content_type or "application/octet-stream",
            object_key=stored.object_key,
            sha256=stored.sha256,
            byte_size=stored.byte_size,
            uploaded_by=user.id,
            parse_status=parse_status,
            parser=parser,
            parsed_text=parsed_text,
        )
        store().add_event(
            identifier,
            user.id,
            event_type="material_version_created",
            payload={"material_version_id": version.id, "sha256": version.sha256},
        )
        return asdict(version)

    @router.get("/api/cases/{identifier}/materials")
    async def list_materials(
        identifier: str,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        case = store().get_case(identifier)
        if case is None or not can_view(user, case):
            raise HTTPException(status_code=404, detail="案件不存在或无权访问")
        items = enterprise().list_material_versions(identifier)
        return {"items": [asdict(item) for item in items], "total": len(items)}

    @router.get("/api/materials/{version_id}/download")
    async def download_material(
        version_id: str,
        user: UserRecord = Depends(current_user),
    ) -> Response:
        version = enterprise().get_material_version(version_id)
        if version is None:
            raise HTTPException(status_code=404, detail="材料版本不存在")
        case = store().get_case(version.case_id)
        if case is None or not can_view(user, case):
            raise HTTPException(status_code=404, detail="材料版本不存在或无权访问")
        content = originals().get_original(version.object_key)
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if actual_sha256 != version.sha256 or len(content) != version.byte_size:
            store().add_event(
                version.case_id,
                user.id,
                event_type="material_integrity_failed",
                payload={
                    "material_version_id": version.id,
                    "expected_sha256": version.sha256,
                    "actual_sha256": actual_sha256,
                },
            )
            raise HTTPException(status_code=409, detail="材料原件完整性校验失败")
        return Response(
            content=content,
            media_type=version.content_type,
            headers={
                "Content-Disposition": (
                    f"attachment; filename=material{Path(version.filename).suffix}; "
                    f"filename*=UTF-8''{quote(version.filename)}"
                )
            },
        )

    @router.post("/api/cases/{identifier}/material-snapshots")
    async def freeze_material_snapshot(
        identifier: str,
        payload: MaterialSnapshotRequest,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        case = store().get_case(identifier)
        if case is None or not can_view(user, case):
            raise HTTPException(status_code=404, detail="案件不存在或无权访问")
        if case["status"] not in {"draft", "needs_info"}:
            raise HTTPException(status_code=409, detail="当前案件状态不允许重新冻结材料快照")
        snapshot = enterprise().create_material_snapshot(
            case_id=identifier,
            version_ids=payload.version_ids,
            created_by=user.id,
        )
        intake = IntakePayload.model_validate(case.get("intake") or {}).model_dump(mode="json")
        intake_snapshot = enterprise().create_intake_snapshot(
            case_id=identifier,
            material_snapshot_id=snapshot.id,
            intake=intake,
            created_by=user.id,
        )
        # The frozen inputs just changed, so a paused Agent question asked about the previous
        # snapshot is stale: close it instead of letting it be resumed against old material.
        for superseded_task_id in enterprise().supersede_waiting_tasks(identifier):
            store().add_event(
                identifier,
                user.id,
                event_type="review_task_superseded",
                payload={"task_id": superseded_task_id, "material_snapshot_id": snapshot.id},
            )
        store().add_event(
            identifier,
            user.id,
            event_type="material_snapshot_frozen",
            payload={
                "material_snapshot_id": snapshot.id,
                "fingerprint": snapshot.fingerprint,
                "intake_snapshot_id": intake_snapshot.id,
                "intake_fingerprint": intake_snapshot.fingerprint,
            },
        )
        return {"material_snapshot": asdict(snapshot), "intake_snapshot": asdict(intake_snapshot)}

    @router.get("/api/cases")
    async def list_cases(
        query: str | None = None,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        cases = store().list_cases(user, query)
        return {"items": [case_summary(case) for case in cases], "total": len(cases)}

    @router.get("/api/cases/{identifier}")
    async def get_case(
        identifier: str,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        case = store().get_case(identifier)
        if case is None or not can_view(user, case):
            raise HTTPException(status_code=404, detail="案件不存在或无权访问")
        return case_payload(case)

    @router.get("/api/cases/{identifier}/knowledge-rechecks")
    async def get_case_knowledge_rechecks(
        identifier: str, user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        case = store().get_case(identifier)
        if case is None or not can_view(user, case):
            raise HTTPException(status_code=404, detail="案件不存在或无权访问")
        from law_agent.config import load_service_config
        items = PostgresEnrichmentStore(load_service_config().postgres.dsn).case_rechecks(identifier)
        return {"items": items}

    @router.patch("/api/cases/{identifier}")
    async def update_case(
        identifier: str,
        payload: CaseUpdateRequest,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        case = store().get_case(identifier)
        if case is None or not can_view(user, case):
            raise HTTPException(status_code=404, detail="案件不存在或无权访问")
        if case["status"] in {"approved", "conditionally_approved", "rejected"}:
            raise HTTPException(status_code=409, detail="已签署案件不允许继续修改")
        if user.role == "requester" and case["status"] not in {"draft", "needs_info"}:
            raise HTTPException(status_code=403, detail="当前案件状态不允许申请人编辑")
        values = payload.model_dump(exclude_unset=True, mode="json")
        if "intake" in values and values["intake"] is not None:
            values["intake_json"] = values.pop("intake")
        if user.role == "requester":
            values.pop("owner_id", None)
        updated = store().update_case(identifier, **values)
        store().add_event(
            identifier,
            user.id,
            event_type="case_updated",
            payload={"fields": list(values)},
        )
        return case_payload(updated)

    @router.post("/api/cases/{identifier}/status")
    async def update_case_status(
        identifier: str,
        payload: CaseStatusRequest,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        case = store().get_case(identifier)
        if case is None or not can_view(user, case):
            raise HTTPException(status_code=404, detail="案件不存在或无权访问")
        current: CaseStatus = case["status"]
        if payload.status in {"approved", "conditionally_approved", "rejected"}:
            raise HTTPException(status_code=403, detail="审批终态只能由飞书已验签事件回写")
        if payload.status in {"review_running", "pending_feishu_approval", "run_failed"}:
            raise HTTPException(status_code=403, detail="该状态只能由审查任务或 worker 写入")
        if user.role == "requester" and payload.status != "pending_review":
            raise HTTPException(status_code=403, detail="申请人只能确认事实并提交待审")
        if user.role != "requester":
            reviewer_only(user)
        if payload.status == "pending_review":
            snapshot = enterprise().get_latest_material_snapshot(identifier)
            intake_snapshot = (
                enterprise().get_latest_intake_snapshot(
                    case_id=identifier,
                    material_snapshot_id=snapshot.id,
                )
                if snapshot is not None
                else None
            )
            if snapshot is None or intake_snapshot is None:
                raise HTTPException(status_code=409, detail="提交前必须冻结材料与申请人事实")
            if intake_snapshot.intake != IntakePayload.model_validate(case.get("intake") or {}).model_dump(mode="json"):
                raise HTTPException(status_code=409, detail="申请人事实已变化，请重新冻结快照")
        try:
            validate_case_transition(current=current, target=payload.status, authority="local")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        status_values: dict[str, Any] = {"status": payload.status}
        if payload.status == "pending_review":
            status_values["facts_confirmed"] = True
        updated = store().update_case(identifier, **status_values)
        store().add_event(
            identifier,
            user.id,
            event_type="status_changed",
            from_status=current,
            to_status=payload.status,
            payload={"note": payload.note},
        )
        if payload.status == "pending_review":
            # 提交即进入审查队列：申请人不需要再等审核人手动启动。
            # A failed enqueue must not leave the case looking submitted while it is not queued,
            # and the audit trail must not show an unexplained successful status change.
            try:
                queue_review(identifier, user, updated, snapshot, intake_snapshot)
            except (HTTPException, ValueError) as exc:
                store().update_case(
                    identifier, status=current, facts_confirmed=case.get("facts_confirmed", False)
                )
                store().add_event(
                    identifier,
                    user.id,
                    event_type="status_change_rolled_back",
                    from_status=payload.status,
                    to_status=current,
                    payload={"reason": str(getattr(exc, "detail", exc))},
                )
                if isinstance(exc, HTTPException):
                    raise
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            updated = store().get_case(identifier) or updated
        return case_payload(updated)

    @router.post("/api/cases/{identifier}/run")
    async def run_case(
        identifier: str,
        user: UserRecord = Depends(current_user),
    ) -> JSONResponse:
        reviewer_only(user)
        case = store().get_case(identifier)
        if case is None:
            raise HTTPException(status_code=404, detail="案件不存在")
        if case["status"] in {"review_running", "needs_info"}:
            active_task = enterprise().get_latest_task(identifier)
            if active_task is not None and active_task.status in {
                "queued", "running", "waiting_input"
            }:
                return JSONResponse(
                    status_code=202,
                    content={"task_id": active_task.id, "status": active_task.status},
                )
        if case["status"] not in {"pending_review", "needs_info", "pending_source_verification"}:
            raise HTTPException(status_code=409, detail="案件必须处于待审查或待补充信息状态")
        material_snapshot = enterprise().get_latest_material_snapshot(identifier)
        if material_snapshot is None:
            raise HTTPException(status_code=409, detail="案件尚未生成不可变材料快照")
        intake_snapshot = enterprise().get_latest_intake_snapshot(
            case_id=identifier,
            material_snapshot_id=material_snapshot.id,
        )
        if intake_snapshot is None:
            raise HTTPException(status_code=409, detail="当前材料尚未冻结申请人事实")
        latest_task = enterprise().get_latest_task(identifier)
        if (
            latest_task is not None
            and latest_task.status == "succeeded"
            and latest_task.material_snapshot_id == material_snapshot.id
            and latest_task.intake_snapshot_id == intake_snapshot.id
        ):
            raise HTTPException(
                status_code=409,
                detail="当前冻结输入已完成调查；请补充材料或事实后重新运行",
            )
        queued = queue_review(identifier, user, case, material_snapshot, intake_snapshot)
        return JSONResponse(status_code=202, content=queued)

    @router.get("/api/tasks/{task_id}")
    async def get_review_task(
        task_id: str,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        task = enterprise().get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="审查任务不存在")
        case = store().get_case(task.case_id)
        if case is None or not can_view(user, case):
            raise HTTPException(status_code=404, detail="审查任务不存在或无权访问")
        return asdict(task)

    @router.post("/api/tasks/{task_id}/answer")
    async def answer_agent_question(
        task_id: str,
        payload: AgentInputRequest,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        from law_agent.review.agent import AgentState, answer_agent

        reviewer_only(user)
        task = enterprise().get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="审查任务不存在")
        case = store().get_case(task.case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="案件不存在")
        if payload.changes_frozen_facts:
            raise HTTPException(
                status_code=409,
                detail="该补充会改变申请人确认事实，请重新冻结事实与材料后提交",
            )
        if task.agent_state is None:
            raise HTTPException(status_code=409, detail="任务没有可恢复的 Agent 状态")
        try:
            state = answer_agent(
                AgentState.model_validate(task.agent_state),
                gate_id=payload.gate_id,
                answer=payload.answer,
            )
            resumed = enterprise().resume_task(
                task_id, state=state.model_dump(mode="json")
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        store().update_case(task.case_id, status="review_running")
        store().add_event(
            task.case_id,
            user.id,
            event_type="agent_input_received",
            from_status=case["status"],
            to_status="review_running",
            payload={"task_id": task_id, "gate_id": payload.gate_id},
        )
        return asdict(resumed)

    @router.post("/api/tasks/{task_id}/retry")
    async def retry_review_task(
        task_id: str,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        reviewer_only(user)
        task = enterprise().get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="审查任务不存在")
        case = store().get_case(task.case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="案件不存在")
        latest_task = enterprise().get_latest_task(task.case_id)
        latest_snapshot = enterprise().get_latest_material_snapshot(task.case_id)
        latest_intake = (
            enterprise().get_latest_intake_snapshot(
                case_id=task.case_id,
                material_snapshot_id=latest_snapshot.id,
            )
            if latest_snapshot is not None
            else None
        )
        if case["status"] != "run_failed":
            raise HTTPException(status_code=409, detail="只有运行失败的案件可以重试")
        if latest_task is None or latest_task.id != task.id:
            raise HTTPException(status_code=409, detail="只能重试案件当前的审查任务")
        if (
            latest_snapshot is None
            or latest_intake is None
            or latest_snapshot.id != task.material_snapshot_id
            or latest_intake.id != task.intake_snapshot_id
        ):
            raise HTTPException(status_code=409, detail="案件材料或事实快照已变化，请重新提交审查")
        try:
            retried = enterprise().retry_task(task_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        validate_case_transition(current="run_failed", target="review_running", authority="local")
        store().update_case(task.case_id, status="review_running")
        store().add_event(
            task.case_id,
            user.id,
            event_type="review_retried",
            from_status="run_failed",
            to_status="review_running",
            payload={"task_id": task_id},
        )
        return asdict(retried)

    app.include_router(router)


__all__ = ["register_case_routes"]
