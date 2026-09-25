"""Tests for DeepSeek-owned review LLM nodes."""

from __future__ import annotations

import pytest

from law_agent.llm.openai_compatible import ChatMessage, _loads_tool_arguments
from law_agent.review.evidence import (
    build_evidence_check_messages,
    run_self_check_with_deepseek,
)
from law_agent.review.facts import (
    build_fact_extraction_messages,
    extract_facts_with_deepseek,
)
from law_agent.review.llm import ReviewWorkflowFailed, StructuredLLMNode
from law_agent.review.query_planner import (
    build_query_planning_messages,
    plan_queries_with_deepseek,
)
from law_agent.review.result_builder import (
    build_result_generation_messages,
    build_review_result_with_deepseek,
)
from law_agent.review.schemas import (
    EvidenceSelfCheck,
    ReviewFacts,
    SourceEvidencePacket,
)
from law_agent.review.telemetry import current_telemetry, reset_telemetry
from tests.test_review_result_builder import _hit


class FakeClient:
    def __init__(self, outputs: list[dict] | None = None, errors: list[Exception] | None = None):
        self.outputs = list(outputs or [])
        self.errors = list(errors or [])
        self.calls: list[list[ChatMessage]] = []
        self.kwargs: list[dict] = []

    def chat_json(self, messages: list[ChatMessage], **kwargs) -> dict:
        self.calls.append(messages)
        self.kwargs.append(kwargs)
        if self.errors:
            raise self.errors.pop(0)
        return self.outputs.pop(0)


def _valid_facts_payload() -> dict:
    return {
        "business_activity": "移动 App 个性化推荐",
        "data_types": ["手机号", "定位信息"],
        "sensitive_personal_info": True,
        "cross_border_transfer": True,
        "overseas_recipient": "新加坡",
        "processing_purpose": "推荐优化",
        "legal_basis_or_consent": None,
        "industry": None,
        "regions": ["CN"],
        "missing_information": ["legal_basis_or_consent"],
    }


def test_fact_prompt_contains_json_example() -> None:
    messages = build_fact_extraction_messages("材料", "问题")
    combined = "\n".join(message.content for message in messages)

    assert "json" in combined.lower()
    assert "json_example" in combined
    assert '"business_activity"' in combined


def test_query_prompt_contains_json_example() -> None:
    messages = build_query_planning_messages("问题", ReviewFacts(), "材料")
    combined = "\n".join(message.content for message in messages)

    assert "json" in combined.lower()
    assert "json_example" in combined
    assert '"queries"' in combined


def test_evidence_prompt_contains_json_example() -> None:
    messages = build_evidence_check_messages([_hit()], ReviewFacts(cross_border_transfer=True))
    combined = "\n".join(message.content for message in messages)

    assert "json" in combined.lower()
    assert "json_example" in combined
    assert '"second_retrieval_plan"' in combined


def test_result_prompt_contains_json_example() -> None:
    representative = _hit()
    packet = SourceEvidencePacket(
        source_id=representative.source_id,
        title=representative.title,
        representative_chunk=representative,
        supporting_chunks=[
            representative.model_copy(update={"chunk_id": "support_1", "text": "支撑条款"})
        ],
        neighbor_chunks=[
            representative.model_copy(update={"chunk_id": "neighbor_1", "text": "上下文条款"})
        ],
    )
    messages = build_result_generation_messages(
        facts=ReviewFacts(cross_border_transfer=True),
        self_check=EvidenceSelfCheck(status="sufficient"),
        evidence_hits=[representative],
        source_evidence_packets=[packet],
        question="是否需要数据出境安全评估？",
        material_text="手机号发送给新加坡服务商。",
    )
    combined = "\n".join(message.content for message in messages)

    assert "json" in combined.lower()
    assert "json_example" in combined
    assert '"risk_level"' in combined
    assert '"decision_summary"' in combined
    assert '"question"' in combined
    assert '"material_excerpt"' in combined
    assert "evidence_packets" in combined
    assert "supporting_chunk_ids" in combined
    assert "supporting_chunks" in combined
    assert "neighbor_chunks" in combined
    assert "retrieval_queries" in combined


