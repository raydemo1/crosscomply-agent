from __future__ import annotations

from threading import Barrier

from law_agent.llm.openai_compatible import ChatMessage
from law_agent.review.agents import (
    build_evidence_dossiers,
    gate_revision_actions,
    run_case_analyst,
    run_evidence_critic,
    select_issue_aware_hits,
    should_run_evidence_critic,
)
from law_agent.review.query_planner import LLMRetrievalQuery
from law_agent.review.schemas import (
    CritiqueDecision,
    EvidenceSelfCheck,
    IssueDraft,
    IssuePlan,
    IssuePlanDraft,
    RetrievalQuery,
    ReviewFacts,
    ReviewIssue,
    ReviewResult,
)
from tests.test_review_result_builder import _hit


class FakeClient:
    def __init__(self, output: dict) -> None:
        self.output = output
        self.calls: list[list[ChatMessage]] = []

    def chat_json(self, messages: list[ChatMessage], **kwargs) -> dict:
        self.calls.append(messages)
        return self.output


def _issue_plan(queries: list[RetrievalQuery]) -> IssuePlan:
    return IssuePlan(
        issues=[
            ReviewIssue(
                issue_id=f"issue_{index}",
                question=query.text,
                query_ids=[query.query_id],
                query_types=[query.query_type],
                priority="high" if query.query_type == "legal_issue" else "medium",
            )
            for index, query in enumerate(queries, start=1)
        ]
    )


def test_evidence_dossiers_map_hits_to_issue_query_types() -> None:
    queries = [
        RetrievalQuery(query_id="q_1", query_type="legal_issue", text="出境评估条件"),
        RetrievalQuery(query_id="q_2", query_type="region_condition", text="上海负面清单"),
    ]
    plan = _issue_plan(queries)
    hits = [
        _hit().model_copy(update={"chunk_id": "legal", "matched_query_type": "legal_issue"}),
        _hit().model_copy(update={"chunk_id": "region", "matched_query_type": "region_condition"}),
    ]

    dossiers = build_evidence_dossiers(plan, hits)

    assert dossiers[0].evidence_chunk_ids == ["legal"]
    assert dossiers[1].evidence_chunk_ids == ["region"]
    assert all(not dossier.evidence_gap for dossier in dossiers)


def test_case_analyst_runs_three_steps_and_parallel_issue_queries() -> None:
    calls: list[str] = []
    barrier = Barrier(2)
    facts = ReviewFacts(cross_border_transfer=True, region="上海")

    def fake_facts(material_text: str, question: str | None = None) -> ReviewFacts:
        calls.append("facts")
        assert material_text == "向境外提供个人信息。"
        assert question == "是否需要申报？"
        return facts

    def fake_issue_plan(
        question: str, material_text: str, extracted_facts: ReviewFacts
    ) -> IssuePlanDraft:
        calls.append("issues")
        assert (question, material_text, extracted_facts) == (
            "是否需要申报？",
            "向境外提供个人信息。",
            facts,
        )
        return IssuePlanDraft(
            issues=[
                IssueDraft(
                    question="是否达到安全评估申报门槛？",
                    query_types=["legal_issue"],
                    required_evidence_roles=["primary_legal_basis"],
                    priority="high",
                ),
                IssueDraft(
                    question="是否适用上海自贸区负面清单？",
                    query_types=["region_condition"],
                ),
            ]
        )

    def fake_issue_queries(
        question: str,
        material_text: str,
        extracted_facts: ReviewFacts,
        issue: IssueDraft,
    ) -> list[LLMRetrievalQuery]:
        assert (question, material_text, extracted_facts) == (
            "是否需要申报？",
            "向境外提供个人信息。",
            facts,
        )
        barrier.wait(timeout=1)
        calls.append(f"queries:{issue.question}")
        if issue.query_types == ["legal_issue"]:
            return [
                LLMRetrievalQuery(
                    query_type="legal_issue",
                    text="数据出境安全评估 申报条件",
                    pathway="security_assessment",
                )
            ]
        return [
            LLMRetrievalQuery(
                query_type="region_condition",
                text="上海自贸区 数据出境 负面清单",
            )
        ]

    analysis = run_case_analyst(
        question="是否需要申报？",
        material_text="向境外提供个人信息。",
        facts_extractor=fake_facts,
        issue_planner=fake_issue_plan,
        issue_query_planner=fake_issue_queries,
    )

    assert calls[:2] == ["facts", "issues"]
    assert {call for call in calls[2:]} == {
        "queries:是否达到安全评估申报门槛？",
        "queries:是否适用上海自贸区负面清单？",
    }
    assert analysis.facts == facts
    assert [query.query_id for query in analysis.queries] == ["q_1", "q_2"]
    assert analysis.issue_plan.issues[0].query_ids == ["q_1"]
    assert analysis.issue_plan.issues[1].query_ids == ["q_2"]


