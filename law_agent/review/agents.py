"""Bounded multi-agent roles layered over the deterministic review workflow."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor

from law_agent.config import require_llm_config
from law_agent.data.schemas import Chunk
from law_agent.llm.openai_compatible import ChatMessage, OpenAICompatibleClient
from law_agent.review.facts import FactsExtractor, extract_facts_with_deepseek
from law_agent.review.llm import StructuredLLMNode
from law_agent.review.query_planner import (
    LLMRetrievalQuery,
    plan_issue_queries_with_deepseek,
)
from law_agent.review.retrieval.adapters import KeywordSearchAdapter, VectorSearchAdapter
from law_agent.review.retrieval.boosts import apply_boosts_to_hits
from law_agent.review.retrieval.fusion import rrf_fuse, source_aware_fuse
from law_agent.review.retrieval.hits import merge_hits_by_chunk_id
from law_agent.review.retrieval.text import tokenize
from law_agent.review.schemas import (
    CaseAnalysis,
    CritiqueDecision,
    EvidenceDossier,
    EvidenceSelfCheck,
    IssueDraft,
    IssuePlan,
    IssuePlanDraft,
    IssueResearchResult,
    RetrievalHit,
    RetrievalQuery,
    ReviewFacts,
    ReviewIssue,
    ReviewResult,
    RevisionAction,
)

IssuePlanner = Callable[[str, str, ReviewFacts], IssuePlanDraft]
IssueQueryPlanner = Callable[[str, str, ReviewFacts, IssueDraft], list[LLMRetrievalQuery]]


def build_issue_planning_messages(
    *,
    question: str,
    material_text: str,
    facts: ReviewFacts,
) -> list[ChatMessage]:
    payload = {
        "question": question,
        "material_text": material_text[:6000],
        "facts": facts.model_dump(),
    }
    example = {
        "issues": [
            {
                "question": "是否达到数据出境安全评估申报条件？",
                "query_types": ["legal_issue"],
                "required_evidence_roles": ["primary_legal_basis"],
                "priority": "high",
            }
        ]
    }
    return [
        ChatMessage(
            role="system",
            content=(
                "你是企业数据合规审查的 Case Analyst。只负责把材料拆成最多四个相互独立、"
                "可由法律证据回答的问题。每个 issue 必须是一个可独立研究的法律或合规问题，"
                "不得按同一制度的近义表述重复拆分。只使用材料已明确给出的事实；材料缺失时，"
                "将其留给后续检索和报告披露，不得补充假设。不要生成检索词，不要给出法律结论，"
                "不要判断风险等级或建议措施。"
            ),
        ),
        ChatMessage(
            role="user",
            content=(
                "输出严格 JSON，issues 至少一项。每个 issue 必须有明确 question 和至少一个 "
                "query_type；query_types、required_evidence_roles 必须使用受控枚举。"
                "issue.question 应写成后续 Researcher 可以直接回答的具体问题，而不是材料摘要、"
                "宽泛的合规检查清单或预设结论。优先覆盖用户问题所需的核心判断，再覆盖由材料"
                "明确触发的独立问题。"
                f"\njson_example={json.dumps(example, ensure_ascii=False)}"
                f"\npayload={json.dumps(payload, ensure_ascii=False)}"
            ),
        ),
    ]


def plan_issues_with_deepseek(
    *,
    question: str,
    material_text: str,
    facts: ReviewFacts,
    client: OpenAICompatibleClient | None = None,
    max_retries: int | None = None,
    trace_id: str | None = None,
) -> IssuePlanDraft:
    """Run the Case Analyst issue-planning node."""

    if client is None:
        client = OpenAICompatibleClient(require_llm_config())
    node = StructuredLLMNode(
        node_name="case_analyst",
        output_model=IssuePlanDraft,
        client=client,
        max_retries=max_retries,
        trace_id=trace_id,
    )
    return node.run(
        build_issue_planning_messages(
            question=question,
            material_text=material_text,
            facts=facts,
        )
    )


def run_case_analyst(
    *,
    question: str,
    material_text: str,
    client: OpenAICompatibleClient | None = None,
    max_retries: int | None = None,
    trace_id: str | None = None,
    facts_extractor: FactsExtractor | None = None,
    issue_planner: IssuePlanner | None = None,
    issue_query_planner: IssueQueryPlanner | None = None,
) -> CaseAnalysis:
    """Run the multi-step Case Analyst and return its complete research plan.

    Fact extraction and issue planning execute in sequence. Query planning is
    independent after issue planning, so each issue is generated concurrently
    and merged in issue order with system-assigned query IDs.
    """

    if facts_extractor is not None:
        facts = facts_extractor(material_text, question)
    else:
        facts = extract_facts_with_deepseek(
            material_text,
            question,
            client=client,
            max_retries=max_retries,
            trace_id=trace_id,
        )

    if issue_planner is not None:
        draft = issue_planner(question, material_text, facts)
    else:
        draft = plan_issues_with_deepseek(
            question=question,
            material_text=material_text,
            facts=facts,
            client=client,
            max_retries=max_retries,
            trace_id=trace_id,
        )

    def plan_for_issue(issue: IssueDraft) -> list[LLMRetrievalQuery]:
        if issue_query_planner is not None:
            return issue_query_planner(question, material_text, facts, issue)
        return plan_issue_queries_with_deepseek(
            question=question,
            material_text=material_text,
            facts=facts,
            issue=issue,
            client=client,
            max_retries=max_retries,
            trace_id=trace_id,
        )

    with ThreadPoolExecutor(max_workers=len(draft.issues)) as executor:
        query_drafts_by_issue = list(executor.map(plan_for_issue, draft.issues))

    queries: list[RetrievalQuery] = []
    issues: list[ReviewIssue] = []
    next_query_id = 1
    for issue_number, (draft_issue, query_drafts) in enumerate(
        zip(draft.issues, query_drafts_by_issue, strict=True), start=1
    ):
        issue_queries: list[RetrievalQuery] = []
        for query_draft in query_drafts:
            query = RetrievalQuery(
                query_id=f"q_{next_query_id}",
                query_type=query_draft.query_type,
                text=query_draft.text.strip(),
                pathway=query_draft.pathway,
            )
            next_query_id += 1
            issue_queries.append(query)
            queries.append(query)
        issues.append(
            ReviewIssue(
                issue_id=f"issue_{issue_number}",
                question=draft_issue.question,
                query_ids=[query.query_id for query in issue_queries],
                query_types=draft_issue.query_types,
                required_evidence_roles=draft_issue.required_evidence_roles,
                priority=draft_issue.priority,
            )
        )

    return CaseAnalysis(
        facts=facts,
        issue_plan=IssuePlan(issues=issues),
        queries=queries,
    )


def run_evidence_researcher(
    *,
    issue: ReviewIssue,
    queries: list[RetrievalQuery],
    facts: ReviewFacts,
    chunks_by_id: Mapping[str, Chunk],
    keyword_retriever: KeywordSearchAdapter,
    vector_retriever: VectorSearchAdapter,
    candidate_top_k: int,
) -> IssueResearchResult:
    """Execute one issue's bounded retrieval-tool workflow.

    The Evidence Researcher does not invent a second LLM planning step. Its
    only authority is to execute the Case Analyst's assigned queries and to
    return the issue-scoped evidence pool consumed by the Evidence Gate.
    """

    query_by_id = {query.query_id: query for query in queries}
    issue_queries = [
        query_by_id[query_id]
        for query_id in issue.query_ids
        if query_id in query_by_id
    ]
    if not issue_queries:
        raise ValueError(f"issue {issue.issue_id} has no executable queries")

    pairs = [(query.text, query.query_type) for query in issue_queries]
    keyword_per_query = keyword_retriever.search_many(pairs, top_k=candidate_top_k)
    vector_per_query = vector_retriever.search_many(pairs, top_k=candidate_top_k)
    if len(keyword_per_query) != len(issue_queries) or len(vector_per_query) != len(
        issue_queries
    ):
        raise RuntimeError(
            f"issue {issue.issue_id} retrieval adapter returned a mismatched result count"
        )

    def tag_query_type(
        per_query_hits: list[list[RetrievalHit]],
    ) -> list[list[RetrievalHit]]:
        return [
            [
                hit.model_copy(update={"matched_query_type": query.query_type})
                for hit in hits
            ]
            for query, hits in zip(issue_queries, per_query_hits, strict=True)
        ]

    keyword_per_query = tag_query_type(keyword_per_query)
    vector_per_query = tag_query_type(vector_per_query)
    keyword_hits = merge_hits_by_chunk_id(keyword_per_query, top_k=candidate_top_k)
    vector_hits = merge_hits_by_chunk_id(vector_per_query, top_k=candidate_top_k)
    candidate_hits = rrf_fuse(
        apply_boosts_to_hits(keyword_hits, chunks_by_id, facts),
        apply_boosts_to_hits(vector_hits, chunks_by_id, facts),
        top_k=candidate_top_k,
    )
    evidence_hits = source_aware_fuse(
        candidate_hits,
        top_k=candidate_top_k,
        chunks_by_id=chunks_by_id,
    )
    return IssueResearchResult(
        issue_id=issue.issue_id,
        executed_queries=issue_queries,
        keyword_hits=keyword_hits,
        vector_hits=vector_hits,
        candidate_hits=candidate_hits,
        evidence_hits=evidence_hits,
    )


def build_evidence_dossiers(
    issue_plan: IssuePlan,
    evidence_hits: list[RetrievalHit],
    *,
    issue_hits_by_issue: dict[str, list[RetrievalHit]] | None = None,
    research_results: list[IssueResearchResult] | None = None,
) -> list[EvidenceDossier]:
    """Build the deterministic Evidence Gate handoff for every issue."""

    results_by_issue = {
        result.issue_id: result for result in (research_results or [])
    }
    dossiers: list[EvidenceDossier] = []
    for issue in issue_plan.issues:
        matched = (
            issue_hits_by_issue.get(issue.issue_id, [])
            if issue_hits_by_issue is not None
            else results_by_issue[issue.issue_id].evidence_hits
            if issue.issue_id in results_by_issue
            else [
                hit
                for hit in evidence_hits
                if hit.matched_query_type in set(issue.query_types)
            ]
        )
        chunk_ids = list(dict.fromkeys(hit.chunk_id for hit in matched))
        source_ids = list(dict.fromkeys(hit.source_id for hit in matched))
        found_roles = {hit.citation_role for hit in matched}
        missing_roles = [
            role
            for role in issue.required_evidence_roles
            if role not in found_roles
        ]
        if not matched:
            coverage_status = "missing"
        elif missing_roles:
            coverage_status = "partial"
        else:
            coverage_status = "covered"
        dossiers.append(
            EvidenceDossier(
                issue_id=issue.issue_id,
                evidence_chunk_ids=chunk_ids,
                source_ids=source_ids,
                evidence_gap=coverage_status != "covered",
                coverage_status=coverage_status,
                missing_evidence_roles=missing_roles,
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
        "decision": "revision_required",
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
                "高优先级 issue 是否遗漏、风险等级是否与证据冲突。issues 是 Reviewer 必须覆盖的"
                "问题清单；dossiers 是每个问题已经归属的证据和覆盖状态。不要重新分析案件、"
                "重新规划 issue 或要求文风修改。accept 仅在没有实质性证据或覆盖问题时使用；"
                "research_required 仅用于缺少且可能通过一次具体检索补齐的证据；"
                "revision_required 用于现有证据已经足够完成收窄、删除或披露缺口的修订。"
                "修订必须使用 revision_actions 的受控操作；不得要求引用 payload 中不存在的法规。"
            ),
        ),
        ChatMessage(
            role="user",
            content=(
                "请输出严格 JSON。revision_instructions 是兼容字段，保持为空；"
                "decision 只能是 accept、research_required、revision_required。"
                "accept 时 revision_instructions、revision_actions 和 targeted_retrieval_requests 必须为空；"
                "research_required 必须提供 targeted_retrieval_requests 和后续修订 actions；"
                "revision_required 不得提供 targeted_retrieval_requests。若 dossier.coverage_status 为"
                "partial 或 missing，不得因该 issue 未被单独写成段落就直接判错；只在报告遗漏"
                "必要结论、把缺口写成确定事实，或证据与结论冲突时采取行动。需要补证据但当前没有"
                "直接依据时，使用 mark_evidence_gap 或 narrow_claim；定向查询必须短、具体，"
                "只服务于指定 issue，且不得把材料未说明的事实写进 query。"
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