def test_markdown_result_generation_persists_one_shared_decision_summary() -> None:
    summary = (
        "当前材料显示该境外 SaaS 场景属于个人信息出境，但现有事实尚未达到安全评估门槛。"
        "建议采用标准合同或认证路径，并在上线前完成影响评估、合同签署备案和敏感信息范围核实。"
    )
    client = FakeClient(
        outputs=[{
            "risk_level": "medium",
            "decision_summary": summary,
            "report": (
                "### 风险定性\n该场景属于个人信息出境，存在中等合规风险。\n\n"
                "### 建议措施\n1. 完成个人信息保护影响评估。"
            ),
            "claims": [{
                "text": "该场景属于个人信息出境。",
                "supporting_chunk_ids": ["c1"],
            }],
            "trigger_reasons": ["cross_border_transfer"],
        }]
    )

    result = build_review_result_with_deepseek(
        review_result_id="result_markdown",
        review_case_id="review_markdown",
        trace_id="trace_markdown",
        facts=ReviewFacts(cross_border_transfer=True),
        self_check=EvidenceSelfCheck(status="sufficient"),
        evidence_hits=[_hit()],
        question="是否需要数据出境安全评估？",
        material_text="采购境外 CRM 并向欧洲供应商提供客户与工单数据。",
        client=client,  # type: ignore[arg-type]
        max_retries=0,
        output_format="markdown",
    )

    assert result.decision_summary == summary
    assert result.conclusion.startswith("### 风险定性")
    assert client.kwargs[0]["structured_output_mode"] == "json_object"


def test_structured_node_retries_validation_failure() -> None:
    reset_telemetry()
    client = FakeClient(outputs=[{"extra": "bad"}, _valid_facts_payload()])
    node = StructuredLLMNode(
        node_name="fact_extraction",
        output_model=ReviewFacts,
        client=client,  # type: ignore[arg-type]
        max_retries=1,
        trace_id="trace_1",
    )

    facts = node.run([ChatMessage(role="user", content="json")])

    assert facts.cross_border_transfer is True
    assert len(client.calls) == 2
    assert client.kwargs[0]["output_model"] is ReviewFacts
    assert client.kwargs[0]["tool_name"] == "fact_extraction"
    assert "validation_errors=" in client.calls[1][-1].content
    telemetry = current_telemetry()
    assert telemetry.llm_call_count == 2
    assert telemetry.retry_count == 1


def test_strict_tool_argument_loader_repairs_malformed_json() -> None:
    payload = (
        '{"regions": [CN], "cross_border_transfer": true, '
        '"industry": null, "missing_information": []} trailing text'
    )

    parsed = _loads_tool_arguments(payload)

    assert parsed["regions"] == ["CN"]
    assert parsed["cross_border_transfer"] is True
    assert parsed["industry"] is None


def test_strict_tool_argument_loader_requires_json_object() -> None:
    with pytest.raises(ValueError):
        _loads_tool_arguments("[1, 2, 3]")


def test_structured_node_leaves_model_to_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    # The whole workflow shares one configured model; nodes must not pick
    # their own, so the client's config.model decides.
    monkeypatch.setenv("LAWAGENT_LLM_FACT_MODEL", "some-other-model")
    client = FakeClient(outputs=[_valid_facts_payload()])
    node = StructuredLLMNode(
        node_name="fact_extraction",
        output_model=ReviewFacts,
        client=client,  # type: ignore[arg-type]
        max_retries=0,
    )

    node.run([ChatMessage(role="user", content="json")])

    assert "model" not in client.kwargs[0]


def test_structured_node_exhaustion_returns_review_failed() -> None:
    client = FakeClient(outputs=[{"extra": "bad"}, {"still": "bad"}])
    node = StructuredLLMNode(
        node_name="fact_extraction",
        output_model=ReviewFacts,
        client=client,  # type: ignore[arg-type]
        max_retries=1,
        trace_id="trace_1",
    )

    with pytest.raises(ReviewWorkflowFailed) as exc_info:
        node.run([ChatMessage(role="user", content="json")])

    failure = exc_info.value
    assert failure.failed_node == "fact_extraction"
    assert failure.reason == "pydantic_validation_failed"
    assert failure.attempts == 2
    assert failure.to_response()["status"] == "review_failed"
    assert failure.to_response()["trace_id"] == "trace_1"


