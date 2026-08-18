"""Evidence self-check and controlled second retrieval.

Issue 7: Evaluate retrieved evidence for sufficiency and run at most one
controlled second retrieval when evidence is weak or mismatched. This gives
the system Agentic RAG behavior without uncontrolled agent loops.

Self-check triggers:
1. No ``primary_legal_basis`` in results
2. Region facts present but no matching local evidence
3. Industry facts present but no matching industry evidence
4. Only implementation/interpretation evidence (no legal basis at all)
5. Cross-border facts present but evidence doesn't match
6. Critical facts are missing (from ReviewFacts.missing_information)

Second retrieval:
- Expand queries with legal terminology
- Add fact keywords (data_types, overseas_recipient, industry, region)
- Increase top_k
- Apply stronger region/industry boost
- Never loops more than once
"""

from __future__ import annotations

import json

from law_agent.config import require_llm_config
from law_agent.data.schemas import Chunk, StrictModel
from law_agent.llm.openai_compatible import ChatMessage, OpenAICompatibleClient
from law_agent.review.llm import StructuredLLMNode
from law_agent.review.query_planner import LLMRetrievalQuery, _QueryIdGenerator
from law_agent.review.schemas import (
    EvidenceIssue,
    EvidenceIssueType,
    EvidenceSelfCheck,
    EvidenceStatus,
    RetrievalHit,
    RetrievalQuery,
    ReviewFacts,
    SecondRetrievalPlan,
)

# ---------------------------------------------------------------------------
# Legal terminology expansions for second retrieval
# ---------------------------------------------------------------------------

_LEGAL_TERM_EXPANSIONS: dict[str, list[str]] = {
    "数据出境": ["数据出境安全评估", "数据跨境传输", "个人信息出境", "数据出境安全评估申报"],
    "安全评估": ["安全评估申报", "安全评估程序", "安全评估条件"],
    "个人信息": ["个人信息保护", "个人信息处理", "个人信息处理者"],
    "跨境": ["数据跨境", "跨境传输", "跨境提供"],
    "负面清单": ["负面清单管理", "自贸区数据出境", "管理清单"],
    "同意": ["单独同意", "知情同意", "告知同意"],
    "汽车": ["汽车数据", "智能网联汽车", "汽车数据处理者"],
    "金融": ["金融数据", "金融信息服务", "金融数据分类分级"],
}

# Critical facts that, if missing, prevent a definitive judgment
_CRITICAL_MISSING_FACTS: frozenset[str] = frozenset(
    {
        "legal_basis_or_consent",
        "overseas_recipient",
        "data_volume_threshold",
    }
)


class LLMEvidenceIssue(StrictModel):
    issue_type: EvidenceIssueType
    description: str


class LLMSecondRetrievalPlan(StrictModel):
    expanded_queries: list[LLMRetrievalQuery]
    increased_top_k: int
    stronger_boost: bool
    reason: str


class LLMEvidenceSelfCheck(StrictModel):
    status: EvidenceStatus
    issues: list[LLMEvidenceIssue]
    triggered_reasons: list[str]
    second_retrieval_triggered: bool
    second_retrieval_plan: LLMSecondRetrievalPlan | None


# ---------------------------------------------------------------------------
# Self-check logic
# ---------------------------------------------------------------------------


def _check_primary_legal_basis(hits: list[RetrievalHit]) -> EvidenceIssue | None:
    """Check if any primary_legal_basis evidence is present."""

    has_primary = any(h.citation_role == "primary_legal_basis" and h.can_cite_clause for h in hits)
    if not has_primary:
        return EvidenceIssue(
            issue_type="no_primary_legal_basis",
            description="检索结果中缺乏可引用的主要法律依据（primary_legal_basis）",
        )
    return None


def _check_region_match(
    hits: list[RetrievalHit], facts: ReviewFacts, chunks_by_id: dict[str, Chunk]
) -> EvidenceIssue | None:
    """Check if region facts have matching local evidence."""

    if not facts.region:
        return None

    from law_agent.review.retrieval.boosts import _REGION_CODE_MAP

    region_code = _REGION_CODE_MAP.get(facts.region, facts.region)
    has_local = False
    for hit in hits:
        chunk = chunks_by_id.get(hit.chunk_id)
        if chunk and (
            chunk.applicable_region == region_code or chunk.applicable_region == facts.region
        ):
            has_local = True
            break

    if not has_local:
        return EvidenceIssue(
            issue_type="region_mismatch",
            description=f"审查事实涉及地区「{facts.region}」，但检索结果中无匹配的地区性依据",
        )
    return None


