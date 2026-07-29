"""Bounded multi-agent roles layered over the deterministic review workflow."""

from __future__ import annotations

import json
import re

from law_agent.config import require_llm_config
from law_agent.llm.openai_compatible import ChatMessage, OpenAICompatibleClient
from law_agent.review.llm import StructuredLLMNode
from law_agent.review.retrieval.text import tokenize
from law_agent.review.schemas import (
    CaseAnalysis,
    CritiqueDecision,
    EvidenceDossier,
    EvidenceSelfCheck,
    IssuePlan,
    ReviewIssue,
    ReviewResult,
    RevisionAction,
    RetrievalHit,
    RetrievalQuery,
    RetrievalQueryType,
)

_ISSUE_GROUPS: tuple[tuple[str, tuple[RetrievalQueryType, ...], str], ...] = (
    ("核心法律问题", ("legal_issue", "material_fact"), "high"),
    ("地区适用条件", ("region_condition",), "medium"),
    ("行业适用条件", ("industry_condition",), "medium"),
    ("关键缺失信息", ("missing_information",), "medium"),
)


_MAX_ISSUES = 5


def build_issue_plan(queries: list[RetrievalQuery]) -> IssuePlan:
    """Group existing typed queries into at most five review issues.

    The core legal bucket (legal_issue + material_fact) is sub-grouped by
    pathway so that distinct legal institutions become separate issues.
    """

    issues: list[ReviewIssue] = []

    for label, query_types, priority in _ISSUE_GROUPS:
        grouped = [query for query in queries if query.query_type in query_types]
        if not grouped:
            continue

        # Sub-group the core legal bucket by pathway.
        if "legal_issue" in query_types:
            by_pathway: dict[str, list[RetrievalQuery]] = {}
            for query in grouped:
                key = query.pathway or ""
                by_pathway.setdefault(key, []).append(query)
            for pathway_key, sub in by_pathway.items():
                if len(issues) >= _MAX_ISSUES:
                    break
                suffix = f"（{pathway_key}）" if pathway_key else ""
                issue_number = len(issues) + 1
                issues.append(
                    ReviewIssue(
                        issue_id=f"issue_{issue_number}",
                        question=f"{label}{suffix}：" + "；".join(q.text for q in sub),
                        query_ids=[q.query_id for q in sub],
                        query_types=list(dict.fromkeys(q.query_type for q in sub)),
                        priority=priority,
                    )
                )
        else:
            if len(issues) >= _MAX_ISSUES:
                break
            issue_number = len(issues) + 1
            issues.append(
                ReviewIssue(
                    issue_id=f"issue_{issue_number}",
                    question=f"{label}：" + "；".join(query.text for query in grouped),
                    query_ids=[query.query_id for query in grouped],
                    query_types=list(dict.fromkeys(query.query_type for query in grouped)),
                    priority=priority,
                )
            )

    return IssuePlan(issues=issues)


def should_run_case_analyst(facts: object, question: str) -> bool:
    """Route only compound cases through the LLM-owned Analyst module."""

    facets = (
        bool(getattr(facts, "region", None)),
        bool(getattr(facts, "industry", None)),
        getattr(facts, "cross_border_transfer", None) is True,
        getattr(facts, "sensitive_personal_info", None) is True,
    )
    compound_sensitive_path = sum(facets) >= 2 and (facets[2] or facets[3])
    conflict_markers = ("冲突", "比较", "哪个", "边界", "分别", "同时满足", "优先")
    explicit_path_conflict = any(marker in question for marker in conflict_markers)
    return compound_sensitive_path or explicit_path_conflict


