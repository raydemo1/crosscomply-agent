"""FastAPI transport for the CrossComply compliance case workbench.

The public API is case-oriented.  Review execution remains in the existing
RAG/application services, while this module owns authentication, persistence,
workflow transitions, and the HTTP contract used by the frontend.
"""

# FastAPI intentionally declares dependency providers in endpoint defaults.
# ruff: noqa: B008

from __future__ import annotations

import hashlib
import html
import os
import re
import tempfile
import threading
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError, field_validator

from law_agent.config import RerankMode, load_llm_config, load_service_config
from law_agent.review.case_store import CaseStore, InMemoryCaseStore, PostgresCaseStore, UserRecord
from law_agent.review.enterprise_store import InMemoryEnterpriseStore, PostgresEnterpriseStore
from law_agent.review.evalset.cases import EvalSuite
from law_agent.review.evalset.runner import ReviewEvalMode, run_evaluation
from law_agent.review.evalset.schemas import EvalSummary
from law_agent.review.feishu import (
    FeishuApprovalClient,
    FeishuApprovalConfig,
    FeishuEventError,
    apply_authoritative_decision,
    decode_event_body,
    parse_approval_event,
)
from law_agent.review.governance_store import (
    InMemoryGovernanceStore,
    PostgresGovernanceStore,
)
from law_agent.review.io import read_review_results
from law_agent.review.object_store import MaterialObjectStore, material_object_store_from_env
from law_agent.review.reports import DecisionReportData, LegalSource, generate_decision_report
from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH
from law_agent.review.rules import ComplianceFacts, evaluate_national_path
from law_agent.review.schemas import (
    CitationGroup,
    EvidenceSelfCheck,
    RetrievalHit,
    RetrievalQuery,
    ReviewFacts,
    ReviewResult,
    SourceEvidencePacket,
)
from law_agent.review.service import ReviewMode, create_review_case, run_service_retrieval
from law_agent.review.user_admin import (
    PostgresUserAdminStore,
    UserAdminError,
    UserAdminStore,
    UserRole,
)
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
}
SESSION_COOKIE = "crosscomply_session"


class IntakePayload(BaseModel):
    business_activity: str = ""
    data_types: list[str] = Field(default_factory=list)
    sensitive_personal_info: bool | None = None
    cross_border_transfer: bool | None = None
    important_data_status: Literal["unknown", "not_important", "important", "under_review"] = (
        "unknown"
    )
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
    status: Literal[
        "draft",
        "needs_info",
        "pending_review",
        "review_running",
        "pending_feishu_approval",
        "approved",
        "conditionally_approved",
        "rejected",
        "run_failed",
    ]
    note: str = ""


class CaseActionRequest(BaseModel):
    title: str = Field(..., min_length=1)
    description: str = ""
    owner_role: str = "reviewer"
    priority: Literal["high", "medium", "low"] = "medium"
    status: Literal["open", "in_progress", "completed"] = "open"
    due_date: str | None = None


class MaterialSnapshotRequest(BaseModel):
    version_ids: list[str] = Field(..., min_length=1)
    facts: ComplianceFacts


class UserCreateRequest(BaseModel):
    username: str = Field(..., min_length=1)
    display_name: str = Field(..., min_length=1)
    password: str = Field(..., min_length=12)
    role: UserRole


class UserStateRequest(BaseModel):
    active: bool


class UserPasswordResetRequest(BaseModel):
    password: str = Field(..., min_length=12)