def _check_industry_match(
    hits: list[RetrievalHit], facts: ReviewFacts, chunks_by_id: dict[str, Chunk]
) -> EvidenceIssue | None:
    """Check if industry facts have matching industry evidence."""

    if not facts.industry:
        return None

    from law_agent.review.retrieval.boosts import _INDUSTRY_KEYWORD_MAP

    keywords = _INDUSTRY_KEYWORD_MAP.get(facts.industry, [facts.industry])
    has_industry = False
    for hit in hits:
        chunk = chunks_by_id.get(hit.chunk_id)
        if chunk:
            combined = " ".join(chunk.applicable_subjects) + " " + " ".join(chunk.topic_tags)
            if any(kw in combined for kw in keywords):
                has_industry = True
                break

    if not has_industry:
        return EvidenceIssue(
            issue_type="industry_mismatch",
            description=f"审查事实涉及行业「{facts.industry}」，但检索结果中无匹配的行业性依据",
        )
    return None


def _check_only_auxiliary_evidence(hits: list[RetrievalHit]) -> EvidenceIssue | None:
    """Check if results contain only implementation/interpretation evidence."""

    if not hits:
        return None

    non_auxiliary_roles = {
        "primary_legal_basis",
        "conditional_local_basis",
        "conditional_industry_basis",
    }
    has_non_auxiliary = any(h.citation_role in non_auxiliary_roles for h in hits)

    if not has_non_auxiliary:
        return EvidenceIssue(
            issue_type="only_auxiliary_evidence",
            description="检索结果仅包含实施参考或解释辅助类证据，缺乏法律效力依据",
        )
    return None


def _check_cross_border_match(hits: list[RetrievalHit], facts: ReviewFacts) -> EvidenceIssue | None:
    """Check if cross-border facts have matching evidence."""

    if not facts.cross_border_transfer:
        return None

    cross_border_keywords = ["出境", "跨境", "境外", "传输"]
    has_match = False
    for hit in hits:
        text = hit.text + hit.title
        if any(kw in text for kw in cross_border_keywords):
            has_match = True
            break

    if not has_match:
        return EvidenceIssue(
            issue_type="cross_border_mismatch",
            description="审查事实涉及数据出境，但检索结果中无数据出境相关证据",
        )
    return None


def _check_critical_facts_missing(facts: ReviewFacts) -> EvidenceIssue | None:
    """Check if critical facts are missing.

    This is a soft warning, not a hard block. Missing facts reduce confidence
    and are recorded in risk_boundaries, but do NOT force abstention when
    evidence is otherwise sufficient. Users often don't mention consent or
    data volume in their material — that's an input quality issue, not an
    evidence sufficiency issue.
    """

    critical_missing = [f for f in facts.missing_information if f in _CRITICAL_MISSING_FACTS]
    if critical_missing:
        return EvidenceIssue(
            issue_type="critical_facts_missing",
            description=f"关键事实缺失：{', '.join(critical_missing)}，影响判断准确性",
        )
    return None


def run_self_check(
    hits: list[RetrievalHit],
    facts: ReviewFacts,
    chunks_by_id: dict[str, Chunk],
) -> EvidenceSelfCheck:
    """Run evidence self-check on retrieved hits.

    Returns an ``EvidenceSelfCheck`` with ``status`` set to:
    - ``sufficient``: no issues detected
    - ``needs_second_retrieval``: issues detected that a second retrieval may fix
    - ``insufficient``: critical issues that second retrieval cannot fix
    """

    issues: list[EvidenceIssue] = []
    triggered_reasons: list[str] = []

    # Run all checks
    checks = [
        ("no_primary_legal_basis", lambda: _check_primary_legal_basis(hits)),
        ("region_mismatch", lambda: _check_region_match(hits, facts, chunks_by_id)),
        ("industry_mismatch", lambda: _check_industry_match(hits, facts, chunks_by_id)),
        ("only_auxiliary_evidence", lambda: _check_only_auxiliary_evidence(hits)),
        ("cross_border_mismatch", lambda: _check_cross_border_match(hits, facts)),
        ("critical_facts_missing", lambda: _check_critical_facts_missing(facts)),
    ]

    for reason, check_fn in checks:
        issue = check_fn()
        if issue is not None:
            issues.append(issue)
            triggered_reasons.append(reason)

    if not issues:
        return EvidenceSelfCheck(
            status="sufficient",
            issues=[],
            triggered_reasons=[],
            second_retrieval_triggered=False,
        )

    # Separate evidence-quality issues from input-quality issues
    has_critical_missing = any(i.issue_type == "critical_facts_missing" for i in issues)
    # Evidence-quality issues are potentially fixable by second retrieval
    evidence_issues = [i for i in issues if i.issue_type != "critical_facts_missing"]

    # If the ONLY issue is critical_facts_missing (evidence is actually good),
    # do NOT abstain — mark as sufficient with a warning. Missing facts is an
    # input quality issue, not an evidence sufficiency issue.
    if not evidence_issues and has_critical_missing:
        return EvidenceSelfCheck(
            status="sufficient",
            issues=issues,
            triggered_reasons=triggered_reasons,
            second_retrieval_triggered=False,
        )

    # Has evidence-quality issues — trigger second retrieval to try to fix them
    plan = build_second_retrieval_plan(evidence_issues, facts, triggered_reasons)
    return EvidenceSelfCheck(
        status="needs_second_retrieval",
        issues=issues,
        triggered_reasons=triggered_reasons,
        second_retrieval_triggered=False,
        second_retrieval_plan=plan,
    )


