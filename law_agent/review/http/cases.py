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

from law_agent.config import load_llm_config
from law_agent.review.case_store import CaseStore, UserRecord
from law_agent.review.enterprise_store import InMemoryEnterpriseStore, PostgresEnterpriseStore
from law_agent.review.http.schemas import (
    CaseCreateRequest,
    CaseStatusRequest,
    CaseUpdateRequest,
    IntakePayload,
    MaterialSnapshotRequest,
)
from law_agent.review.object_store import MaterialObjectStore
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
    evaluate_national_path: Callable[..., Any],
) -> None:
    router = APIRouter()

    @router.post("/api/cases")
    async def create_case_endpoint(
        request: Request,
        user: UserRecord = Depends(current_user),
        title: str | None = Form(default=None),
        question: str | None = Form(default=None),
        material_text: str = Form(default=""),
        material_source: str | None = Form(default=None),
        intake_json: str = Form(default="{}"),
        review_mode: str = Form(default="llm"),
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
                    review_mode=review_mode,
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
            review_mode=payload.review_mode,
            rerank_mode=payload.rerank_mode,
            created_by=user.id,
            owner_id=user.id,
        )
        store().add_event(item["id"], user.id, event_type="case_created", to_status="draft")
        return case_payload(item)

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
        decision = evaluate_national_path(payload.facts)
        rule = enterprise().create_rule_snapshot(
            case_id=identifier,
            material_snapshot_id=snapshot.id,
            ruleset_version=decision.rule_version,
            facts=payload.facts.model_dump(mode="json"),
            determination=decision.model_dump(mode="json"),
        )
        if decision.needs_info and case["status"] != "needs_info":
            store().update_case(identifier, status="needs_info", facts_confirmed=False)
        store().add_event(
            identifier,
            user.id,
            event_type="material_snapshot_frozen",
            payload={
                "material_snapshot_id": snapshot.id,
                "fingerprint": snapshot.fingerprint,
                "rule_snapshot_id": rule.id,
                "rule_version": rule.ruleset_version,
            },
        )
        return {"material_snapshot": asdict(snapshot), "rule_decision": asdict(rule)}

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
            rule = (
                enterprise().get_latest_rule_snapshot(
                    case_id=identifier,
                    material_snapshot_id=snapshot.id,
                )
                if snapshot is not None
                else None
            )
            if snapshot is None or rule is None:
                raise HTTPException(status_code=409, detail="提交前必须冻结材料并完成规则判定")
            if rule.determination.get("needs_info"):
                raise HTTPException(status_code=409, detail="仍有关键事实缺失，不得提交审查")
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
        if case["status"] == "review_running":
            active_task = enterprise().get_latest_task(identifier)
            if active_task is not None and active_task.status in {"queued", "running"}:
                return JSONResponse(
                    status_code=202,
                    content={"task_id": active_task.id, "status": active_task.status},
                )
        if case["status"] != "pending_review":
            raise HTTPException(status_code=409, detail="案件必须完成事实确认并处于待审查状态")
        material_snapshot = enterprise().get_latest_material_snapshot(identifier)
        if material_snapshot is None:
            raise HTTPException(status_code=409, detail="案件尚未生成不可变材料快照")
        rule_snapshot = enterprise().get_latest_rule_snapshot(
            case_id=identifier,
            material_snapshot_id=material_snapshot.id,
        )
        if rule_snapshot is None:
            raise HTTPException(status_code=409, detail="当前材料快照尚未完成全国主路径判定")
        if rule_snapshot.determination.get("needs_info"):
            raise HTTPException(status_code=409, detail="仍有关键事实缺失，不得运行审查或送审")
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
            rule_snapshot_id=rule_snapshot.id,
            model_id=llm_config.model or "not-configured",
            data_boundary_summary={
                "base_url": llm_config.base_url,
                "deployment": os.getenv(
                    "CROSSCOMPLY_MODEL_BOUNDARY",
                    "enterprise-approved-api",
                ),
            },
        )
        validate_case_transition(
            current="pending_review",
            target="review_running",
            authority="local",
        )
        store().update_case(identifier, owner_id=user.id, status="review_running")
        store().add_event(
            identifier,
            user.id,
            event_type="review_queued",
            from_status="pending_review",
            to_status="review_running",
            payload={"task_id": task.id, "material_snapshot_id": material_snapshot.id},
        )
        return JSONResponse(
            status_code=202,
            content={"task_id": task.id, "status": task.status},
        )

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
        latest_rule = (
            enterprise().get_latest_rule_snapshot(
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
            or latest_rule is None
            or latest_snapshot.id != task.material_snapshot_id
            or latest_rule.id != task.rule_snapshot_id
        ):
            raise HTTPException(status_code=409, detail="案件材料或规则快照已变化，请重新提交审查")
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