def build_case_analyst_messages(
    *,
    question: str,
    material_text: str,
    facts: object,
    initial_queries: list[RetrievalQuery],
) -> list[ChatMessage]:
    payload = {
        "question": question,
        "material_text": material_text,
        "facts": facts.model_dump(),
        "initial_queries": [query.model_dump() for query in initial_queries],
    }
    example = {
        "issues": [
            {
                "issue_id": "issue_1",
                "question": "是否达到数据出境安全评估申报条件？",
                "query_ids": [],
                "query_types": ["legal_issue"],
                "research_queries": ["数据出境安全评估 申报条件 条文"],
                "required_evidence_roles": ["primary_legal_basis"],
                "priority": "high",
            }
        ]
    }
    return [
        ChatMessage(
            role="system",
            content=(
                "你是企业数据合规审查的 Case Analyst。把材料拆成最多四个相互独立、"
                "可由法律证据回答的问题。每个问题生成一到三个短而具体的检索词，优先使用"
                "明确制度名、义务、门槛、地区或行业锚点；禁止只复述材料或生成宽泛问题。"
                "不要给出法律结论。query_ids 留空，由系统分配。"
            ),
        ),
        ChatMessage(
            role="user",
            content=(
                "输出严格 JSON，issues 至少一项。query_types、required_evidence_roles 必须使用"
                "示例所示受控枚举。"
                f"\njson_example={json.dumps(example, ensure_ascii=False)}"
                f"\npayload={json.dumps(payload, ensure_ascii=False)}"
            ),
        ),
    ]


def run_case_analyst(
    *,
    question: str,
    material_text: str,
    facts: object,
    initial_queries: list[RetrievalQuery],
    client: OpenAICompatibleClient | None = None,
    max_retries: int | None = None,
    trace_id: str | None = None,
) -> CaseAnalysis:
    """Generate issue-specific research queries while preserving frozen inputs."""

    if client is None:
        client = OpenAICompatibleClient(require_llm_config())
    node = StructuredLLMNode(
        node_name="case_analyst",
        output_model=IssuePlan,
        client=client,
        max_retries=max_retries,
        trace_id=trace_id,
    )
    draft = node.run(
        build_case_analyst_messages(
            question=question,
            material_text=material_text,
            facts=facts,
            initial_queries=initial_queries,
        )
    )

    combined = list(initial_queries)
    seen_text = {query.text.strip().casefold() for query in initial_queries}
    next_id = len(initial_queries) + 1
    normalized_issues: list[ReviewIssue] = []
    for index, issue in enumerate(draft.issues[:4], start=1):
        assigned: list[str] = []
        query_type = issue.query_types[0] if issue.query_types else "legal_issue"
        for text in issue.research_queries[:3]:
            normalized = text.strip()
            if not normalized or normalized.casefold() in seen_text:
                continue
            query = RetrievalQuery(
                query_id=f"q_{next_id}", query_type=query_type, text=normalized
            )
            next_id += 1
            combined.append(query)
            assigned.append(query.query_id)
            seen_text.add(normalized.casefold())
        if not assigned:
            matching = [q.query_id for q in initial_queries if q.query_type in issue.query_types]
            assigned = matching[:3]
        normalized_issues.append(
            issue.model_copy(
                update={"issue_id": f"issue_{index}", "query_ids": assigned}
            )
        )

    if not normalized_issues:
        fallback = build_issue_plan(initial_queries)
        return CaseAnalysis(issue_plan=fallback, queries=combined)
    return CaseAnalysis(issue_plan=IssuePlan(issues=normalized_issues), queries=combined)


def build_evidence_dossiers(
    issue_plan: IssuePlan,
    evidence_hits: list[RetrievalHit],
    *,
    issue_hits_by_issue: dict[str, list[RetrievalHit]] | None = None,
) -> list[EvidenceDossier]:
    """Assign retrieved evidence to issues using recorded query types."""

    dossiers: list[EvidenceDossier] = []
    for issue in issue_plan.issues:
        matched = (
            issue_hits_by_issue.get(issue.issue_id, [])
            if issue_hits_by_issue is not None
            else [
                hit
                for hit in evidence_hits
                if hit.matched_query_type in set(issue.query_types)
            ]
        )
        chunk_ids = list(dict.fromkeys(hit.chunk_id for hit in matched))
        source_ids = list(dict.fromkeys(hit.source_id for hit in matched))
        dossiers.append(
            EvidenceDossier(
                issue_id=issue.issue_id,
                evidence_chunk_ids=chunk_ids,
                source_ids=source_ids,
                evidence_gap=not chunk_ids,
            )
        )
    return dossiers