def needs_llm_self_check(*, question: str, material_text: str, facts: ReviewFacts) -> bool:
    """Return whether scope or sparse-fact ambiguity needs semantic judgment."""

    text = f"{question}\n{material_text}".lower()
    out_of_scope_markers = (
        "eu ai act",
        "欧盟 ai act",
        "ccpa",
        "cpra",
        "gdpr",
    )
    if any(marker in text for marker in out_of_scope_markers):
        return True
    substantive_facts = (
        facts.business_activity,
        facts.data_types,
        facts.cross_border_transfer,
        facts.processing_purpose,
        facts.industry,
        facts.region,
    )
    return not any(value not in (None, "", []) for value in substantive_facts)


# ---------------------------------------------------------------------------
# DeepSeek evidence checker
# ---------------------------------------------------------------------------


def build_evidence_check_messages(
    hits: list[RetrievalHit],
    facts: ReviewFacts,
    *,
    question: str | None = None,
    material_text: str | None = None,
) -> list[ChatMessage]:
    """Build a DeepSeek JSON prompt for evidence sufficiency checking."""

    json_example = {
        "status": "needs_second_retrieval",
        "issues": [
            {
                "issue_type": "no_primary_legal_basis",
                "description": "检索证据缺少可引用的主要法律依据",
            }
        ],
        "triggered_reasons": ["no_primary_legal_basis"],
        "second_retrieval_triggered": False,
        "second_retrieval_plan": {
            "expanded_queries": [
                {
                    "query_type": "legal_issue",
                    "text": "数据出境安全评估 申报条件 个人信息出境",
                }
            ],
            "increased_top_k": 20,
            "stronger_boost": True,
            "reason": "需要补充主要法律依据",
        },
    }
    evidence = [
        {
            "chunk_id": hit.chunk_id,
            "source_id": hit.source_id,
            "title": hit.title,
            "text": hit.text[:1200],
            "citation_role": hit.citation_role,
            "can_cite_clause": hit.can_cite_clause,
            "matched_query_type": hit.matched_query_type,
        }
        for hit in hits[:12]
    ]
    payload = {
        "question": question,
        "material_excerpt": (material_text or "")[:3000],
        "review_facts": facts.model_dump(),
        "evidence": evidence,
        "corpus_scope": {
            "jurisdiction": "中国大陆数据合规和个人信息保护语料",
            "includes": [
                "个人信息保护法、数据安全法、网络安全法、网络数据安全管理条例",
                "数据出境安全评估、个人信息出境标准合同、个人信息保护认证",
                "国家网信部门政策问答、TC260 标准、自贸区地方数据出境清单、汽车/金融等行业材料",
            ],
            "excludes": [
                "EU AI Act",
                "CCPA/CPRA",
                "其他外国法或非数据合规领域问题",
            ],
        },
        "json_example": json_example,
        "instructions": [
            "判断证据是否足以支持材料驱动合规审查。",
            "必须结合 question、material_excerpt、review_facts 和 evidence 判断；不要只因为检索到了相似中国法证据就认为足够。",
            "如果 question 明确超出 corpus_scope，或 evidence 与 question/material_excerpt 不匹配，应输出 insufficient。",
            "不要因为仍有 missing_information 就直接判 insufficient；只要 evidence 足以说明适用规则、义务或边界，就应输出 sufficient，并把事实缺口交给 result_generation 表达。",
            "如果缺少的是法律依据或场景对应证据，可通过补充检索确认法律边界，应输出 needs_second_retrieval。",
            "只有材料几乎没有可审查事实、问题库外、或证据明显不支持问题时，才输出 insufficient。",
            "second_retrieval_plan 的 expanded_queries 必须仍面向 corpus_scope 内语料，不要为库外外国法编造国内法替代查询。",
            "必须输出合法 json object，字段必须与 json_example 完全一致。",
            "status 只能是 sufficient、needs_second_retrieval 或 insufficient。",
            "只有 status 为 needs_second_retrieval 时才给 second_retrieval_plan，否则为 null。",
            "不要输出解释、markdown 或自然语言。",
        ],
    }
    return [
        ChatMessage(
            role="system",
            content=(
                "你是法律合规审查证据自检助手。只输出 json，不输出解释、markdown 或自然语言。"
            ),
        ),
        ChatMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
    ]


