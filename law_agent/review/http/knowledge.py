"""Knowledge-base administration HTTP adapter.

This module owns the transport concerns for source listing, local-file import,
metadata replacement, recycle-bin deletion, restoration, and asynchronous job
status.  The actual corpus mutation remains in :mod:`law_agent.kb.admin`.
"""

# FastAPI intentionally declares dependency providers in endpoint defaults.
# ruff: noqa: B008

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from law_agent.data.schemas import LibraryKind, SourceRecord
from law_agent.kb.admin import (
    InMemoryKnowledgeJobStore,
    JobStatus,
    KnowledgeBaseAdminService,
    KnowledgeJobStore,
    source_summary_payload,
)
from law_agent.kb.ingestion import prepare_document_for_ingest
from law_agent.kb.service import normalized_content_hash
from law_agent.review.case_store import UserRecord
from law_agent.review.http.schemas import (
    KnowledgeDeleteJobRequest,
    KnowledgeDeletePreviewRequest,
    KnowledgeImportCommitRequest,
    KnowledgeMetadataUpdateRequest,
    KnowledgeRestoreRequest,
)

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


def configure_knowledge_state(
    app: FastAPI,
    *,
    corpus: Path,
    job_store: KnowledgeJobStore | None,
) -> None:
    """Attach the knowledge service and its process-local job runtime."""

    app.state.knowledge_corpus = corpus
    app.state.knowledge_job_store = job_store or InMemoryKnowledgeJobStore()
    app.state.knowledge_service = KnowledgeBaseAdminService(corpus, read_only=False)
    app.state.knowledge_previews = {}
    app.state.knowledge_delete_tokens = {}
    app.state.knowledge_executor = ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="knowledge-admin",
    )


def initialize_knowledge_state(app: FastAPI) -> None:
    app.state.knowledge_job_store.initialize()


def shutdown_knowledge_state(app: FastAPI) -> None:
    app.state.knowledge_executor.shutdown(wait=False, cancel_futures=True)