def select_issue_aware_hits(
    issue_plan: IssuePlan,
    issue_hits_by_issue: dict[str, list[RetrievalHit]],
    global_hits: list[RetrievalHit],
    *,
    top_k: int,
) -> list[RetrievalHit]:
    """Allocate evidence using global strength plus score-aware issue support."""

    if top_k <= 0:
        return []
    priority = {"high": 0, "medium": 1, "low": 2}
    issues = sorted(issue_plan.issues, key=lambda issue: priority[issue.priority])
    selected: list[RetrievalHit] = []
    chunk_ids: set[str] = set()
    source_ids: set[str] = set()

    def add_best(candidates: list[RetrievalHit]) -> bool:
        ordered = sorted(candidates, key=lambda hit: (-hit.score, hit.rank, hit.chunk_id))
        for prefer_new_source in (True, False):
            for hit in ordered:
                if hit.chunk_id in chunk_ids:
                    continue
                if prefer_new_source and hit.source_id in source_ids:
                    continue
                selected.append(hit)
                chunk_ids.add(hit.chunk_id)
                source_ids.add(hit.source_id)
                return True
        return False

    # Preserve three strong global anchors. At most one high-priority issue
    # may displace a global source in Top-5; the remainder stays globally ranked.
    for hit in global_hits[: min(3, top_k)]:
        add_best([hit])

    high_priority = [issue for issue in issues if issue.priority == "high"]
    if high_priority and len(selected) < top_k:
        add_best(issue_hits_by_issue.get(high_priority[0].issue_id, []))

    while len(selected) < top_k and add_best(global_hits):
        pass
    for issue in issues:
        if len(selected) >= top_k:
            break
        add_best(issue_hits_by_issue.get(issue.issue_id, []))

    return [
        hit.model_copy(update={"rank": rank, "retriever": "hybrid"})
        for rank, hit in enumerate(selected, start=1)
    ]


def should_run_evidence_critic(
    result: ReviewResult,
    self_check: EvidenceSelfCheck,
    issue_plan: IssuePlan,
) -> bool:
    """Limit Critic cost to risky, retried, insufficient, or complex cases."""

    return (
        result.risk_level == "high"
        or self_check.second_retrieval_triggered
        or self_check.status == "insufficient"
    )


def gate_revision_actions(
    decision: CritiqueDecision,
    *,
    issue_hits_by_issue: dict[str, list[RetrievalHit]],
    targeted_hits_by_issue: dict[str, list[RetrievalHit]] | None = None,
) -> list[RevisionAction]:
    """Downgrade impossible evidence additions before the Revision node."""

    targeted_hits_by_issue = targeted_hits_by_issue or {}
    actions = list(decision.revision_actions)
    if not actions:
        actions = [
            RevisionAction(operation="narrow_claim", reason=instruction)
            for instruction in decision.revision_instructions
        ]

    gated: list[RevisionAction] = []

    def relevant(hit: RetrievalHit, request_text: str) -> bool:
        stop_terms = {
            "法律",
            "法规",
            "依据",
            "条文",
            "直接",
            "要求",
            "缺少",
            "补充",
            "规定",
            "制度",
            "问题",
            "相关",
            "当前",
            "证据",
        }
        hit_text = f"{hit.title} {hit.text[:800]}"
        english_anchors = {
            token.casefold()
            for token in re.findall(r"[A-Za-z][A-Za-z0-9-]*", request_text)
            if token.casefold() not in {"law", "legal", "rule", "rules"}
        }
        if english_anchors and not english_anchors.issubset(
            set(re.findall(r"[A-Za-z][A-Za-z0-9-]*", hit_text.casefold()))
        ):
            return False
        request_terms = {
            term
            for term in tokenize(request_text)
            if term not in stop_terms and len(term) > 1
        }
        if not request_terms:
            return False
        hit_terms = set(tokenize(hit_text))
        return bool(request_terms & hit_terms)

    for action in actions:
        if action.operation != "add_supported_claim":
            gated.append(action)
            continue
        issue_hits = issue_hits_by_issue.get(action.issue_id or "", [])
        action_text = f"{action.reason} {action.replacement_text or ''}"
        allowed = {
            hit.chunk_id
            for hit in issue_hits
            if hit.can_cite_clause and relevant(hit, action_text)
        }
        requested = set(action.supporting_chunk_ids)
        if requested and requested.issubset(allowed):
            gated.append(action)
            continue
        gated.append(
            RevisionAction(
                operation="mark_evidence_gap",
                issue_id=action.issue_id,
                reason=(
                    "定向检索后仍未召回 Critic 要求的可引用法条；"
                    f"不得新增结论。原要求：{action.reason}"
                ),
            )
        )

    existing_gap_issues = {
        action.issue_id
        for action in gated
        if action.operation == "mark_evidence_gap"
    }
    for request in decision.targeted_retrieval_requests:
        targeted_citable = [
            hit
            for hit in targeted_hits_by_issue.get(request.issue_id, [])
            if hit.can_cite_clause
            and relevant(hit, f"{request.query} {request.reason}")
        ]
        if targeted_citable or request.issue_id in existing_gap_issues:
            continue
        gated.append(
            RevisionAction(
                operation="mark_evidence_gap",
                issue_id=request.issue_id,
                reason=(
                    "定向检索未召回可引用条文，只能收窄结论并披露证据缺口："
                    f"{request.reason}"
                ),
            )
        )
    return gated[:5]