def test_fact_extraction_requires_all_llm_fields() -> None:
    payload = _valid_facts_payload()
    payload.pop("missing_information")
    client = FakeClient(outputs=[payload])

    with pytest.raises(ReviewWorkflowFailed) as exc_info:
        extract_facts_with_deepseek(
            "手机号发送给新加坡",
            "是否需要数据出境安全评估？",
            client=client,  # type: ignore[arg-type]
            max_retries=0,
        )

    assert exc_info.value.reason == "pydantic_validation_failed"


def test_query_planning_does_not_fill_empty_queries() -> None:
    client = FakeClient(outputs=[{"queries": []}])

    with pytest.raises(ReviewWorkflowFailed) as exc_info:
        plan_queries_with_deepseek(
            "是否需要数据出境安全评估？",
            ReviewFacts(cross_border_transfer=True),
            "材料",
            client=client,  # type: ignore[arg-type]
            max_retries=0,
        )

    assert exc_info.value.failed_node == "query_planning"


def test_query_planning_assigns_internal_query_ids_after_validation() -> None:
    client = FakeClient(
        outputs=[
            {
                "queries": [
                    {
                        "query_type": "legal_issue",
                        "text": "数据出境安全评估 申报条件",
                    },
                    {
                        "query_type": "material_fact",
                        "text": "手机号 新加坡 数据出境",
                    },
                ]
            }
        ]
    )

    queries = plan_queries_with_deepseek(
        "是否需要数据出境安全评估？",
        ReviewFacts(cross_border_transfer=True),
        "材料",
        client=client,  # type: ignore[arg-type]
        max_retries=0,
    )

    assert [query.query_id for query in queries] == ["q_1", "q_2"]
    assert [query.query_type for query in queries] == ["legal_issue", "material_fact"]


def test_evidence_check_with_deepseek_returns_second_retrieval_plan() -> None:
    client = FakeClient(
        outputs=[
            {
                "status": "needs_second_retrieval",
                "issues": [
                    {
                        "issue_type": "no_primary_legal_basis",
                        "description": "缺少主要法律依据",
                    }
                ],
                "triggered_reasons": ["no_primary_legal_basis"],
                "second_retrieval_triggered": False,
                "second_retrieval_plan": {
                    "expanded_queries": [
                        {
                            "query_type": "legal_issue",
                            "text": "数据出境安全评估 申报条件",
                        }
                    ],
                    "increased_top_k": 20,
                    "stronger_boost": True,
                    "reason": "补充主要法律依据",
                },
            }
        ]
    )

    check = run_self_check_with_deepseek(
        [_hit()],
        ReviewFacts(cross_border_transfer=True),
        {},
        client=client,  # type: ignore[arg-type]
        max_retries=0,
    )

    assert check.status == "needs_second_retrieval"
    assert check.second_retrieval_plan is not None
    assert check.second_retrieval_plan.expanded_queries[0].query_id == "q_1"


def test_result_generation_with_deepseek_uses_program_citation_groups() -> None:
    client = FakeClient(
        outputs=[
            {
                "risk_level": "medium",
                "decision_summary": "当前材料显示该场景涉及数据出境，现阶段可作中风险的有边界判断；审批前仍需确认适用门槛和具体申报条件。",
                "conclusion": "该场景涉及数据出境，需要进一步确认申报条件。",
                "claims": [
                    {
                        "text": "该场景涉及数据出境。",
                        "supporting_chunk_ids": ["c1"],
                    }
                ],
                "trigger_reasons": ["cross_border_transfer"],
                "missing_information": ["data_volume_threshold"],
                "recommended_actions": ["确认出境数据规模"],
                "risk_boundaries": ["本结论基于当前证据。"],
            }
        ]
    )

    result = build_review_result_with_deepseek(
        review_result_id="result_1",
        review_case_id="review_1",
        trace_id="trace_1",
        facts=ReviewFacts(cross_border_transfer=True),
        self_check=EvidenceSelfCheck(status="sufficient"),
        evidence_hits=[_hit()],
        client=client,  # type: ignore[arg-type]
        max_retries=0,
    )

    assert result.risk_level == "medium"
    assert result.decision_summary.startswith("当前材料显示该场景涉及数据出境")
    assert result.conclusion.startswith("该场景涉及数据出境")
    assert result.claims[0].supporting_chunk_ids == ["c1"]
    assert result.applicable_evidence
    assert result.citations[0].chunk_id == "c1"