def run_self_check_with_deepseek(
    hits: list[RetrievalHit],
    facts: ReviewFacts,
    chunks_by_id: dict[str, Chunk],
    *,
    question: str | None = None,
    material_text: str | None = None,
    client: OpenAICompatibleClient | None = None,
    max_retries: int | None = None,
    trace_id: str | None = None,
) -> EvidenceSelfCheck:
    """Run LLM evidence self-check without rule fallback."""

    if client is None:
        client = OpenAICompatibleClient(require_llm_config())
    node = StructuredLLMNode(
        node_name="evidence_check",
        output_model=LLMEvidenceSelfCheck,
        client=client,
        max_retries=max_retries,
        trace_id=trace_id,
    )
    output = node.run(
        build_evidence_check_messages(
            hits,
            facts,
            question=question,
            material_text=material_text,
        )
    )

    ids = _QueryIdGenerator()
    plan = None
    if output.second_retrieval_plan is not None:
        plan = SecondRetrievalPlan(
            expanded_queries=[
                RetrievalQuery(
                    query_id=ids.next_id(),
                    query_type=query.query_type,
                    text=query.text,
                )
                for query in output.second_retrieval_plan.expanded_queries
            ],
            increased_top_k=output.second_retrieval_plan.increased_top_k,
            stronger_boost=output.second_retrieval_plan.stronger_boost,
            reason=output.second_retrieval_plan.reason,
        )

    return EvidenceSelfCheck(
        status=output.status,
        issues=[
            EvidenceIssue(issue_type=issue.issue_type, description=issue.description)
            for issue in output.issues
        ],
        triggered_reasons=output.triggered_reasons,
        second_retrieval_triggered=output.second_retrieval_triggered,
        second_retrieval_plan=plan,
    )


def validate_llm_self_check(
    llm_check: EvidenceSelfCheck,
    hits: list[RetrievalHit],
    facts: ReviewFacts,
    chunks_by_id: dict[str, Chunk],
) -> EvidenceSelfCheck:
    """Validate LLM self-check output against deterministic rule checks.

    The LLM may hallucinate evidence-composition issues — e.g. reporting
    ``no_primary_legal_basis`` when primary-legal-basis hits are clearly
    present, or ``only_auxiliary_evidence`` when non-auxiliary roles exist.
    These are objective, verifiable facts about the evidence set, so rules
    are authoritative.

    After correction, if no evidence-quality issues remain (only
    ``critical_facts_missing`` which is a soft input-quality warning),
    status is set to ``sufficient`` per the project hard constraint.
    """

    if llm_check.status == "sufficient":
        return llm_check

    # Run rule checks for objective evidence-composition claims.
    # These four issue types are verifiable facts about the evidence set,
    # so rules are authoritative over LLM judgments.
    rule_confirmed: set[str] = set()
    if _check_primary_legal_basis(hits) is not None:
        rule_confirmed.add("no_primary_legal_basis")
    if _check_only_auxiliary_evidence(hits) is not None:
        rule_confirmed.add("only_auxiliary_evidence")
    if _check_region_match(hits, facts, chunks_by_id) is not None:
        rule_confirmed.add("region_mismatch")
    if _check_industry_match(hits, facts, chunks_by_id) is not None:
        rule_confirmed.add("industry_mismatch")

    # Remove LLM issues that rules contradict
    objective_types = {
        "no_primary_legal_basis",
        "only_auxiliary_evidence",
        "region_mismatch",
        "industry_mismatch",
    }
    validated_issues: list[EvidenceIssue] = []
    for issue in llm_check.issues:
        if issue.issue_type in objective_types and issue.issue_type not in rule_confirmed:
            continue  # LLM hallucinated — rule says this issue does not exist
        validated_issues.append(issue)

    # Recompute status: evidence-quality issues vs soft warnings
    evidence_quality_issues = [
        i for i in validated_issues if i.issue_type != "critical_facts_missing"
    ]

    if not evidence_quality_issues:
        return EvidenceSelfCheck(
            status="sufficient",
            issues=validated_issues,
            triggered_reasons=[i.issue_type for i in validated_issues],
            second_retrieval_triggered=False,
        )

    # Real evidence-quality issues remain — keep needs_second_retrieval
    return llm_check.model_copy(update={"issues": validated_issues})