def build_critic_messages(
    *,
    result: ReviewResult,
    issue_plan: IssuePlan,
    dossiers: list[EvidenceDossier],
    evidence_hits: list[RetrievalHit],
) -> list[ChatMessage]:
    payload = {
        "issues": [issue.model_dump() for issue in issue_plan.issues],
        "dossiers": [dossier.model_dump() for dossier in dossiers],
        "result": {
            "risk_level": result.risk_level,
            "conclusion": result.conclusion,
            "claims": [
                {"claim_index": index, **claim.model_dump()}
                for index, claim in enumerate(result.claims)
            ],
            "missing_information": result.missing_information,
            "risk_boundaries": result.risk_boundaries,
        },
        "evidence": [
            {
                "chunk_id": hit.chunk_id,
                "title": hit.title,
                "text": hit.text,
                "can_cite_clause": hit.can_cite_clause,
                "citation_role": hit.citation_role,
            }
            for hit in evidence_hits
        ],
    }
    example = {
        "decision": "revise",
        "unsupported_claims": ["缺少直接依据的确定性结论"],
        "missing_issue_ids": [],
        "revision_instructions": [],
        "revision_actions": [
            {
                "operation": "narrow_claim",
                "reason": "现有证据只支持条件性判断",
                "claim_index": 0,
            }
        ],
        "targeted_retrieval_requests": [],
        "reason": "删除或收窄无依据结论",
    }
    return [
        ChatMessage(
            role="system",
            content=(
                "你是企业数据合规审查的 Evidence Critic。只检查结论是否超出证据、"
                "高优先级 issue 是否遗漏、风险等级是否与证据冲突。若缺少可补齐的关键证据，"
                "可给出最多三个 targeted_retrieval_requests；不要要求文风修改。"
                "修订必须使用 revision_actions 的受控操作；不得要求引用 payload 中不存在的法规。"
            ),
        ),
        ChatMessage(
            role="user",
            content=(
                "请输出严格 JSON。revision_instructions 是兼容字段，保持为空；"
                "approve 时 revision_instructions 和 targeted_retrieval_requests 必须为空；"
                "approve 时 revision_actions 也必须为空。需要补证据但当前没有直接依据时，"
                "使用 mark_evidence_gap 或 narrow_claim；定向查询必须短、具体。"
                f"\njson_example={json.dumps(example, ensure_ascii=False)}"
                f"\npayload={json.dumps(payload, ensure_ascii=False)}"
            ),
        ),
    ]


def run_evidence_critic(
    *,
    result: ReviewResult,
    issue_plan: IssuePlan,
    dossiers: list[EvidenceDossier],
    evidence_hits: list[RetrievalHit],
    client: OpenAICompatibleClient | None = None,
    max_retries: int | None = None,
) -> CritiqueDecision:
    if client is None:
        client = OpenAICompatibleClient(require_llm_config())
    node = StructuredLLMNode(
        node_name="evidence_critic",
        output_model=CritiqueDecision,
        client=client,
        max_retries=max_retries,
        trace_id=result.trace_id,
    )
    valid_issue_ids = {issue.issue_id for issue in issue_plan.issues}

    def validate_requests(decision: CritiqueDecision) -> CritiqueDecision:
        invalid = [
            request.issue_id
            for request in decision.targeted_retrieval_requests
            if request.issue_id not in valid_issue_ids
        ]
        if invalid:
            raise ValueError(f"unknown targeted retrieval issue_ids: {invalid}")
        return decision

    return node.run(
        build_critic_messages(
            result=result,
            issue_plan=issue_plan,
            dossiers=dossiers,
            evidence_hits=evidence_hits,
        ),
        post_validate=validate_requests,
        post_validation_reason="critic_request_validation_failed",
    )