def test_result_generation_prompt_receives_trace_context() -> None:
    client = FakeClient(
        outputs=[
            {
                "risk_level": "medium",
                "decision_summary": "当前材料和证据能够支持初步审查，但关键事实仍需补充确认；建议在信息核实完成前维持中风险并限制上线。",
                "conclusion": "需要结合材料和证据补充确认。",
                "claims": [
                    {
                        "text": "需要结合材料和证据补充确认。",
                        "supporting_chunk_ids": ["c1"],
                    }
                ],
                "trigger_reasons": ["cross_border_transfer"],
                "missing_information": [],
                "recommended_actions": ["补充确认"],
                "risk_boundaries": ["基于当前材料。"],
            }
        ]
    )

    build_review_result_with_deepseek(
        review_result_id="result_1",
        review_case_id="review_1",
        trace_id="trace_1",
        facts=ReviewFacts(cross_border_transfer=True),
        self_check=EvidenceSelfCheck(status="sufficient"),
        evidence_hits=[_hit()],
        question="是否需要数据出境安全评估？",
        material_text="手机号发送给新加坡服务商。",
        client=client,  # type: ignore[arg-type]
        max_retries=0,
    )

    prompt = client.calls[0][-1].content
    assert "是否需要数据出境安全评估" in prompt
    assert "手机号发送给新加坡服务商" in prompt


def test_result_generation_rejects_all_claims_ungrounded() -> None:
    client = FakeClient(
        outputs=[
            {
                "risk_level": "medium",
                "decision_summary": "当前材料显示该场景涉及数据出境，但原判断缺少可引用证据支撑；建议维持中风险并补充法源后再作审批决定。",
                "conclusion": "该场景涉及数据出境。",
                "claims": [
                    {
                        "text": "该场景涉及数据出境。",
                        "supporting_chunk_ids": ["missing_chunk"],
                    }
                ],
                "trigger_reasons": ["cross_border_transfer"],
                "missing_information": [],
                "recommended_actions": ["补充确认"],
                "risk_boundaries": ["基于当前材料。"],
            }
        ]
    )

    with pytest.raises(ReviewWorkflowFailed, match="未检索到的法条"):
        build_review_result_with_deepseek(
        review_result_id="result_1",
        review_case_id="review_1",
        trace_id="trace_1",
        facts=ReviewFacts(cross_border_transfer=True),
        self_check=EvidenceSelfCheck(status="sufficient"),
        evidence_hits=[_hit()],
        client=client,  # type: ignore[arg-type]
        max_retries=0,
    )

    assert len(client.calls) == 1


def test_result_generation_retries_invalid_grounding() -> None:
    reset_telemetry()
    invalid = {
        "risk_level": "medium",
        "decision_summary": "当前材料显示该场景涉及数据出境，但原判断缺少可引用证据支撑；建议维持中风险并补充法源后再作审批决定。",
        "conclusion": "该场景涉及数据出境。",
        "claims": [
            {
                "text": "该场景涉及数据出境。",
                "supporting_chunk_ids": ["missing_chunk"],
            }
        ],
        "trigger_reasons": ["cross_border_transfer"],
        "missing_information": [],
        "recommended_actions": ["补充确认"],
        "risk_boundaries": ["基于当前材料。"],
    }
    client = FakeClient(outputs=[invalid])

    with pytest.raises(ReviewWorkflowFailed):
        build_review_result_with_deepseek(
        review_result_id="result_1",
        review_case_id="review_1",
        trace_id="trace_1",
        facts=ReviewFacts(cross_border_transfer=True),
        self_check=EvidenceSelfCheck(status="sufficient"),
        evidence_hits=[_hit()],
        client=client,  # type: ignore[arg-type]
        max_retries=1,
    )

    assert len(client.calls) == 2


