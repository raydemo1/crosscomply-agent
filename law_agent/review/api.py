"""Application composition for the CrossComply HTTP API.

Business routes live in ``law_agent.review.http`` adapters.  This module keeps
the public ``create_app`` factory, shared storage dependencies, authentication
policy, and review execution helpers used by the worker.
"""

from __future__ import annotations

import html
import os
import re
import tempfile
import threading
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from law_agent.config import load_service_config
from law_agent.kb.admin import KnowledgeJobStore
from law_agent.review.case_store import CaseStore, InMemoryCaseStore, PostgresCaseStore, UserRecord
from law_agent.review.enterprise_store import InMemoryEnterpriseStore, PostgresEnterpriseStore
from law_agent.review.feishu import (
    FeishuApprovalClient,
    FeishuApprovalConfig,
)
from law_agent.review.governance_store import (
    InMemoryGovernanceStore,
    PostgresGovernanceStore,
)
from law_agent.review.http.activity import register_activity_routes
from law_agent.review.http.auth import SESSION_COOKIE, register_auth_routes
from law_agent.review.http.cases import register_case_routes
from law_agent.review.http.evaluation import (
    idle_eval_job,
    preload_eval_cache,
    register_evaluation_routes,
)
from law_agent.review.http.integrations import register_integration_routes
from law_agent.review.http.knowledge import (
    configure_knowledge_state,
    initialize_knowledge_state,
    register_knowledge_routes,
    shutdown_knowledge_state,
)
from law_agent.review.http.remediation import register_remediation_routes
from law_agent.review.http.reports import register_report_routes
from law_agent.review.http.system import register_system_routes
from law_agent.review.http.templates import register_template_routes
from law_agent.review.http.users import register_user_routes
from law_agent.review.io import read_review_results
from law_agent.review.object_store import MaterialObjectStore, material_object_store_from_env
from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH
from law_agent.review.rules import evaluate_national_path
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
from law_agent.review.template_store import (
    InMemoryTemplateStore,
    PostgresTemplateStore,
    TemplateStore,
)
from law_agent.review.user_admin import PostgresUserAdminStore, UserAdminStore


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
        "remediation_plan": store.get_remediation_plan(case["id"]),
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
    remediation_plan: dict[str, Any] | None,
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
    decision_summary = _compact_approval_text(review_result.get("decision_summary"), limit=240)
    summary_parts = [f"风险：{risk}"]
    if paths:
        summary_parts.append(f"候选路径：{'、'.join(paths)}")
    if decision_summary:
        summary_parts.append(f"审批摘要：{decision_summary}")

    action_titles = [
        _compact_approval_text(item, limit=100)
        for item in review_result.get("recommended_actions") or []
    ]
    action_titles.extend(
        _compact_approval_text(action.get("title"), limit=100)
        for action in (remediation_plan or {}).get("tasks", [])
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
    template_store: TemplateStore | None = None,
    feishu_client: FeishuApprovalClient | None = None,
    feishu_config: FeishuApprovalConfig | None = None,
    knowledge_job_store: KnowledgeJobStore | None = None,
    knowledge_corpus: Path | str | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        if not application.state.case_store_initialized:
            application.state.case_store.initialize()
            application.state.template_store.initialize()
            initialize_knowledge_state(application)
            application.state.case_store_initialized = True
        try:
            yield
        finally:
            shutdown_knowledge_state(application)

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
    knowledge_corpus_path = Path(knowledge_corpus) if knowledge_corpus else Path(chunks_path).parent
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
    if template_store is None:
        template_store = (
            InMemoryTemplateStore()
            if isinstance(case_store, InMemoryCaseStore)
            else PostgresTemplateStore(load_service_config().postgres.dsn)
        )
    app.state.template_store = template_store
    configure_knowledge_state(
        app,
        corpus=knowledge_corpus_path,
        job_store=knowledge_job_store,
    )
    app.state.feishu_client = feishu_client
    app.state.feishu_config = feishu_config
    app.state.case_store_initialized = False
    app.state.eval_cache: dict[str, Any] = {"off": None, "embedding": None}
    app.state.eval_jobs: dict[str, dict[str, Any]] = {
        "off": idle_eval_job(),
        "embedding": idle_eval_job(),
    }
    app.state.eval_lock = threading.Lock()
    app.state.eval_cache_dir = Path(eval_cache_dir) if eval_cache_dir is not None else None
    if app.state.eval_cache_dir is not None:
        preload_eval_cache(app, app.state.eval_cache_dir)

    def store() -> CaseStore:
        if not app.state.case_store_initialized:
            app.state.case_store.initialize()
            app.state.case_store_initialized = True
        return app.state.case_store

    def templates() -> TemplateStore:
        return app.state.template_store

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
            remediation_plan = store().get_remediation_plan(case["id"])
            instance = client.create_instance(
                open_id=config.initiator_open_id,
                idempotency_key=running.idempotency_key,
                form=_feishu_approval_form(
                    case=case,
                    task=task,
                    rule_determination=dict(rule.determination),
                    remediation_plan=remediation_plan,
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
                "remediation_plan_snapshot": remediation_plan,
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
            raise HTTPException(status_code=401, detail="请先登录 CrossComply 案件管理")
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

    register_knowledge_routes(app, current_user=current_user, admin_only=admin_only)
    register_system_routes(app)
    register_auth_routes(app, current_user=current_user, store=store)
    register_evaluation_routes(app, current_user=current_user, admin_only=admin_only)
    register_template_routes(
        app,
        current_user=current_user,
        store=store,
        templates=templates,
    )
    register_user_routes(
        app,
        current_user=current_user,
        admin_only=admin_only,
        user_admin=user_admin,
    )
    register_case_routes(
        app,
        current_user=current_user,
        reviewer_only=reviewer_only,
        store=store,
        enterprise=enterprise,
        originals=originals,
        case_payload=case_payload,
        case_summary=_case_summary,
        can_view=_can_view,
        evaluate_national_path=evaluate_national_path,
    )
    register_remediation_routes(
        app,
        current_user=current_user,
        reviewer_only=reviewer_only,
        store=store,
        originals=originals,
        case_summary=_case_summary,
        can_view=_can_view,
    )

    def record_remediation_event(
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

    register_integration_routes(
        app,
        current_user=current_user,
        admin_only=admin_only,
        reviewer_only=reviewer_only,
        store=store,
        enterprise=enterprise,
        governance=governance,
        configured_feishu=configured_feishu,
        deliver_feishu_approval=deliver_feishu_approval,
        remediation_event=record_remediation_event,
    )
    register_report_routes(
        app,
        current_user=current_user,
        reviewer_only=reviewer_only,
        store=store,
        enterprise=enterprise,
        governance=governance,
        originals=originals,
        can_view=_can_view,
    )
    register_activity_routes(
        app,
        current_user=current_user,
        store=store,
        can_view=_can_view,
    )

    return app


app = create_app(eval_cache_dir=Path("data/review_runs"))
