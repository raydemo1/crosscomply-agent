"""FastAPI transport for the CrossComply compliance case workbench.

The public API is case-oriented.  Review execution remains in the existing
RAG/application services, while this module owns authentication, persistence,
workflow transitions, and the HTTP contract used by the frontend.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, field_validator

from law_agent.config import RerankMode, load_llm_config, load_service_config
from law_agent.review.case_store import (
    CASE_TRANSITIONS,
    CaseStatus,
    CaseStore,
    PostgresCaseStore,
    UserRecord,
)
from law_agent.review.evalset.cases import EvalSuite
from law_agent.review.evalset.runner import ReviewEvalMode, run_evaluation
from law_agent.review.evalset.schemas import EvalSummary
from law_agent.review.io import read_review_results
from law_agent.review.llm import ReviewWorkflowFailed
from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH
from law_agent.review.schemas import (
    CitationGroup,
    EvidenceSelfCheck,
    RetrievalHit,
    RetrievalQuery,
    ReviewFacts,
    ReviewFailedResponse,
    ReviewResult,
    SourceEvidencePacket,
)
from law_agent.review.service import ReviewMode, create_review_case, run_service_retrieval

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
ALLOWED_UPLOAD_SUFFIXES = {
    ".txt", ".md", ".markdown", ".pdf", ".docx", ".html", ".htm", ".json",
}
SESSION_COOKIE = "crosscomply_session"


class IntakePayload(BaseModel):
    business_activity: str = ""
    data_types: list[str] = Field(default_factory=list)
    sensitive_personal_info: bool | None = None
    cross_border_transfer: bool | None = None
    important_data_status: Literal["unknown", "not_important", "important", "under_review"] = "unknown"
    ciio_status: Literal["unknown", "not_ciio", "ciio", "under_review"] = "unknown"
    annual_non_sensitive_count: str = ""
    annual_sensitive_count: str = ""
    overseas_recipient: str = ""
    destination_region: str = ""
    processing_purpose: str = ""
    transfer_mechanism: str = ""
    vendor_name: str = ""
    contract_status: str = ""
    legal_basis_or_consent: str = ""
    notes: str = ""


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class CaseCreateRequest(BaseModel):
    title: str | None = None
    question: str = Field(..., min_length=1)
    material_text: str = Field(..., min_length=1)
    material_source: str | None = None
    intake: IntakePayload = Field(default_factory=IntakePayload)
    review_mode: ReviewMode = "llm"
    rerank_mode: RerankMode = "off"

    @field_validator("question", "material_text")
    @classmethod
    def must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value.strip()


class CaseUpdateRequest(BaseModel):
    title: str | None = None
    question: str | None = None
    material_text: str | None = None
    intake: IntakePayload | None = None
    facts_confirmed: bool | None = None
    owner_id: str | None = None


class CaseStatusRequest(BaseModel):
    status: Literal["draft", "submitted", "in_review", "needs_info", "completed", "review_failed"]
    note: str = ""


class CaseActionRequest(BaseModel):
    title: str = Field(..., min_length=1)
    description: str = ""
    owner_role: str = "reviewer"
    priority: Literal["high", "medium", "low"] = "medium"
    status: Literal["open", "in_progress", "completed"] = "open"
    due_date: str | None = None


class CaseActionUpdateRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    owner_role: str | None = None
    priority: Literal["high", "medium", "low"] | None = None
    status: Literal["open", "in_progress", "completed"] | None = None
    due_date: str | None = None


class FeedbackRequest(BaseModel):
    conclusion_useful: bool | None = None
    missing_sources: str = ""
    notes: str = ""
    citation_verdicts: dict[str, str] = Field(default_factory=dict)


class ReviewResponse(BaseModel):
    """Structured result persisted inside a workbench case."""

    review_case_id: str
    trace_id: str
    review_facts: ReviewFacts
    review_result: ReviewResult
    evidence_self_check: EvidenceSelfCheck
    citation_groups: list[CitationGroup] = Field(default_factory=list)
    second_retrieval_triggered: bool = False
    retrieval_queries: list[RetrievalQuery] = Field(default_factory=list)
    evidence_chunks: list[RetrievalHit] = Field(default_factory=list)
    source_evidence_packets: list[SourceEvidencePacket] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str = "ok"
    llm: dict[str, Any] = Field(default_factory=dict)
    services: dict[str, Any] = Field(default_factory=dict)
    corpus: dict[str, Any] = Field(default_factory=dict)


class EvalRunRequest(BaseModel):
    chunks_path: str | None = None
    review_mode: ReviewEvalMode = "llm"
    top_k: int = Field(default=10, ge=1, le=100)
    max_workers: int = Field(default=4, ge=1, le=16)
    rerank_mode: RerankMode = "off"
    suite: EvalSuite = "full"


class EvalJobResponse(BaseModel):
    job_id: str | None = None
    status: Literal["idle", "running", "succeeded", "failed"] = "idle"
    message: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


def _now_iso() -> str:
    from law_agent.review.ids import utc_now_iso

    return utc_now_iso()


def _idle_job() -> dict[str, Any]:
    return {
        "job_id": None,
        "status": "idle",
        "message": None,
        "started_at": None,
        "finished_at": None,
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
        "could not load document" in lower or "data format error" in lower or "conversion failed" in lower
    ):
        return f"{filename} 无法加载：文件为空、损坏或受密码保护。"
    return f"无法解析文件 {filename}：{message}"


def _intake_context(intake: dict[str, Any]) -> str:
    labels = {
        "business_activity": "业务活动",
        "data_types": "数据类型",
        "sensitive_personal_info": "敏感个人信息",
        "cross_border_transfer": "跨境传输",
        "important_data_status": "重要数据识别状态",
        "ciio_status": "关键信息基础设施运营者状态",
        "annual_non_sensitive_count": "非敏感个人信息数量区间",
        "annual_sensitive_count": "敏感个人信息数量区间",
        "overseas_recipient": "境外接收方",
        "destination_region": "目的地",
        "processing_purpose": "处理目的",
        "transfer_mechanism": "拟采用的出境路径",
        "vendor_name": "供应商",
        "contract_status": "合同状态",
        "legal_basis_or_consent": "法律依据或同意",
        "notes": "补充说明",
    }
    lines: list[str] = []
    for key, value in intake.items():
        if value in (None, "", [], "unknown"):
            continue
        if isinstance(value, bool):
            value = "是" if value else "否"
        if isinstance(value, list):
            value = "、".join(str(item) for item in value)
        lines.append(f"- {labels.get(key, key)}：{value}")
    return "\n【申请人已确认的案件要素】\n" + "\n".join(lines) if lines else ""


async def _material_from_upload(file: UploadFile) -> tuple[str, str]:
    filename = Path(file.filename or "uploaded-material").name
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_UPLOAD_SUFFIXES:
        raise HTTPException(status_code=422, detail={"code": "unsupported_file_type", "filename": filename})
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=422, detail={"code": "empty_file", "filename": filename})
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=422, detail={"code": "file_too_large", "filename": filename})

    from law_agent.review.materials import material_from_file

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / filename
        path.write_bytes(raw)
        try:
            material = material_from_file(path)
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail={"code": "file_parse_failed", "filename": filename, "message": _file_parse_hint(filename, exc)}) from exc
    if not material.material_text.strip():
        raise HTTPException(status_code=422, detail={"code": "empty_extraction", "filename": filename})
    return material.material_text, filename


def _preload_eval_cache(app: FastAPI, cache_dir: Path) -> None:
    import glob

    if not cache_dir.exists():
        return
    patterns = {
        "off": ["*rerank_off*", "*rerank=off*", "*rerank-off*"],
        "embedding": ["*rerank_on*", "*rerank=on*", "*rerank-on*"],
    }
    for arm, names in patterns.items():
        candidates: list[Path] = []
        for name in names:
            candidates.extend(Path(path) for path in glob.glob(str(cache_dir / name)))
        if not candidates:
            continue
        latest = max(candidates, key=lambda path: path.stat().st_mtime)
        try:
            app.state.eval_cache[arm] = EvalSummary.model_validate_json(latest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue


def _case_summary(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": case["id"],
        "title": case["title"],
        "question": case["question"],
        "status": case["status"],
        "risk_level": case.get("risk_level"),
        "facts_confirmed": case.get("facts_confirmed", False),
        "created_by": case["created_by"],
        "owner_id": case.get("owner_id"),
        "created_at": case["created_at"],
        "updated_at": case["updated_at"],
        "has_result": case.get("response") is not None,
    }


def _case_payload(store: CaseStore, case: dict[str, Any]) -> dict[str, Any]:
    case_payload = dict(case)
    if isinstance(case_payload.get("response"), dict):
        case_payload["response"] = _normalize_review_response_payload(case_payload["response"])
    return {
        "case": case_payload,
        "actions": store.list_actions(case["id"]),
        "events": store.list_events(case["id"]),
        "feedback": store.get_feedback(case["id"]),
    }


def _normalize_review_response_payload(response: dict[str, Any]) -> dict[str, Any]:
    """Present persisted results through the current citation contract.

    This is a read-time contract repair for cases created before citation
    metadata was introduced. It does not rewrite stored case JSON or create a
    second frontend rendering path; new runs already contain these fields.
    Missing legal metadata remains explicitly unknown.
    """

    if response.get("status") == "review_failed":
        return response

    normalized = dict(response)
    result = dict(normalized.get("review_result") or {})
    raw_groups = normalized.get("citation_groups") or result.get("applicable_evidence") or []
    groups: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
    refs_by_chunk: dict[str, str] = {}
    explicit_refs = {
        str(raw_citation.get("citation_ref"))
        for raw_group in raw_groups
        for raw_citation in (raw_group.get("citations") or [])
        if raw_citation.get("citation_ref")
    }
    used_refs: set[str] = set()
    next_ref = 1
    for raw_group in raw_groups:
        group = dict(raw_group)
        group_citations: list[dict[str, Any]] = []
        for raw_citation in group.get("citations") or []:
            citation = dict(raw_citation)
            citation_ref = citation.get("citation_ref")
            if not citation_ref or str(citation_ref) in used_refs:
                while f"法源-{next_ref:02d}" in explicit_refs or f"法源-{next_ref:02d}" in used_refs:
                    next_ref += 1
                citation_ref = f"法源-{next_ref:02d}"
                next_ref += 1
            citation_ref = str(citation_ref)
            used_refs.add(citation_ref)
            citation["citation_ref"] = citation_ref
            for field, default in (
                ("article_no", None), ("full_article_text", None),
                ("doc_type", "unknown"), ("authority", "unknown"),
                ("law_status", "unknown"), ("publish_date", None),
                ("effective_date", None), ("issuing_body", None),
                ("heading_path", []),
            ):
                citation.setdefault(field, default)
            if citation.get("chunk_id"):
                refs_by_chunk[str(citation["chunk_id"])] = citation_ref
            group_citations.append(citation)
            citations.append(citation)
        group["citations"] = group_citations
        groups.append(group)

    claims: list[dict[str, Any]] = []
    for raw_claim in result.get("claims") or []:
        claim = dict(raw_claim)
        refs = list(claim.get("supporting_citation_refs") or [])
        if not refs:
            refs = [
                refs_by_chunk[str(chunk_id)]
                for chunk_id in claim.get("supporting_chunk_ids") or []
                if str(chunk_id) in refs_by_chunk
            ]
        claim["supporting_citation_refs"] = refs
        claims.append(claim)

    conclusion = result.get("conclusion")
    if isinstance(conclusion, str) and claims:
        def add_ref(match: re.Match[str]) -> str:
            claim_index = int(match.group(1))
            refs = claims[claim_index].get("supporting_citation_refs") if claim_index < len(claims) else []
            if not refs or "data-citation-ref=" in match.group(0):
                return match.group(0)
            return match.group(0).replace(">", f' data-citation-ref="{refs[0]}">', 1)

        conclusion = re.sub(
            r'<sup\b(?=[^>]*data-claim-index="(\d+)")[^>]*>',
            add_ref,
            conclusion,
        )

    result["claims"] = claims
    result["citations"] = citations
    result["applicable_evidence"] = groups
    normalized["review_result"] = result
    normalized["citation_groups"] = groups
    return normalized


def _can_view(user: UserRecord, case: dict[str, Any]) -> bool:
    return user.role in {"reviewer", "admin"} or case["created_by"] == user.id


def _can_complete(case: dict[str, Any]) -> bool:
    response = case.get("response") or {}
    result = response.get("review_result") or {}
    self_check = response.get("evidence_self_check") or {}
    return bool(
        response
        and result.get("risk_level") != "insufficient_evidence"
        and self_check.get("status") not in {"insufficient", "needs_second_retrieval"}
        and not result.get("missing_information")
    )


def _run_review(app: FastAPI, case: dict[str, Any]) -> ReviewResponse:
    material = case["material_text"] + _intake_context(case.get("intake") or {})
    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir)
        created = create_review_case(
            question=case["question"],
            material_text=material,
            output_dir=output_dir,
            review_mode=case["review_mode"],
        )
        trace = run_service_retrieval(
            case_id=created.review_case.review_case_id,
            chunks_path=app.state.chunks_path,
            output_dir=output_dir,
            review_mode=case["review_mode"],
            rerank_mode=case["rerank_mode"],
            output_format="markdown",
        )
        results = read_review_results(output_dir / "review_results.jsonl")
        if not results:
            raise RuntimeError("review result was not generated")
        result = results[0]
        from law_agent.review.service import flatten_source_evidence_packets

        return ReviewResponse(
            review_case_id=created.review_case.review_case_id,
            trace_id=created.trace.trace_id,
            review_facts=result.review_facts,
            review_result=result,
            evidence_self_check=trace.evidence_self_check,
            citation_groups=result.applicable_evidence,
            second_retrieval_triggered=trace.evidence_self_check.second_retrieval_triggered,
            retrieval_queries=trace.queries,
            evidence_chunks=flatten_source_evidence_packets(trace.source_evidence_packets),
            source_evidence_packets=trace.source_evidence_packets,
        )


def create_app(
    *,
    chunks_path: Path | str = DEFAULT_CHUNKS_PATH,
    review_mode: ReviewMode = "llm",
    eval_cache_dir: Path | str | None = None,
    case_store: CaseStore | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        if not application.state.case_store_initialized:
            application.state.case_store.initialize()
            application.state.case_store_initialized = True
        yield

    app = FastAPI(
        title="CrossComply Case Workbench API",
        description="Persisted, evidence-grounded cross-border data compliance cases.",
        version="0.2.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:*", "http://127.0.0.1:*"],
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.chunks_path = Path(chunks_path)
    app.state.review_mode = review_mode
    app.state.case_store = case_store or PostgresCaseStore(load_service_config().postgres.dsn)
    app.state.case_store_initialized = False
    app.state.eval_cache: dict[str, EvalSummary | None] = {"off": None, "embedding": None}
    app.state.eval_jobs: dict[str, dict[str, Any]] = {"off": _idle_job(), "embedding": _idle_job()}
    app.state.eval_lock = threading.Lock()
    app.state.eval_cache_dir = Path(eval_cache_dir) if eval_cache_dir is not None else None
    if app.state.eval_cache_dir is not None:
        _preload_eval_cache(app, app.state.eval_cache_dir)

    def store() -> CaseStore:
        if not app.state.case_store_initialized:
            app.state.case_store.initialize()
            app.state.case_store_initialized = True
        return app.state.case_store

    async def current_user(request: Request) -> UserRecord:
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            raise HTTPException(status_code=401, detail="请先登录 CrossComply 工作台")
        user = store().get_user_by_session(token)
        if user is None:
            raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
        return user

    def reviewer_only(user: UserRecord) -> None:
        if user.role not in {"reviewer", "admin"}:
            raise HTTPException(status_code=403, detail="该操作需要合规审核权限")

    @app.get("/api/health", response_model=HealthResponse)
    async def health_check() -> HealthResponse:
        llm_config = load_llm_config()
        try:
            from law_agent.review.retrieval.service_backends import healthcheck

            services = healthcheck(load_service_config())
        except Exception as exc:  # noqa: BLE001
            services = {"elasticsearch": False, "postgres": False, "error": str(exc)}
        chunks = Path(app.state.chunks_path)
        status = "ok" if services.get("elasticsearch") and services.get("postgres") and llm_config.enabled else "degraded"
        return HealthResponse(
            status=status,
            llm={"configured": llm_config.enabled, "reachable": llm_config.enabled, "model": llm_config.model, "base_url": llm_config.base_url},
            services=services,
            corpus={
                "chunks_path": str(chunks),
                "chunks_file_exists": chunks.exists(),
                "indexed_count": {
                    "elasticsearch_docs": services.get("elasticsearch_docs", 0),
                    "pgvector_rows": services.get("pgvector_rows", 0),
                },
            },
        )

    @app.post("/api/auth/login")
    async def login(payload: LoginRequest) -> JSONResponse:
        user = store().authenticate(payload.username, payload.password)
        if user is None:
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        try:
            ttl_hours = max(1, int(os.getenv("CROSSCOMPLY_SESSION_TTL_HOURS", "12")))
            token, expires_at = store().create_session(user.id, ttl_hours)
        except Exception as exc:
            raise HTTPException(status_code=503, detail="案件数据库尚未完成初始化或未配置初始账号") from exc
        response = JSONResponse({"user": user.to_dict(), "expires_at": expires_at})
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=ttl_hours * 3600,
            httponly=True,
            samesite="lax",
            secure=os.getenv("CROSSCOMPLY_COOKIE_SECURE", "false").lower() == "true",
            path="/",
        )
        return response

    @app.post("/api/auth/logout")
    async def logout(request: Request) -> JSONResponse:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            store().delete_session(token)
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/api/auth/me")
    async def me(user: UserRecord = Depends(current_user)) -> dict[str, Any]:
        return {"user": user.to_dict()}

    @app.post("/api/cases")
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
        if file is None and question is None and "application/json" in request.headers.get("content-type", "").lower():
            try:
                payload = CaseCreateRequest.model_validate(await request.json())
            except ValidationError as exc:
                raise HTTPException(status_code=422, detail=exc.errors()) from exc
        else:
            if file is not None:
                material_text, material_source = await _material_from_upload(file)
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
        return _case_payload(store(), item)

    @app.get("/api/cases")
    async def list_cases(query: str | None = None, user: UserRecord = Depends(current_user)) -> dict[str, Any]:
        cases = store().list_cases(user, query)
        return {"items": [_case_summary(case) for case in cases], "total": len(cases)}

    @app.get("/api/cases/{identifier}")
    async def get_case(identifier: str, user: UserRecord = Depends(current_user)) -> dict[str, Any]:
        case = store().get_case(identifier)
        if case is None or not _can_view(user, case):
            raise HTTPException(status_code=404, detail="案件不存在或无权访问")
        return _case_payload(store(), case)

    @app.patch("/api/cases/{identifier}")
    async def update_case(identifier: str, payload: CaseUpdateRequest, user: UserRecord = Depends(current_user)) -> dict[str, Any]:
        case = store().get_case(identifier)
        if case is None or not _can_view(user, case):
            raise HTTPException(status_code=404, detail="案件不存在或无权访问")
        if user.role == "requester" and case["status"] not in {"draft", "needs_info"}:
            raise HTTPException(status_code=403, detail="当前案件状态不允许申请人编辑")
        values = payload.model_dump(exclude_unset=True, mode="json")
        if "intake" in values and values["intake"] is not None:
            values["intake_json"] = values.pop("intake")
        if user.role == "requester":
            values.pop("owner_id", None)
        updated = store().update_case(identifier, **values)
        store().add_event(identifier, user.id, event_type="case_updated", payload={"fields": list(values)})
        return _case_payload(store(), updated)

    @app.post("/api/cases/{identifier}/status")
    async def update_case_status(identifier: str, payload: CaseStatusRequest, user: UserRecord = Depends(current_user)) -> dict[str, Any]:
        case = store().get_case(identifier)
        if case is None or not _can_view(user, case):
            raise HTTPException(status_code=404, detail="案件不存在或无权访问")
        current = case["status"]
        if payload.status != current and payload.status not in CASE_TRANSITIONS.get(current, set()):
            raise HTTPException(status_code=409, detail=f"不能将案件从 {current} 变更为 {payload.status}")
        if user.role == "requester" and payload.status != "submitted":
            raise HTTPException(status_code=403, detail="申请人只能提交案件或补充材料")
        if payload.status in {"in_review", "completed", "review_failed"}:
            reviewer_only(user)
        if payload.status == "completed" and not _can_complete(case):
            raise HTTPException(status_code=409, detail="证据不足或仍有缺失信息，不能完成案件")
        status_values: dict[str, Any] = {"status": payload.status}
        if payload.status == "submitted":
            status_values["facts_confirmed"] = True
        updated = store().update_case(identifier, **status_values)
        store().add_event(identifier, user.id, event_type="status_changed", from_status=current, to_status=payload.status, payload={"note": payload.note})
        return _case_payload(store(), updated)

    @app.post("/api/cases/{identifier}/run")
    async def run_case(identifier: str, user: UserRecord = Depends(current_user)) -> dict[str, Any]:
        reviewer_only(user)
        case = store().get_case(identifier)
        if case is None:
            raise HTTPException(status_code=404, detail="案件不存在")
        if case["status"] not in {"submitted", "needs_info", "review_failed"}:
            raise HTTPException(status_code=409, detail="案件必须处于待审核、待补充或失败状态才能运行审查")
        previous_status = case["status"]
        store().update_case(identifier, status="in_review", owner_id=user.id)
        store().add_event(identifier, user.id, event_type="review_started", from_status=previous_status, to_status="in_review")
        try:
            result = _run_review(app, case)
        except ReviewWorkflowFailed as exc:
            failed = ReviewFailedResponse.model_validate(exc.to_response()).model_dump(mode="json")
            updated = store().update_case(identifier, status="review_failed", response_json=failed, trace_id=failed.get("trace_id"))
            store().add_event(identifier, user.id, event_type="review_failed", from_status="in_review", to_status="review_failed", payload={"failed_node": failed.get("failed_node")})
            return {**_case_payload(store(), updated), "run_status": "review_failed"}
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            updated = store().update_case(identifier, status="review_failed", response_json={"status": "review_failed", "message": str(exc)})
            store().add_event(identifier, user.id, event_type="review_failed", from_status="in_review", to_status="review_failed", payload={"message": str(exc)})
            return {**_case_payload(store(), updated), "run_status": "review_failed"}
        response_json = result.model_dump(mode="json")
        final_status: CaseStatus = "completed" if _can_complete({**case, "response": response_json}) else "needs_info"  # type: ignore[assignment]
        updated = store().update_case(
            identifier,
            status=final_status,
            risk_level=result.review_result.risk_level,
            trace_id=result.trace_id,
            response_json=response_json,
        )
        store().add_event(identifier, user.id, event_type="review_completed", from_status="in_review", to_status=final_status, payload={"risk_level": result.review_result.risk_level})
        for action in result.review_result.recommended_actions:
            if not any(existing["title"] == action for existing in store().list_actions(identifier)):
                created_action = store().create_action(
                    identifier,
                    title=action,
                    description="由审查报告生成，请审核人确认后分配负责人。",
                )
                store().add_event(
                    identifier,
                    user.id,
                    event_type="action_created",
                    payload={"action_id": created_action["id"], "source": "review"},
                )
        updated = store().get_case(identifier) or updated
        return {**_case_payload(store(), updated), "run_status": final_status}

    @app.post("/api/cases/{identifier}/actions")
    async def create_action(identifier: str, payload: CaseActionRequest, user: UserRecord = Depends(current_user)) -> dict[str, Any]:
        reviewer_only(user)
        case = store().get_case(identifier)
        if case is None:
            raise HTTPException(status_code=404, detail="案件不存在")
        action = store().create_action(identifier, **payload.model_dump(mode="json"))
        store().add_event(identifier, user.id, event_type="action_created", payload={"action_id": action["id"]})
        return action

    @app.patch("/api/actions/{identifier}")
    async def update_action(identifier: str, payload: CaseActionUpdateRequest, user: UserRecord = Depends(current_user)) -> dict[str, Any]:
        reviewer_only(user)
        try:
            action = store().update_action(identifier, **payload.model_dump(exclude_unset=True, mode="json"))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="整改动作不存在") from exc
        case = store().get_case(action["case_id"])
        if case is not None:
            store().add_event(
                action["case_id"],
                user.id,
                event_type="action_updated",
                payload={"action_id": action["id"], "status": action["status"]},
            )
        return action

    @app.post("/api/cases/{identifier}/feedback")
    async def save_feedback(identifier: str, payload: FeedbackRequest, user: UserRecord = Depends(current_user)) -> dict[str, Any]:
        case = store().get_case(identifier)
        if case is None or not _can_view(user, case):
            raise HTTPException(status_code=404, detail="案件不存在或无权访问")
        feedback = store().save_feedback(identifier, user.id, **payload.model_dump(mode="json"))
        store().add_event(identifier, user.id, event_type="feedback_saved")
        return feedback

    @app.get("/api/cases/{identifier}/events")
    async def get_events(identifier: str, user: UserRecord = Depends(current_user)) -> dict[str, Any]:
        case = store().get_case(identifier)
        if case is None or not _can_view(user, case):
            raise HTTPException(status_code=404, detail="案件不存在或无权访问")
        return {"items": store().list_events(identifier)}

    @app.get("/api/dashboard/summary")
    async def dashboard_summary(user: UserRecord = Depends(current_user)) -> dict[str, Any]:
        return store().dashboard_summary(user)

    @app.get("/api/eval/latest")
    async def get_latest_eval(rerank_mode: RerankMode = "off", user: UserRecord = Depends(current_user)) -> JSONResponse:
        reviewer_only(user)
        if app.state.eval_cache_dir is not None:
            _preload_eval_cache(app, app.state.eval_cache_dir)
        cached = app.state.eval_cache.get(rerank_mode)
        if cached is None:
            raise HTTPException(status_code=404, detail=f"no evaluation has been run for rerank_mode={rerank_mode}")
        return JSONResponse(content=cached.model_dump())

    @app.post("/api/eval/run")
    async def trigger_eval(request: EvalRunRequest | None = None, user: UserRecord = Depends(current_user)) -> EvalJobResponse:
        if user.role != "admin":
            raise HTTPException(status_code=403, detail="只有管理员可以运行评测")
        chunks = Path(request.chunks_path) if request and request.chunks_path else app.state.chunks_path
        review_mode_value = request.review_mode if request else "llm"
        top_k = request.top_k if request else 10
        max_workers = request.max_workers if request else 4
        rerank_mode_value = request.rerank_mode if request else "off"
        suite = request.suite if request else "full"
        with app.state.eval_lock:
            job = app.state.eval_jobs[rerank_mode_value]
            if job["status"] == "running":
                return EvalJobResponse.model_validate(job)
            job_id = uuid.uuid4().hex
            app.state.eval_jobs[rerank_mode_value] = {
                "job_id": job_id, "status": "running", "message": None,
                "started_at": _now_iso(), "finished_at": None,
            }
        thread = threading.Thread(
            target=_run_eval_job,
            args=(app, job_id, chunks, review_mode_value, top_k, max_workers, rerank_mode_value, suite),
            daemon=True,
        )
        thread.start()
        return EvalJobResponse.model_validate(app.state.eval_jobs[rerank_mode_value])

    @app.get("/api/eval/status")
    async def get_eval_status(rerank_mode: RerankMode = "off", user: UserRecord = Depends(current_user)) -> EvalJobResponse:
        reviewer_only(user)
        return EvalJobResponse.model_validate(app.state.eval_jobs.get(rerank_mode, _idle_job()))

    return app


def _run_eval_job(
    app: FastAPI,
    job_id: str,
    chunks: Path,
    review_mode: ReviewEvalMode,
    top_k: int,
    max_workers: int,
    rerank_mode: RerankMode,
    suite: EvalSuite,
) -> None:
    try:
        summary = run_evaluation(
            chunks_path=chunks,
            review_mode=review_mode,
            top_k=top_k,
            rerank_mode=rerank_mode,
            max_workers=max_workers,
            suite=suite,
        )
    except Exception as exc:  # noqa: BLE001
        with app.state.eval_lock:
            job = app.state.eval_jobs.get(rerank_mode, _idle_job())
            if job.get("job_id") == job_id:
                app.state.eval_jobs[rerank_mode] = {**job, "status": "failed", "message": str(exc), "finished_at": _now_iso()}
        return
    with app.state.eval_lock:
        job = app.state.eval_jobs.get(rerank_mode, _idle_job())
        if job.get("job_id") != job_id:
            return
        app.state.eval_cache[rerank_mode] = summary
        app.state.eval_jobs[rerank_mode] = {**job, "status": "succeeded", "message": None, "finished_at": _now_iso()}


app = create_app(eval_cache_dir=Path("data/review_runs"))