# ---------------------------------------------------------------------------
# Second retrieval plan
# ---------------------------------------------------------------------------


def build_second_retrieval_plan(
    issues: list[EvidenceIssue],
    facts: ReviewFacts,
    triggered_reasons: list[str],
) -> SecondRetrievalPlan:
    """Build a second retrieval plan with expanded queries."""

    ids = _QueryIdGenerator()
    expanded: list[RetrievalQuery] = []

    # Add legal terminology expansions based on issue types
    expansion_terms: list[str] = []
    if (
        "no_primary_legal_basis" in triggered_reasons
        or "only_auxiliary_evidence" in triggered_reasons
    ):
        expansion_terms.extend(["数据出境", "安全评估", "个人信息"])
    if "region_mismatch" in triggered_reasons and facts.region:
        expansion_terms.extend(["负面清单", "自贸区"])
    if "industry_mismatch" in triggered_reasons and facts.industry:
        expansion_terms.append(facts.industry)
    if "cross_border_mismatch" in triggered_reasons:
        expansion_terms.extend(["数据出境", "跨境"])

    seen_texts: set[str] = set()
    for term in expansion_terms:
        for expansion in _LEGAL_TERM_EXPANSIONS.get(term, []):
            if expansion not in seen_texts:
                seen_texts.add(expansion)
                expanded.append(
                    RetrievalQuery(
                        query_id=ids.next_id(),
                        query_type="legal_issue",
                        text=expansion,
                    )
                )

    # Add fact keyword queries
    fact_terms: list[str] = []
    fact_terms.extend(facts.data_types)
    if facts.overseas_recipient:
        fact_terms.append(facts.overseas_recipient)
    if facts.industry:
        fact_terms.append(facts.industry)
    if facts.region:
        fact_terms.append(facts.region)

    if fact_terms:
        expanded.append(
            RetrievalQuery(
                query_id=ids.next_id(),
                query_type="material_fact",
                text=" ".join(fact_terms),
            )
        )

    reason = "; ".join(i.description for i in issues)

    return SecondRetrievalPlan(
        expanded_queries=expanded,
        increased_top_k=20,
        stronger_boost=True,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Post-second-retrieval evaluation
# ---------------------------------------------------------------------------


def evaluate_after_second_retrieval(
    hits: list[RetrievalHit],
    facts: ReviewFacts,
    chunks_by_id: dict[str, Chunk],
    previous_issues: list[EvidenceIssue],
) -> EvidenceSelfCheck:
    """Evaluate evidence after second retrieval. Never triggers another retrieval.

    After the second (final) retrieval attempt, the system should not abstain
    if any citable legal basis is present — region/industry mismatches at this
    point mean the corpus doesn't have a perfect match, not that evidence is
    insufficient to provide a governed review.
    """

    new_check = run_self_check(hits, facts, chunks_by_id)

    if new_check.status == "sufficient":
        return EvidenceSelfCheck(
            status="sufficient",
            issues=[],
            triggered_reasons=[],
            second_retrieval_triggered=True,
        )

    if new_check.issues and all(
        issue.issue_type == "critical_facts_missing" for issue in new_check.issues
    ):
        return EvidenceSelfCheck(
            status="sufficient",
            issues=new_check.issues,
            triggered_reasons=new_check.triggered_reasons,
            second_retrieval_triggered=True,
        )

    # After second retrieval, if any citable legal basis exists (primary or
    # conditional), treat as sufficient — remaining mismatches are corpus
    # coverage limits, not evidence sufficiency failures.
    has_citable_basis = any(
        h.can_cite_clause
        and h.citation_role
        in (
            "primary_legal_basis",
            "conditional_local_basis",
            "conditional_industry_basis",
        )
        for h in hits
    )
    if has_citable_basis:
        return EvidenceSelfCheck(
            status="sufficient",
            issues=new_check.issues,
            triggered_reasons=new_check.triggered_reasons,
            second_retrieval_triggered=True,
        )

    # No citable basis even after second retrieval — genuinely insufficient
    return EvidenceSelfCheck(
        status="insufficient",
        issues=new_check.issues,
        triggered_reasons=new_check.triggered_reasons,
        second_retrieval_triggered=True,
    )