def test_issue_aware_selection_reserves_evidence_for_each_issue() -> None:
    plan = _issue_plan(
        [
            RetrievalQuery(query_id="q_1", query_type="legal_issue", text="核心条件"),
            RetrievalQuery(query_id="q_2", query_type="region_condition", text="地区条件"),
        ]
    )
    legal = _hit().model_copy(
        update={"chunk_id": "legal", "source_id": "law", "score": 0.8}
    )
    region = _hit().model_copy(
        update={"chunk_id": "region", "source_id": "region", "score": 0.7}
    )
    global_best = _hit().model_copy(
        update={"chunk_id": "global", "source_id": "guide", "score": 0.9}
    )

    selected = select_issue_aware_hits(
        plan,
        {"issue_1": [legal], "issue_2": [region]},
        [global_best],
        top_k=3,
    )

    assert {hit.chunk_id for hit in selected} == {"legal", "region", "global"}
    assert [hit.rank for hit in selected] == [1, 2, 3]


def test_issue_aware_selection_does_not_force_weak_medium_hit_into_top_five() -> None:
    plan = _issue_plan(
        [RetrievalQuery(query_id="q_1", query_type="region_condition", text="地区条件")]
    )
    global_hits = [
        _hit().model_copy(
            update={
                "chunk_id": f"global_{index}",
                "source_id": f"global_source_{index}",
                "score": 1.0 - index * 0.1,
                "rank": index,
            }
        )
        for index in range(5)
    ]
    weak_region = _hit().model_copy(
        update={"chunk_id": "weak_region", "source_id": "weak_region_source"}
    )

    selected = select_issue_aware_hits(
        plan,
        {"issue_1": [weak_region]},
        global_hits,
        top_k=5,
    )

    assert "weak_region" not in {hit.chunk_id for hit in selected}


def test_single_issue_selection_preserves_global_ranking() -> None:
    plan = _issue_plan(
        [RetrievalQuery(query_id="q_1", query_type="legal_issue", text="核心条件")]
    )
    global_hits = [
        _hit().model_copy(
            update={
                "chunk_id": f"global_{index}",
                "source_id": f"global_source_{index}",
                "score": 1.0 - index * 0.1,
                "rank": index,
            }
        )
        for index in range(5)
    ]
    promoted = _hit().model_copy(
        update={"chunk_id": "promoted", "source_id": "promoted_source", "score": 1.1}
    )

    selected = select_issue_aware_hits(
        plan,
        {"issue_1": [promoted]},
        global_hits,
        top_k=5,
    )

    assert [hit.chunk_id for hit in selected] == [
        "global_0",
        "global_1",
        "global_2",
        "global_3",
        "global_4",
    ]


def test_issue_aware_selection_never_repeats_a_source() -> None:
    plan = _issue_plan(
        [
            RetrievalQuery(query_id="q_1", query_type="legal_issue", text="核心条件"),
            RetrievalQuery(query_id="q_2", query_type="region_condition", text="地区条件"),
        ]
    )
    global_hits = [
        _hit().model_copy(update={"chunk_id": "a1", "source_id": "source_a"}),
        _hit().model_copy(update={"chunk_id": "b1", "source_id": "source_b"}),
    ]
    duplicate = _hit().model_copy(
        update={"chunk_id": "a2", "source_id": "source_a", "score": 1.0}
    )

    selected = select_issue_aware_hits(
        plan,
        {"issue_1": [duplicate], "issue_2": []},
        global_hits,
        top_k=3,
    )

    assert [hit.source_id for hit in selected] == ["source_a", "source_b"]


def test_dossiers_can_use_issue_specific_candidate_pools() -> None:
    plan = _issue_plan(
        [RetrievalQuery(query_id="q_1", query_type="legal_issue", text="核心条件")]
    )
    precise = _hit().model_copy(update={"chunk_id": "precise", "source_id": "law"})

    dossiers = build_evidence_dossiers(
        plan,
        [],
        issue_hits_by_issue={"issue_1": [precise]},
    )

    assert dossiers[0].evidence_chunk_ids == ["precise"]


def test_revision_gate_converts_unavailable_addition_to_evidence_gap() -> None:
    decision = CritiqueDecision(
        decision="research_required",
        revision_actions=[
            {
                "operation": "add_supported_claim",
                "issue_id": "issue_1",
                "reason": "补充测绘法直接依据",
                "replacement_text": "该活动构成测绘活动。",
                "supporting_chunk_ids": ["not_retrieved"],
            }
        ],
        targeted_retrieval_requests=[
            {
                "issue_id": "issue_1",
                "query": "测绘法 测绘活动 定义",
                "reason": "缺少直接依据",
            }
        ],
        reason="需要补证据",
    )
    auxiliary = _hit().model_copy(
        update={
            "can_cite_clause": True,
            "citation_role": "primary_legal_basis",
            "title": "个人信息保护法",
            "text": "个人信息处理者应当履行个人信息保护义务。",
        }
    )

    actions = gate_revision_actions(
        decision,
        issue_hits_by_issue={"issue_1": [auxiliary]},
        targeted_hits_by_issue={"issue_1": [auxiliary]},
    )

    assert [action.operation for action in actions] == ["mark_evidence_gap"]
    assert "未召回" in actions[0].reason