class UserRoleRequest(BaseModel):
    role: UserRole


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
        "could not load document" in lower
        or "data format error" in lower
        or "conversion failed" in lower
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
        raise HTTPException(
            status_code=422, detail={"code": "unsupported_file_type", "filename": filename}
        )
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=422, detail={"code": "empty_file", "filename": filename})
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=422, detail={"code": "file_too_large", "filename": filename}
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
            status_code=422, detail={"code": "empty_extraction", "filename": filename}
        )
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
            app.state.eval_cache[arm] = EvalSummary.model_validate_json(
                latest.read_text(encoding="utf-8")
            )
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
                while (
                    f"法源-{next_ref:02d}" in explicit_refs or f"法源-{next_ref:02d}" in used_refs
                ):
                    next_ref += 1
                citation_ref = f"法源-{next_ref:02d}"
                next_ref += 1
            citation_ref = str(citation_ref)
            used_refs.add(citation_ref)
            citation["citation_ref"] = citation_ref
            for field, default in (
                ("article_no", None),
                ("full_article_text", None),
                ("doc_type", "unknown"),
                ("authority", "unknown"),
                ("law_status", "unknown"),
                ("publish_date", None),
                ("effective_date", None),
                ("issuing_body", None),
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
            refs = (
                claims[claim_index].get("supporting_citation_refs")
                if claim_index < len(claims)
                else []
            )
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


def _compact_approval_text(value: Any, *, limit: int) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", "", str(value or "")))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1].rstrip()}…"


def _candidate_path_labels(determination: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for item in determination.get("candidate_paths") or []:
        if isinstance(item, str):
            label = item
        elif isinstance(item, dict):
            label = next(
                (
                    str(item[key])
                    for key in ("label", "name", "path", "code")
                    if item.get(key)
                ),
                "",
            )
        else:
            label = ""
        label = _compact_approval_text(label, limit=80)
        if label and label not in labels:
            labels.append(label)
    return labels[:2]


def _feishu_approval_form(
    *,
    case: dict[str, Any],
    task: Any,
    rule_determination: dict[str, Any],
    actions: list[dict[str, Any]],
    public_base_url: str,
) -> list[dict[str, str]]:
    result = dict(task.result or {})
    review_result = dict(result.get("review_result") or {})
    risk_labels = {
        "high": "高",
        "medium": "中",
        "low": "低",
        "insufficient_evidence": "待核验",
    }
    risk = risk_labels.get(str(review_result.get("risk_level", "")).lower(), "待核验")
    paths = _candidate_path_labels(rule_determination)
    conclusion = _compact_approval_text(review_result.get("conclusion"), limit=360)
    summary_parts = [f"风险：{risk}"]
    if paths:
        summary_parts.append(f"候选路径：{'、'.join(paths)}")
    if conclusion:
        summary_parts.append(f"AI审查：{conclusion}")

    action_titles = [
        _compact_approval_text(item, limit=100)
        for item in review_result.get("recommended_actions") or []
    ]
    action_titles.extend(
        _compact_approval_text(action.get("title"), limit=100) for action in actions
    )
    unique_actions = list(dict.fromkeys(item for item in action_titles if item))[:3]
    action_summary = "；".join(unique_actions) or "无待办整改项"
    case_url = f"{public_base_url.rstrip('/')}/?case={quote(case['id'], safe='')}"
    return [
        {"id": "case_number", "type": "input", "value": case["case_number"]},
        {"id": "title", "type": "input", "value": case["title"]},
        {
            "id": "decision_summary",
            "type": "input",
            "value": _compact_approval_text("｜".join(summary_parts), limit=500),
        },
        {
            "id": "key_actions",
            "type": "input",
            "value": _compact_approval_text(action_summary, limit=300),
        },
        {"id": "case_url", "type": "input", "value": case_url},
        {"id": "task_id", "type": "input", "value": task.id},
    ]


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
    enterprise_store: InMemoryEnterpriseStore | PostgresEnterpriseStore | None = None,
    material_object_store: MaterialObjectStore | None = None,
    user_admin_store: UserAdminStore | None = None,
    governance_store: InMemoryGovernanceStore | PostgresGovernanceStore | None = None,
    feishu_client: FeishuApprovalClient | None = None,
    feishu_config: FeishuApprovalConfig | None = None,
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
    app.state.enterprise_store = enterprise_store or PostgresEnterpriseStore(
        load_service_config().postgres.dsn
    )
    app.state.material_object_store = material_object_store
    app.state.user_admin_store = user_admin_store or PostgresUserAdminStore(
        load_service_config().postgres.dsn
    )
    if governance_store is None and isinstance(case_store, InMemoryCaseStore):
        governance_store = InMemoryGovernanceStore()
    app.state.governance_store = governance_store or PostgresGovernanceStore(
        load_service_config().postgres.dsn
    )
    app.state.feishu_client = feishu_client
    app.state.feishu_config = feishu_config
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

    def enterprise() -> InMemoryEnterpriseStore | PostgresEnterpriseStore:
        return app.state.enterprise_store

    def originals() -> MaterialObjectStore:
        if app.state.material_object_store is None:
            try:
                app.state.material_object_store = material_object_store_from_env()
            except (ImportError, RuntimeError) as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
        return app.state.material_object_store

    def user_admin() -> UserAdminStore:
        return app.state.user_admin_store

    def governance() -> InMemoryGovernanceStore | PostgresGovernanceStore:
        return app.state.governance_store

    def configured_feishu() -> tuple[FeishuApprovalClient, FeishuApprovalConfig]:
        config = app.state.feishu_config or FeishuApprovalConfig(
            app_id=os.getenv("CROSSCOMPLY_FEISHU_APP_ID", ""),
            app_secret=os.getenv("CROSSCOMPLY_FEISHU_APP_SECRET", ""),
            approval_code=os.getenv("CROSSCOMPLY_FEISHU_APPROVAL_CODE", ""),
            initiator_open_id=os.getenv("CROSSCOMPLY_FEISHU_INITIATOR_OPEN_ID", ""),
            verification_token=os.getenv("CROSSCOMPLY_FEISHU_VERIFICATION_TOKEN", ""),
            encrypt_key=os.getenv("CROSSCOMPLY_FEISHU_ENCRYPT_KEY", ""),
            public_base_url=os.getenv("CROSSCOMPLY_PUBLIC_BASE_URL", ""),
        )
        if not all(
            (
                config.app_id,
                config.app_secret,
                config.approval_code,
                config.initiator_open_id,
                config.verification_token,
                config.encrypt_key,
                config.public_base_url,
            )
        ):
            raise HTTPException(status_code=503, detail="飞书审批配置不完整")
        client = app.state.feishu_client
        if client is None:
            import httpx

            client = FeishuApprovalClient(config, httpx.request)
            app.state.feishu_client = client
        return client, config

    def deliver_feishu_approval(
        *, case: dict[str, Any], task: Any, user: UserRecord, delivery: Any
    ) -> dict[str, Any]:
        client, config = configured_feishu()
        running = governance().begin_approval_attempt(delivery.id)
        try:
            rule = enterprise().get_rule_snapshot(task.rule_snapshot_id)
            if rule is None:
                raise RuntimeError("审批任务绑定的规则快照不存在")
            instance = client.create_instance(
                open_id=config.initiator_open_id,
                idempotency_key=running.idempotency_key,
                form=_feishu_approval_form(
                    case=case,
                    task=task,
                    rule_determination=dict(rule.determination),
                    actions=store().list_actions(case["id"]),
                    public_base_url=config.public_base_url,
                ),
            )
        except Exception as exc:
            failed = governance().fail_approval_delivery(
                running.id, error_message=str(exc) or exc.__class__.__name__
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "feishu_delivery_failed",
                    "delivery_id": failed.id,
                    "attempt_count": failed.attempt_count,
                    "message": failed.error_message,
                },
            ) from exc
        approval = governance().create_approval_record(
            case_id=case["id"],
            task_id=task.id,
            instance_id=instance.instance_id,
            payload={
                "request_id": instance.request_id,
                "idempotency_key": running.idempotency_key,
            },
        )
        governance().succeed_approval_delivery(running.id, instance_id=instance.instance_id)
        store().add_event(
            case["id"],
            user.id,
            event_type="feishu_approval_created",
            from_status="pending_feishu_approval",
            to_status="pending_feishu_approval",
            payload={
                "approval_id": approval.id,
                "instance_id": approval.instance_id,
                "delivery_id": delivery.id,
            },
        )
        return asdict(approval)

    def case_payload(case: dict[str, Any]) -> dict[str, Any]:
        payload = _case_payload(store(), case)
        snapshot = enterprise().get_latest_material_snapshot(case["id"])
        rule = (
            enterprise().get_latest_rule_snapshot(
                case_id=case["id"], material_snapshot_id=snapshot.id
            )
            if snapshot is not None
            else None
        )
        task = enterprise().get_latest_task(case["id"])
        approval = governance().get_latest_approval(case["id"])
        report = governance().get_latest_report(case["id"])
        payload.update(
            {
                "material_snapshot": asdict(snapshot) if snapshot else None,
                "rule_decision": asdict(rule) if rule else None,
                "review_task": asdict(task) if task else None,
                "feishu_approval": asdict(approval) if approval else None,
                "signed_decision": (
                    asdict(approval) if approval and approval.status != "pending" else None
                ),
                "report": asdict(report) if report else None,
            }
        )
        return payload

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

    def admin_only(user: UserRecord) -> None:
        if user.role != "admin":
            raise HTTPException(status_code=403, detail="该操作需要管理员权限")

    @app.get("/api/health", response_model=HealthResponse)
    async def health_check() -> HealthResponse:
        llm_config = load_llm_config()
        try:
            from law_agent.review.retrieval.service_backends import healthcheck

            services = healthcheck(load_service_config())
        except Exception as exc:  # noqa: BLE001
            services = {"elasticsearch": False, "postgres": False, "error": str(exc)}
        chunks = Path(app.state.chunks_path)
        status = (
            "ok"
            if services.get("elasticsearch") and services.get("postgres") and llm_config.enabled
            else "degraded"
        )
        return HealthResponse(
            status=status,
            llm={
                "configured": llm_config.enabled,
                "reachable": llm_config.enabled,
                "model": llm_config.model,
                "base_url": llm_config.base_url,
            },
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
            raise HTTPException(
                status_code=503, detail="案件数据库尚未完成初始化或未配置初始账号"
            ) from exc
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

    @app.get("/api/admin/users")
    async def list_managed_users(
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        admin_only(user)
        items = user_admin().list_users()
        return {"items": [asdict(item) for item in items], "total": len(items)}

    @app.post("/api/admin/users")
    async def create_managed_user(
        payload: UserCreateRequest,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        admin_only(user)
        try:
            created = user_admin().create_user(
                payload.username,
                payload.display_name,
                payload.password,
                payload.role,
            )
        except UserAdminError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return asdict(created)

    @app.patch("/api/admin/users/{user_id}/state")
    async def set_managed_user_state(
        user_id: str,
        payload: UserStateRequest,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        admin_only(user)
        try:
            updated = user_admin().set_active(user_id, payload.active)
        except UserAdminError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return asdict(updated)

    @app.post("/api/admin/users/{user_id}/reset-password")
    async def reset_managed_user_password(
        user_id: str,
        payload: UserPasswordResetRequest,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        admin_only(user)
        try:
            updated = user_admin().reset_password(user_id, payload.password)
        except UserAdminError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return asdict(updated)

    @app.patch("/api/admin/users/{user_id}/role")
    async def assign_managed_user_role(
        user_id: str,
        payload: UserRoleRequest,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        admin_only(user)
        try:
            updated = user_admin().assign_role(user_id, payload.role)
        except UserAdminError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return asdict(updated)

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
                material_text, material_source = await _material_from_upload(file)
            if not question or not material_text.strip():
                raise HTTPException(
                    status_code=422, detail="question and material_text are required"
                )
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

    @app.post("/api/cases/{identifier}/materials")
    async def upload_material(
        identifier: str,
        logical_name: str = Form(...),
        file: UploadFile = File(...),
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        case = store().get_case(identifier)
        if case is None or not _can_view(user, case):
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
                # The original remains available even when generic parsing fails.
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

    @app.get("/api/cases/{identifier}/materials")
    async def list_materials(
        identifier: str, user: UserRecord = Depends(current_user)
    ) -> dict[str, Any]:
        case = store().get_case(identifier)
        if case is None or not _can_view(user, case):
            raise HTTPException(status_code=404, detail="案件不存在或无权访问")
        items = enterprise().list_material_versions(identifier)
        return {"items": [asdict(item) for item in items], "total": len(items)}

    @app.get("/api/materials/{version_id}/download")
    async def download_material(
        version_id: str, user: UserRecord = Depends(current_user)
    ) -> Response:
        version = enterprise().get_material_version(version_id)
        if version is None:
            raise HTTPException(status_code=404, detail="材料版本不存在")
        case = store().get_case(version.case_id)
        if case is None or not _can_view(user, case):
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

    @app.post("/api/cases/{identifier}/material-snapshots")
    async def freeze_material_snapshot(
        identifier: str,
        payload: MaterialSnapshotRequest,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        case = store().get_case(identifier)
        if case is None or not _can_view(user, case):
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

    @app.get("/api/cases")
    async def list_cases(
        query: str | None = None, user: UserRecord = Depends(current_user)
    ) -> dict[str, Any]:
        cases = store().list_cases(user, query)
        return {"items": [_case_summary(case) for case in cases], "total": len(cases)}

    @app.get("/api/cases/{identifier}")
    async def get_case(identifier: str, user: UserRecord = Depends(current_user)) -> dict[str, Any]:
        case = store().get_case(identifier)
        if case is None or not _can_view(user, case):
            raise HTTPException(status_code=404, detail="案件不存在或无权访问")
        return case_payload(case)

    @app.patch("/api/cases/{identifier}")
    async def update_case(
        identifier: str, payload: CaseUpdateRequest, user: UserRecord = Depends(current_user)
    ) -> dict[str, Any]:
        case = store().get_case(identifier)
        if case is None or not _can_view(user, case):
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
            identifier, user.id, event_type="case_updated", payload={"fields": list(values)}
        )
        return case_payload(updated)

    @app.post("/api/cases/{identifier}/status")
    async def update_case_status(
        identifier: str, payload: CaseStatusRequest, user: UserRecord = Depends(current_user)
    ) -> dict[str, Any]:
        case = store().get_case(identifier)
        if case is None or not _can_view(user, case):
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
                    case_id=identifier, material_snapshot_id=snapshot.id
                )
                if snapshot is not None
                else None
            )
            if snapshot is None or rule is None:
                raise HTTPException(status_code=409, detail="提交前必须冻结材料并完成规则判定")
            if rule.determination.get("needs_info"):
                raise HTTPException(status_code=409, detail="仍有关键事实缺失，不得提交审查")
        try:
            validate_case_transition(
                current=current,
                target=payload.status,
                authority="local",
            )
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

    @app.post("/api/cases/{identifier}/run")
    async def run_case(identifier: str, user: UserRecord = Depends(current_user)) -> JSONResponse:
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
                "deployment": os.getenv("CROSSCOMPLY_MODEL_BOUNDARY", "enterprise-approved-api"),
            },
        )
        validate_case_transition(
            current="pending_review", target="review_running", authority="local"
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

    @app.get("/api/tasks/{task_id}")
    async def get_review_task(
        task_id: str, user: UserRecord = Depends(current_user)
    ) -> dict[str, Any]:
        task = enterprise().get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="审查任务不存在")
        case = store().get_case(task.case_id)
        if case is None or not _can_view(user, case):
            raise HTTPException(status_code=404, detail="审查任务不存在或无权访问")
        return asdict(task)

    @app.post("/api/tasks/{task_id}/retry")
    async def retry_review_task(
        task_id: str, user: UserRecord = Depends(current_user)
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
                case_id=task.case_id, material_snapshot_id=latest_snapshot.id
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

    @app.post("/api/cases/{identifier}/feishu-approval")
    async def create_feishu_approval(
        identifier: str, user: UserRecord = Depends(current_user)
    ) -> dict[str, Any]:
        reviewer_only(user)
        case = store().get_case(identifier)
        if case is None:
            raise HTTPException(status_code=404, detail="案件不存在")
        if case["status"] != "pending_feishu_approval":
            raise HTTPException(status_code=409, detail="案件尚未完成审查，不能发起飞书审批")
        task = enterprise().get_latest_task(identifier)
        if task is None or task.status != "succeeded":
            raise HTTPException(status_code=409, detail="案件没有已完成的审查任务")
        existing = governance().get_latest_approval(identifier)
        if existing is not None:
            return asdict(existing)
        governance().requeue_expired_approval_deliveries(target_status="failed")
        idempotency_key = f"{identifier}:{task.id}"
        delivery = governance().enqueue_approval_delivery(
            case_id=identifier,
            task_id=task.id,
            idempotency_key=idempotency_key,
        )
        if delivery.status == "running":
            raise HTTPException(status_code=409, detail="飞书审批投递正在处理中，请稍后重试")
        if delivery.status == "failed":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "feishu_delivery_requires_retry",
                    "delivery_id": delivery.id,
                    "message": delivery.error_message,
                },
            )
        if delivery.status == "succeeded" and delivery.instance_id:
            approval = governance().get_approval_by_instance(delivery.instance_id)
            if approval is not None:
                return asdict(approval)
        return deliver_feishu_approval(case=case, task=task, user=user, delivery=delivery)

    @app.post("/api/approval-deliveries/{delivery_id}/retry")
    async def retry_feishu_approval_delivery(
        delivery_id: str, user: UserRecord = Depends(current_user)
    ) -> dict[str, Any]:
        reviewer_only(user)
        governance().requeue_expired_approval_deliveries(target_status="failed")
        delivery = governance().get_approval_delivery(delivery_id)
        if delivery is None:
            raise HTTPException(status_code=404, detail="飞书审批投递任务不存在")
        case = store().get_case(delivery.case_id)
        task = enterprise().get_task(delivery.task_id)
        if case is None or task is None:
            raise HTTPException(status_code=409, detail="投递任务绑定的案件或审查任务不存在")
        try:
            queued = governance().retry_approval_delivery(delivery_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return deliver_feishu_approval(case=case, task=task, user=user, delivery=queued)

    @app.post("/api/integrations/feishu/approval-events")
    async def receive_feishu_approval_event(request: Request) -> dict[str, Any]:
        _, config = configured_feishu()
        body = await request.body()
        try:
            decoded = decode_event_body(body=body, encrypt_key=config.encrypt_key)
        except FeishuEventError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if decoded.get("type") == "url_verification":
            if decoded.get("token") != config.verification_token:
                raise HTTPException(status_code=401, detail="飞书 verification token 不匹配")
            challenge = decoded.get("challenge")
            if not isinstance(challenge, str) or not challenge:
                raise HTTPException(status_code=400, detail="飞书 URL 校验缺少 challenge")
            return {"challenge": challenge}
        try:
            event = parse_approval_event(
                body=body,
                timestamp=request.headers.get("x-lark-request-timestamp", ""),
                nonce=request.headers.get("x-lark-request-nonce", ""),
                signature=request.headers.get("x-lark-signature", ""),
                config=config,
            )
        except FeishuEventError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        if event.decision is None:
            return {"ok": True, "ignored": True, "status": event.raw_status}
        approval = governance().get_approval_by_instance(event.instance_id)
        if approval is None:
            raise HTTPException(status_code=404, detail="飞书审批实例未绑定 CrossComply 案件")
        receipt = governance().record_approval_event(
            approval_id=approval.id,
            provider_event_id=event.idempotency_key,
            event_type=event.event_type,
            signature_valid=True,
            payload=dict(event.payload),
            target_status=event.decision,
            approver_name=event.approver_id,
            decided_at=event.approval_time,
        )
        case = store().get_case(approval.case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="审批绑定的案件不存在")
        try:
            target = apply_authoritative_decision(
                current_status=case["status"], decision=event.decision
            )
        except FeishuEventError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if case["status"] != target:
            store().update_case(approval.case_id, status=target)
            store().add_event(
                approval.case_id,
                case.get("owner_id") or case["created_by"],
                event_type="feishu_decision_written_back",
                from_status=case["status"],
                to_status=target,
                payload={"approval_event_id": receipt.event.id},
            )
        return {"ok": True, "duplicate": receipt.duplicate, "case_status": target}

    @app.post("/api/integrations/feishu/subscribe")
    async def subscribe_feishu_approval_events(
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        admin_only(user)
        client, config = configured_feishu()
        try:
            client.subscribe_approval_events()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc) or exc.__class__.__name__) from exc
        return {"ok": True, "approval_code": config.approval_code}

    @app.post("/api/cases/{identifier}/reports")
    async def create_decision_report(
        identifier: str, user: UserRecord = Depends(current_user)
    ) -> dict[str, Any]:
        reviewer_only(user)
        case = store().get_case(identifier)
        if case is None:
            raise HTTPException(status_code=404, detail="案件不存在")
        approval = governance().get_latest_approval(identifier)
        if approval is None or approval.status == "pending":
            raise HTTPException(status_code=409, detail="飞书尚未产生最终审批决定")
        existing = governance().get_latest_report(identifier)
        if existing is not None:
            return asdict(existing)
        approved_task = enterprise().get_task(approval.task_id)
        if approved_task is None:
            raise HTTPException(status_code=409, detail="审批记录绑定的审查任务不存在")
        snapshot = enterprise().get_material_snapshot(approved_task.material_snapshot_id)
        rule = enterprise().get_rule_snapshot(approved_task.rule_snapshot_id)
        if snapshot is None or rule is None:
            raise HTTPException(status_code=409, detail="审批任务绑定的材料或规则快照不存在")
        versions = [
            enterprise().get_material_version(version_id) for version_id in snapshot.version_ids
        ]
        sources = tuple(
            LegalSource(
                title=str(item.get("title", "")),
                locator=str(item.get("source_url") or item.get("article") or ""),
            )
            for item in rule.determination.get("official_bases", [])
        )
        report_data = DecisionReportData(
            case_number=case["case_number"],
            decision=approval.status,
            material_hashes=tuple(item.sha256 for item in versions if item is not None),
            rule_version=rule.ruleset_version,
            legal_sources=sources,
            remediation_items=tuple(item["title"] for item in store().list_actions(identifier)),
            approver=approval.approver_name or "Feishu approval",
            approved_at=approval.decided_at or approval.updated_at,
        )
        artifact = generate_decision_report(report_data)
        stored = originals().put_original(
            case_id=identifier,
            logical_name="decision-report",
            filename=f"{case['case_number']}.pdf",
            content_type="application/pdf",
            content=artifact.pdf_bytes,
        )
        report = governance().create_report_record(
            case_id=identifier,
            approval_id=approval.id,
            object_key=stored.object_key,
            sha256=artifact.sha256,
            metadata={
                "case_number": case["case_number"],
                "rule_version": rule.ruleset_version,
                "material_snapshot_id": snapshot.id,
            },
        )
        return asdict(report)

    @app.get("/api/reports/{report_id}/download")
    async def download_decision_report(
        report_id: str, user: UserRecord = Depends(current_user)
    ) -> Response:
        report = governance().get_report(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="决策报告不存在")
        case = store().get_case(report.case_id)
        if case is None or not _can_view(user, case):
            raise HTTPException(status_code=404, detail="决策报告不存在或无权访问")
        content = originals().get_original(report.object_key)
        if hashlib.sha256(content).hexdigest() != report.sha256:
            raise HTTPException(status_code=409, detail="决策报告哈希校验失败")
        return Response(content=content, media_type="application/pdf")

    @app.post("/api/cases/{identifier}/actions")
    async def create_action(
        identifier: str, payload: CaseActionRequest, user: UserRecord = Depends(current_user)
    ) -> dict[str, Any]:
        reviewer_only(user)
        case = store().get_case(identifier)
        if case is None:
            raise HTTPException(status_code=404, detail="案件不存在")
        action = store().create_action(identifier, **payload.model_dump(mode="json"))
        store().add_event(
            identifier, user.id, event_type="action_created", payload={"action_id": action["id"]}
        )
        return action

    @app.patch("/api/actions/{identifier}")
    async def update_action(
        identifier: str, payload: CaseActionUpdateRequest, user: UserRecord = Depends(current_user)
    ) -> dict[str, Any]:
        reviewer_only(user)
        try:
            action = store().update_action(
                identifier, **payload.model_dump(exclude_unset=True, mode="json")
            )
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
    async def save_feedback(
        identifier: str, payload: FeedbackRequest, user: UserRecord = Depends(current_user)
    ) -> dict[str, Any]:
        case = store().get_case(identifier)
        if case is None or not _can_view(user, case):
            raise HTTPException(status_code=404, detail="案件不存在或无权访问")
        feedback = store().save_feedback(identifier, user.id, **payload.model_dump(mode="json"))
        store().add_event(identifier, user.id, event_type="feedback_saved")
        return feedback

    @app.get("/api/cases/{identifier}/events")
    async def get_events(
        identifier: str, user: UserRecord = Depends(current_user)
    ) -> dict[str, Any]:
        case = store().get_case(identifier)
        if case is None or not _can_view(user, case):
            raise HTTPException(status_code=404, detail="案件不存在或无权访问")
        return {"items": store().list_events(identifier)}

    @app.get("/api/dashboard/summary")
    async def dashboard_summary(user: UserRecord = Depends(current_user)) -> dict[str, Any]:
        return store().dashboard_summary(user)

    @app.get("/api/eval/latest")
    async def get_latest_eval(
        rerank_mode: RerankMode = "off", user: UserRecord = Depends(current_user)
    ) -> JSONResponse:
        admin_only(user)
        if app.state.eval_cache_dir is not None:
            _preload_eval_cache(app, app.state.eval_cache_dir)
        cached = app.state.eval_cache.get(rerank_mode)
        if cached is None:
            raise HTTPException(
                status_code=404, detail=f"no evaluation has been run for rerank_mode={rerank_mode}"
            )
        return JSONResponse(content=cached.model_dump())

    @app.post("/api/eval/run")
    async def trigger_eval(
        request: EvalRunRequest | None = None, user: UserRecord = Depends(current_user)
    ) -> EvalJobResponse:
        if user.role != "admin":
            raise HTTPException(status_code=403, detail="只有管理员可以运行评测")
        chunks = (
            Path(request.chunks_path) if request and request.chunks_path else app.state.chunks_path
        )
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
                "job_id": job_id,
                "status": "running",
                "message": None,
                "started_at": _now_iso(),
                "finished_at": None,
            }
        thread = threading.Thread(
            target=_run_eval_job,
            args=(
                app,
                job_id,
                chunks,
                review_mode_value,
                top_k,
                max_workers,
                rerank_mode_value,
                suite,
            ),
            daemon=True,
        )
        thread.start()
        return EvalJobResponse.model_validate(app.state.eval_jobs[rerank_mode_value])

    @app.get("/api/eval/status")
    async def get_eval_status(
        rerank_mode: RerankMode = "off", user: UserRecord = Depends(current_user)
    ) -> EvalJobResponse:
        admin_only(user)
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
                app.state.eval_jobs[rerank_mode] = {
                    **job,
                    "status": "failed",
                    "message": str(exc),
                    "finished_at": _now_iso(),
                }
        return
    with app.state.eval_lock:
        job = app.state.eval_jobs.get(rerank_mode, _idle_job())
        if job.get("job_id") != job_id:
            return
        app.state.eval_cache[rerank_mode] = summary
        app.state.eval_jobs[rerank_mode] = {
            **job,
            "status": "succeeded",
            "message": None,
            "finished_at": _now_iso(),
        }


app = create_app(eval_cache_dir=Path("data/review_runs"))