def register_knowledge_routes(
    app: FastAPI,
    *,
    current_user: Callable[..., Any],
    admin_only: Callable[[UserRecord], None],
) -> None:
    """Register all knowledge administration endpoints on ``app``."""

    router = APIRouter(prefix="/api/admin")

    def knowledge_job_payload(job: Any) -> dict[str, Any]:
        return asdict(job)

    def safe_source_id(source_id: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,180}", source_id):
            raise HTTPException(status_code=400, detail="来源 ID 格式无效")
        return source_id

    def proposed_source_id(kind: LibraryKind, title: str, metadata: dict[str, Any]) -> str:
        # The ID is a source identity, not an editable metadata fingerprint.
        # Keep mutable fields such as issuing body and owning department out
        # of it so a metadata correction still replaces the same source.
        identity = "\x1f".join(
            (
                kind,
                title.strip(),
                str(metadata.get("source_url") or ""),
            )
        )
        prefix = "legal" if kind == "legal" else "policy"
        return f"{prefix}_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]}"

    def source_from_import_metadata(
        kind: LibraryKind,
        filename: str,
        metadata: dict[str, Any],
    ) -> SourceRecord:
        title = str(metadata.get("title") or Path(filename).stem).strip()[:240]
        if not title:
            raise ValueError("来源标题不能为空")
        source_url = str(metadata.get("source_url") or f"local://{filename}")
        doc_type = metadata.get("doc_type") or (
            "internal_policy" if kind == "internal_policy" else "guideline"
        )
        return SourceRecord(
            source_id=proposed_source_id(kind, title, {**metadata, "source_url": source_url}),
            library_kind=kind,
            title=title,
            source_url=source_url,
            download_url=metadata.get("download_url"),
            source_site=str(metadata.get("source_site") or "local_import"),
            doc_type=doc_type,
            authority=metadata.get("authority", "unknown"),
            law_status=metadata.get("law_status", "unknown"),
            publish_date=metadata.get("publish_date"),
            effective_date=metadata.get("effective_date"),
            issuing_body=metadata.get("issuing_body"),
            owning_department=metadata.get("owning_department"),
            internal_status=metadata.get("internal_status"),
            topic_tags=metadata.get("topic_tags") or [],
            file_format=Path(filename).suffix.lstrip(".") or "txt",
            include_in_mvp=True,
        )

    def execute_knowledge_job(job_id: str) -> None:
        job = app.state.knowledge_job_store.get(job_id)
        if job is None:
            return
        app.state.knowledge_job_store.update(job_id, status="running", error=None)
        service = KnowledgeBaseAdminService(app.state.knowledge_corpus, read_only=False)
        results: list[dict[str, Any]] = []
        failures = 0
        try:
            entries = list(job.payload.get("entries") or [])
            if job.job_type in {"import", "metadata"}:
                entries = entries or [job.payload]
                for entry in entries:
                    try:
                        source = SourceRecord.model_validate(entry["source"])
                        path_key = "temp_path" if job.job_type == "import" else "raw_path"
                        raw_path = Path(entry[path_key])
                        result = service.ingest_file(source, raw_path)
                        results.append({"source_id": source.source_id, "status": "succeeded", **result})
                    except Exception as exc:  # noqa: BLE001 - persist per-source failure
                        failures += 1
                        results.append(
                            {
                                "source_id": entry.get("source", {}).get("source_id"),
                                "status": "failed",
                                "error": str(exc),
                            }
                        )
            elif job.job_type == "delete":
                for source_id in entries:
                    try:
                        record = service.trash_source(safe_source_id(str(source_id)))
                        results.append(
                            {
                                "source_id": source_id,
                                "status": "succeeded",
                                "trashed_at": record.trashed_at,
                            }
                        )
                    except Exception as exc:  # noqa: BLE001 - persist per-source failure
                        failures += 1
                        results.append(
                            {"source_id": source_id, "status": "failed", "error": str(exc)}
                        )
            elif job.job_type == "restore":
                source_id = safe_source_id(str(job.payload["source_id"]))
                result = service.restore_source(source_id)
                results.append({"source_id": source_id, "status": "succeeded", **result})
            status: JobStatus = (
                "failed"
                if not results or failures == len(results)
                else "partially_succeeded"
                if failures
                else "succeeded"
            )
            app.state.knowledge_job_store.update(job_id, status=status, result={"items": results})
        except Exception as exc:  # noqa: BLE001 - persist unexpected job failure
            app.state.knowledge_job_store.update(
                job_id,
                status="failed",
                error=str(exc),
                result={"items": results},
            )
        finally:
            if job.job_type == "import":
                for entry in job.payload.get("entries") or []:
                    temp_value = entry.get("temp_path")
                    if not temp_value:
                        continue
                    temp_path = Path(str(temp_value))
                    shutil.rmtree(temp_path.parent, ignore_errors=True)

    def enqueue_knowledge_job(
        job_type: str,
        payload: dict[str, Any],
        user: UserRecord,
    ) -> Any:
        job = app.state.knowledge_job_store.create(job_type, payload, user.id)
        app.state.knowledge_executor.submit(execute_knowledge_job, job.id)
        return job

    @router.get("/knowledge-sources")
    async def list_knowledge_sources(
        library_kind: LibraryKind,
        query: str = "",
        status: str | None = None,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        admin_only(user)
        items = app.state.knowledge_service.list_sources(
            library_kind=library_kind,
            query=query,
            status=status,
        )
        return {"items": [source_summary_payload(item) for item in items], "total": len(items)}

    @router.get("/knowledge-sources/{source_id}")
    async def get_knowledge_source(
        source_id: str,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        admin_only(user)
        try:
            return app.state.knowledge_service.get_source(safe_source_id(source_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="知识库来源不存在") from exc

    @router.get("/knowledge-sources/{source_id}/raw")
    async def download_knowledge_source(
        source_id: str,
        user: UserRecord = Depends(current_user),
    ) -> FileResponse:
        admin_only(user)
        try:
            raw_path = app.state.knowledge_service.raw_path(safe_source_id(source_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="原始来源不存在") from exc
        return FileResponse(raw_path, filename=raw_path.name)

    @router.post("/knowledge-import-previews")
    async def preview_knowledge_import(
        library_kind: LibraryKind = Form(...),
        metadata_json: str = Form("[]"),
        files: list[UploadFile] = File(...),
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        admin_only(user)
        if not files or len(files) > 50:
            raise HTTPException(status_code=422, detail="一次最多预检 50 个文件")
        try:
            metadata_items = json.loads(metadata_json)
            if not isinstance(metadata_items, list):
                raise TypeError("metadata_json 必须是数组")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail="导入元数据格式无效") from exc

        preview_id = f"kbpreview_{uuid.uuid4().hex[:16]}"
        temp_root = Path(tempfile.mkdtemp(prefix=f"{preview_id}_"))
        state = app.state.knowledge_service._read_state()
        existing = {
            item.source.source_id: item
            for item in app.state.knowledge_service.list_sources(library_kind=library_kind)
        }
        items: list[dict[str, Any]] = []
        try:
            for index, upload in enumerate(files):
                filename = Path(upload.filename or f"source-{index}.txt").name
                suffix = Path(filename).suffix.lower()
                if suffix not in ALLOWED_UPLOAD_SUFFIXES:
                    raise HTTPException(status_code=422, detail=f"不支持的文件格式：{filename}")
                content = await upload.read()
                if not content:
                    raise HTTPException(status_code=422, detail=f"文件不能为空：{filename}")
                if len(content) > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail=f"文件超过 20 MB：{filename}")

                path = temp_root / filename
                path.write_bytes(content)
                metadata = (
                    metadata_items[index]
                    if index < len(metadata_items) and isinstance(metadata_items[index], dict)
                    else {}
                )
                source = source_from_import_metadata(library_kind, filename, metadata)
                # A source ID survives metadata edits.  When an operator
                # re-imports the same titled file without an explicit URL,
                # reuse the unique existing title identity instead of
                # creating a second source after the URL was corrected.
                if source.source_id not in existing and not metadata.get("source_url"):
                    title_matches = [
                        value.source
                        for value in existing.values()
                        if value.source.title.casefold() == source.title.casefold()
                    ]
                    if len(title_matches) == 1:
                        source = source.model_copy(update={"source_id": title_matches[0].source_id})
                item: dict[str, Any] = {
                    "id": f"item_{index}_{uuid.uuid4().hex[:8]}",
                    "filename": filename,
                    "size": len(content),
                    "source": source.model_dump(mode="json"),
                    "temp_path": str(path),
                    "action": "add",
                    "error": None,
                }
                try:
                    document = prepare_document_for_ingest(path, parser="auto")
                    digest = normalized_content_hash(document.text)
                    item["content_hash"] = digest
                    same_source = state.get("sources", {}).get(source.source_id, {})
                    if source.source_id in existing:
                        item["action"] = (
                            "skip" if same_source.get("content_hash") == digest else "replace"
                        )
                    elif any(
                        value.get("content_hash") == digest
                        for value in state.get("sources", {}).values()
                    ):
                        item["action"] = "skip"
                        item["duplicate"] = True
                except Exception as exc:  # noqa: BLE001 - expose per-file parser errors
                    item["action"] = "error"
                    item["error"] = str(exc)
                items.append(item)
        except Exception:
            shutil.rmtree(temp_root, ignore_errors=True)
            raise

        app.state.knowledge_previews[preview_id] = {
            "created_at": datetime.now(UTC).isoformat(),
            "library_kind": library_kind,
            "created_by": user.id,
            "temp_root": str(temp_root),
            "items": items,
        }
        return {
            "preview_id": preview_id,
            "items": [
                {key: value for key, value in item.items() if key != "temp_path"}
                for item in items
            ],
        }

    @router.post("/knowledge-import-jobs", status_code=202)
    async def create_knowledge_import_job(
        payload: KnowledgeImportCommitRequest,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        admin_only(user)
        preview = app.state.knowledge_previews.pop(payload.preview_id, None)
        if preview is None or preview.get("created_by") != user.id:
            raise HTTPException(status_code=404, detail="导入预检不存在或已过期")
        entries = [
            item for item in preview["items"] if item.get("action") in {"add", "replace"}
        ]
        if not entries:
            shutil.rmtree(Path(str(preview.get("temp_root", ""))), ignore_errors=True)
            return {"job": {"status": "succeeded", "result": {"items": preview["items"]}}}
        job = enqueue_knowledge_job("import", {"entries": entries}, user)
        return {"job": knowledge_job_payload(job)}

    @router.patch("/knowledge-sources/{source_id}/metadata", status_code=202)
    async def update_knowledge_metadata(
        source_id: str,
        payload: KnowledgeMetadataUpdateRequest,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        admin_only(user)
        source_id = safe_source_id(source_id)
        try:
            detail = app.state.knowledge_service.get_source(source_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="知识库来源不存在") from exc
        values = payload.model_dump(exclude_unset=True)
        if not values:
            raise HTTPException(status_code=422, detail="至少提供一项元数据修改")
        source = SourceRecord.model_validate(detail["source"]).model_copy(update=values)
        raw_path = detail.get("raw_path")
        if not raw_path:
            raise HTTPException(status_code=409, detail="来源没有可重新解析的原文件")
        job = enqueue_knowledge_job(
            "metadata",
            {"entries": [{"source": source.model_dump(mode="json"), "raw_path": raw_path}]},
            user,
        )
        return {"job": knowledge_job_payload(job)}

    @router.post("/knowledge-delete-previews")
    async def preview_knowledge_delete(
        payload: KnowledgeDeletePreviewRequest,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        admin_only(user)
        source_ids = [safe_source_id(item) for item in dict.fromkeys(payload.source_ids)]
        summaries = {
            item.source.source_id: item
            for item in app.state.knowledge_service.list_sources(library_kind=payload.library_kind)
        }
        missing = [item for item in source_ids if item not in summaries]
        items = [source_summary_payload(summaries[item]) for item in source_ids if item in summaries]
        token = uuid.uuid4().hex
        app.state.knowledge_delete_tokens[token] = {
            "source_ids": source_ids,
            "library_kind": payload.library_kind,
            "expires_at": datetime.now(UTC).timestamp() + 600,
            "created_by": user.id,
        }
        return {
            "token": token,
            "items": items,
            "missing": missing,
            "total_chunks": sum(item["chunk_count"] for item in items),
        }

    @router.post("/knowledge-delete-jobs", status_code=202)
    async def create_knowledge_delete_job(
        payload: KnowledgeDeleteJobRequest,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        admin_only(user)
        token = app.state.knowledge_delete_tokens.pop(payload.token, None)
        if (
            token is None
            or token["created_by"] != user.id
            or token["expires_at"] < datetime.now(UTC).timestamp()
        ):
            raise HTTPException(status_code=409, detail="删除预检已过期，请重新预检")
        source_ids = token["source_ids"]
        if payload.confirmation.strip() != f"删除 {len(source_ids)} 项":
            raise HTTPException(status_code=422, detail=f"请输入：删除 {len(source_ids)} 项")
        job = enqueue_knowledge_job("delete", {"entries": source_ids}, user)
        return {"job": knowledge_job_payload(job)}

    @router.get("/knowledge-trash")
    async def list_knowledge_trash(
        library_kind: LibraryKind,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        admin_only(user)
        items = app.state.knowledge_service.list_trash(library_kind)
        return {"items": items, "total": len(items)}

    @router.post("/knowledge-trash/{source_id}/restore", status_code=202)
    async def restore_knowledge_source(
        source_id: str,
        payload: KnowledgeRestoreRequest,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        admin_only(user)
        source_id = safe_source_id(source_id)
        records = [
            item
            for item in app.state.knowledge_service.list_trash(payload.library_kind)
            if item["source_id"] == source_id
        ]
        if not records:
            raise HTTPException(status_code=404, detail="回收站中不存在该来源")
        job = enqueue_knowledge_job("restore", {"source_id": source_id}, user)
        return {"job": knowledge_job_payload(job)}

    @router.get("/knowledge-jobs/{job_id}")
    async def get_knowledge_job(
        job_id: str,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        admin_only(user)
        job = app.state.knowledge_job_store.get(job_id)
        if job is None or job.created_by != user.id:
            raise HTTPException(status_code=404, detail="知识库任务不存在")
        return {"job": knowledge_job_payload(job)}

    app.include_router(router)


__all__ = [
    "configure_knowledge_state",
    "initialize_knowledge_state",
    "register_knowledge_routes",
    "shutdown_knowledge_state",
]