def test_revision_gate_rejects_irrelevant_citable_hit_for_external_law() -> None:
    decision = CritiqueDecision(
        decision="research_required",
        revision_instructions=["明确语料范围并收窄结论"],
        targeted_retrieval_requests=[
            {
                "issue_id": "issue_1",
                "query": "EU AI Act high-risk system obligations",
                "reason": "需要欧盟法直接依据",
            }
        ],
        reason="当前证据不覆盖欧盟法",
    )
    chinese_law = _hit().model_copy(
        update={
            "title": "个人信息保护法",
            "text": "个人信息处理者应当履行保护义务。",
            "can_cite_clause": True,
        }
    )

    actions = gate_revision_actions(
        decision,
        issue_hits_by_issue={"issue_1": [chinese_law]},
        targeted_hits_by_issue={"issue_1": [chinese_law]},
    )

    assert [action.operation for action in actions] == [
        "narrow_claim",
        "mark_evidence_gap",
    ]


def test_critic_only_runs_for_risk_or_evidence_signals() -> None:
    plan = _issue_plan(
        [RetrievalQuery(query_id="q_1", query_type="legal_issue", text="一般问题")]
    )
    low_result = ReviewResult(
        review_result_id="r",
        review_case_id="c",
        trace_id="t",
        risk_level="low",
        conclusion="低风险",
        review_facts=ReviewFacts(),
    )

    assert not should_run_evidence_critic(
        low_result,
        EvidenceSelfCheck(status="sufficient"),
        plan,
    )
    assert should_run_evidence_critic(
        low_result.model_copy(update={"risk_level": "high"}),
        EvidenceSelfCheck(status="sufficient"),
        plan,
    )

    four_issue_plan = _issue_plan(
        [
            RetrievalQuery(query_id="q_1", query_type="legal_issue", text="核心"),
            RetrievalQuery(query_id="q_2", query_type="region_condition", text="地区"),
            RetrievalQuery(query_id="q_3", query_type="industry_condition", text="行业"),
            RetrievalQuery(query_id="q_4", query_type="missing_information", text="缺失"),
        ]
    )
    assert not should_run_evidence_critic(
        low_result,
        EvidenceSelfCheck(status="sufficient"),
        four_issue_plan,
    )


def test_evidence_critic_returns_strict_revision_decision() -> None:
    client = FakeClient(
        {
                "decision": "research_required",
            "unsupported_claims": ["缺少依据的结论"],
            "missing_issue_ids": ["issue_1"],
            "revision_instructions": ["删除无依据结论并覆盖 issue_1"],
            "revision_actions": [
                {
                    "operation": "narrow_claim",
                    "claim_index": 0,
                    "reason": "关键问题证据不足",
                }
            ],
            "targeted_retrieval_requests": [
                {
                    "issue_id": "issue_1",
                    "query": "数据出境安全评估 申报门槛 条文",
                    "query_type": "legal_issue",
                    "reason": "缺少直接法条",
                }
            ],
            "reason": "关键问题未覆盖",
        }
    )
    plan = _issue_plan(
        [RetrievalQuery(query_id="q_1", query_type="legal_issue", text="出境评估条件")]
    )
    result = ReviewResult(
        review_result_id="r",
        review_case_id="c",
        trace_id="t",
        risk_level="high",
        conclusion="需要申报。",
        review_facts=ReviewFacts(cross_border_transfer=True),
    )

    decision = run_evidence_critic(
        result=result,
        issue_plan=plan,
        dossiers=build_evidence_dossiers(plan, [_hit()]),
        evidence_hits=[_hit()],
        client=client,  # type: ignore[arg-type]
        max_retries=0,
    )

    assert decision == CritiqueDecision(
        decision="research_required",
        unsupported_claims=["缺少依据的结论"],
        missing_issue_ids=["issue_1"],
        revision_instructions=["删除无依据结论并覆盖 issue_1"],
        revision_actions=[
            {
                "operation": "narrow_claim",
                "claim_index": 0,
                "reason": "关键问题证据不足",
            }
        ],
        targeted_retrieval_requests=[
            {
                "issue_id": "issue_1",
                "query": "数据出境安全评估 申报门槛 条文",
                "query_type": "legal_issue",
                "reason": "缺少直接法条",
            }
        ],
        reason="关键问题未覆盖",
    )
    assert "issue_1" in client.calls[0][-1].content