def test_result_generation_rejects_ungrounded_material_fact_claim() -> None:
    client = FakeClient(
        outputs=[
            {
                "risk_level": "medium",
                "decision_summary": "材料表明业务包含个人信息出境安排，但部分事实性判断缺少可引用法源；建议先核实接收方和适用义务再审批。",
                "conclusion": "材料描述数据出境，相关义务仍需核验。",
                "claims": [
                    {
                        "text": "材料描述手机号发送给新加坡服务商。",
                        "supporting_chunk_ids": ["missing_chunk"],
                    },
                    {
                        "text": "数据出境义务需要依据适用法规核验。",
                        "supporting_chunk_ids": ["c1"],
                    },
                ],
                "trigger_reasons": ["cross_border_transfer"],
                "missing_information": [],
                "recommended_actions": ["补充确认"],
                "risk_boundaries": ["基于当前材料。"],
            }
        ]
    )

    with pytest.raises(ReviewWorkflowFailed, match="未检索到的法条"):
        build_review_result_with_deepseek(
        review_result_id="result_1",
        review_case_id="review_1",
        trace_id="trace_1",
        facts=ReviewFacts(cross_border_transfer=True),
        self_check=EvidenceSelfCheck(status="sufficient"),
        evidence_hits=[_hit()],
        client=client,  # type: ignore[arg-type]
        max_retries=0,
    )



def test_result_generation_rejects_uncitable_claim() -> None:
    auxiliary_hit = _hit().model_copy(
        update={
            "can_cite_clause": False,
            "citation_role": "implementation_reference",
        }
    )
    client = FakeClient(
        outputs=[
            {
                "risk_level": "medium",
                "decision_summary": "现有行业指南只能作为分类分级的实施参考，不能替代直接法律依据；建议维持中风险并补充法源后再决定。",
                "conclusion": "行业指南可以作为分类分级实施参考。",
                "claims": [
                    {
                        "text": "行业指南提供了分类分级参考。",
                        "supporting_chunk_ids": [auxiliary_hit.chunk_id],
                    }
                ],
                "trigger_reasons": ["industry_guidance"],
                "missing_information": [],
                "recommended_actions": ["结合适用法规核验"],
                "risk_boundaries": ["指南不是法条级依据。"],
            }
        ]
    )

    with pytest.raises(ReviewWorkflowFailed, match="不可作为正式法条"):
        build_review_result_with_deepseek(
        review_result_id="result_1",
        review_case_id="review_1",
        trace_id="trace_1",
        facts=ReviewFacts(industry="金融"),
        self_check=EvidenceSelfCheck(status="sufficient"),
        evidence_hits=[auxiliary_hit],
        client=client,  # type: ignore[arg-type]
        max_retries=0,
    )



def test_insufficient_evidence_allows_empty_claims_with_irrelevant_citable_hits() -> None:
    client = FakeClient(
        outputs=[
            {
                "risk_level": "insufficient_evidence",
                "decision_summary": "当前中国大陆数据合规语料不覆盖 EU AI Act，无法形成可靠审批结论；应转交具备相应法域资料的审查流程。",
                "conclusion": "当前语料不覆盖 EU AI Act。",
                "claims": [],
                "trigger_reasons": ["out_of_corpus"],
                "missing_information": [],
                "recommended_actions": ["咨询欧盟法律专业人士"],
                "risk_boundaries": ["仅覆盖中国大陆数据合规语料"],
            }
        ]
    )

    result = build_review_result_with_deepseek(
        review_result_id="result_1",
        review_case_id="review_1",
        trace_id="trace_1",
        facts=ReviewFacts(),
        self_check=EvidenceSelfCheck(status="sufficient"),
        evidence_hits=[_hit()],
        client=client,  # type: ignore[arg-type]
        max_retries=0,
    )

    assert result.risk_level == "insufficient_evidence"
    assert result.claims == []
