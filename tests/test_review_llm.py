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
from law_agent.review.llm import ReviewWorkflowFailed, StructuredLLMNode, model_for_node
from law_agent.review.query_planner import (
    build_query_planning_messages,
    plan_queries_with_deepseek,
)
from law_agent.review.result_builder import (
    build_result_generation_messages,
    build_review_result_with_deepseek,
    revise_review_result_with_deepseek,
)
from law_agent.review.schemas import (
    EvidenceSelfCheck,
    ReviewFacts,
    ReviewResult,
    RevisionAction,
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
        "region": "CN",
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
    assert '"question"' in combined
    assert '"material_excerpt"' in combined
    assert "evidence_packets" in combined
    assert "supporting_chunk_ids" in combined
    assert "supporting_chunks" in combined
    assert "neighbor_chunks" in combined
    assert "retrieval_queries" in combined


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
        '{"region": CN, "cross_border_transfer": true, '
        '"industry": null, "missing_information": []} trailing text'
    )

    parsed = _loads_tool_arguments(payload)

    assert parsed["region"] == "CN"
    assert parsed["cross_border_transfer"] is True
    assert parsed["industry"] is None


def test_strict_tool_argument_loader_requires_json_object() -> None:
    with pytest.raises(ValueError):
        _loads_tool_arguments("[1, 2, 3]")


def test_structured_node_uses_per_node_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAWAGENT_LLM_FACT_MODEL", "deepseek-v4-flash")
    client = FakeClient(outputs=[_valid_facts_payload()])
    node = StructuredLLMNode(
        node_name="fact_extraction",
        output_model=ReviewFacts,
        client=client,  # type: ignore[arg-type]
        max_retries=0,
    )

    node.run([ChatMessage(role="user", content="json")])

    assert model_for_node("fact_extraction") == "deepseek-v4-flash"
    assert client.kwargs[0]["model"] == "deepseek-v4-flash"


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
    assert result.conclusion.startswith("该场景涉及数据出境")
    assert result.claims[0].supporting_chunk_ids == ["c1"]
    assert result.applicable_evidence
    assert result.citations[0].chunk_id == "c1"


def test_result_generation_prompt_receives_trace_context() -> None:
    client = FakeClient(
        outputs=[
            {
                "risk_level": "medium",
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


def test_result_generation_degrades_when_all_claims_ungrounded() -> None:
    client = FakeClient(
        outputs=[
            {
                "risk_level": "medium",
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

    # All claims referenced unknown chunks → claims silently dropped to []
    # instead of crashing the workflow.
    assert result.claims == []
    assert result.risk_level == "medium"
    assert len(client.calls) == 1


def test_result_generation_no_retry_on_all_claims_ungrounded() -> None:
    """When all claims lose grounding support, the workflow degrades to
    empty claims without retrying — graceful degradation replaces crash."""
    reset_telemetry()
    invalid = {
        "risk_level": "medium",
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

    result = build_review_result_with_deepseek(
        review_result_id="result_1",
        review_case_id="review_1",
        trace_id="trace_1",
        facts=ReviewFacts(cross_border_transfer=True),
        self_check=EvidenceSelfCheck(status="sufficient"),
        evidence_hits=[_hit()],
        client=client,  # type: ignore[arg-type]
        max_retries=1,
    )

    # No retry — claims silently dropped, workflow still produces a result.
    assert result.claims == []
    assert len(client.calls) == 1


def test_result_generation_drops_ungrounded_material_fact_claim() -> None:
    client = FakeClient(
        outputs=[
            {
                "risk_level": "medium",
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

    assert [claim.text for claim in result.claims] == ["数据出境义务需要依据适用法规核验。"]
    assert result.claims[0].supporting_chunk_ids == ["c1"]


def test_result_generation_allows_empty_claim_rail_without_citable_evidence() -> None:
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

    result = build_review_result_with_deepseek(
        review_result_id="result_1",
        review_case_id="review_1",
        trace_id="trace_1",
        facts=ReviewFacts(industry="金融"),
        self_check=EvidenceSelfCheck(status="sufficient"),
        evidence_hits=[auxiliary_hit],
        client=client,  # type: ignore[arg-type]
        max_retries=0,
    )

    assert result.claims == []
    assert result.applicable_evidence
    assert result.applicable_evidence[0].usage == "implementation_reference"


def test_insufficient_evidence_allows_empty_claims_with_irrelevant_citable_hits() -> None:
    client = FakeClient(
        outputs=[
            {
                "risk_level": "insufficient_evidence",
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


def test_revision_applies_bounded_patch_without_regenerating_other_fields() -> None:
    original = ReviewResult(
        review_result_id="result_1",
        review_case_id="review_1",
        trace_id="trace_1",
        risk_level="high",
        conclusion="该活动确定构成测绘活动。",
        review_facts=ReviewFacts(industry="汽车"),
        claims=[
            {
                "text": "该活动确定构成测绘活动。",
                "supporting_chunk_ids": ["c1"],
            }
        ],
        trigger_reasons=["automotive"],
        recommended_actions=["完成内部评估"],
    )
    client = FakeClient(outputs=[])

    result = revise_review_result_with_deepseek(
        result=original,
        actions=[
            RevisionAction(
                operation="mark_evidence_gap",
                issue_id="issue_1",
                reason="未召回可引用的测绘法条文",
            )
        ],
        evidence_hits=[_hit()],
        client=client,  # type: ignore[arg-type]
        max_retries=0,
    )

    assert result.risk_level == "high"
    assert len(result.claims) == 1
    assert result.trigger_reasons == ["automotive"]
    assert result.recommended_actions == ["完成内部评估"]
    assert "测绘法条文" in result.missing_information[0]
    assert client.calls == []


def test_non_abstain_revision_cannot_transition_to_insufficient_evidence() -> None:
    original = ReviewResult(
        review_result_id="result_1",
        review_case_id="review_1",
        trace_id="trace_1",
        risk_level="medium",
        conclusion="当前证据支持有界的中风险判断。",
        review_facts=ReviewFacts(),
        claims=[{"text": "存在有界风险。", "supporting_chunk_ids": ["c1"]}],
    )
    client = FakeClient(
        outputs=[
            {
                "risk_level": "insufficient_evidence",
                "conclusion": "证据不足。",
                "remove_claim_indexes": [0],
                "replace_claims": [],
                "add_claims": [],
                "append_missing_information": [],
                "append_recommended_actions": [],
                "append_risk_boundaries": [],
            }
        ]
    )
    result = revise_review_result_with_deepseek(
        result=original,
        actions=[RevisionAction(operation="narrow_claim", claim_index=0, reason="需要收窄")],
        evidence_hits=[_hit()],
        client=client,  # type: ignore[arg-type]
        max_retries=0,
    )
    assert result.risk_level == "medium"
    assert len(result.claims) == 1


def test_revision_rejects_legal_source_absent_from_evidence() -> None:
    original = ReviewResult(
        review_result_id="result_1",
        review_case_id="review_1",
        trace_id="trace_1",
        risk_level="high",
        conclusion="当前场景存在较高行业合规风险。",
        review_facts=ReviewFacts(industry="汽车"),
        claims=[
            {
                "text": "当前场景存在较高行业合规风险。",
                "supporting_chunk_ids": ["c1"],
            }
        ],
    )
    client = FakeClient(
        outputs=[
            {
                "risk_level": "high",
                "conclusion": "依据《测绘法》，该活动确定构成测绘活动。",
                "remove_claim_indexes": [0],
                "replace_claims": [],
                "add_claims": [],
                "append_missing_information": [],
                "append_recommended_actions": [],
                "append_risk_boundaries": [],
            }
        ]
    )

    with pytest.raises(ReviewWorkflowFailed) as exc_info:
        revise_review_result_with_deepseek(
            result=original,
            actions=[
                RevisionAction(
                    operation="narrow_claim",
                    claim_index=0,
                    reason="当前证据不足",
                )
            ],
            evidence_hits=[_hit(title="个人信息保护法")],
            client=client,  # type: ignore[arg-type]
            max_retries=0,
        )

    assert exc_info.value.reason == "revision_patch_validation_failed"


def test_out_of_corpus_revision_is_deterministic_and_does_not_call_llm() -> None:
    original = ReviewResult(
        review_result_id="result_1",
        review_case_id="review_1",
        trace_id="trace_1",
        risk_level="insufficient_evidence",
        conclusion="当前中国大陆语料不覆盖 EU AI Act。",
        review_facts=ReviewFacts(),
        claims=[],
        risk_boundaries=["仅覆盖中国大陆数据合规语料"],
    )
    client = FakeClient(outputs=[])

    result = revise_review_result_with_deepseek(
        result=original,
        actions=[
            RevisionAction(
                operation="mark_evidence_gap",
                issue_id="issue_1",
                reason="语料未覆盖 EU AI Act，不能生成实体义务结论",
            )
        ],
        evidence_hits=[_hit()],
        client=client,  # type: ignore[arg-type]
        max_retries=0,
    )

    assert result.risk_level == "insufficient_evidence"
    assert result.claims == []
    assert "语料未覆盖" in result.risk_boundaries[-1]
    assert client.calls == []


def test_revision_compiles_single_add_as_narrow_replacement() -> None:
    original = ReviewResult(
        review_result_id="result_1",
        review_case_id="review_1",
        trace_id="trace_1",
        risk_level="medium",
        conclusion="当前证据只支持条件性判断。",
        review_facts=ReviewFacts(),
        claims=[
            {
                "text": "当前证据只支持条件性判断。",
                "supporting_chunk_ids": ["c1"],
            }
        ],
    )
    client = FakeClient(
        outputs=[
            {
                "risk_level": None,
                "conclusion": "当前证据仍只支持条件性判断。",
                "remove_claim_indexes": [],
                "replace_claims": [],
                "add_claims": [
                    {
                        "text": "擅自新增的确定性结论。",
                        "supporting_chunk_ids": ["c1"],
                    }
                ],
                "append_missing_information": [],
                "append_recommended_actions": [],
                "append_risk_boundaries": [],
            }
        ]
    )

    result = revise_review_result_with_deepseek(
        result=original,
        actions=[
            RevisionAction(
                operation="narrow_claim",
                claim_index=0,
                reason="只允许收窄",
            )
        ],
        evidence_hits=[_hit()],
        client=client,  # type: ignore[arg-type]
        max_retries=0,
    )

    assert len(result.claims) == 1
    assert result.claims[0].text == "擅自新增的确定性结论。"


def test_revision_compiler_prefers_replacement_over_duplicate_removal() -> None:
    original = ReviewResult(
        review_result_id="result_1",
        review_case_id="review_1",
        trace_id="trace_1",
        risk_level="medium",
        conclusion="原结论。",
        review_facts=ReviewFacts(),
        claims=[{"text": "原主张。", "supporting_chunk_ids": ["c1"]}],
    )
    client = FakeClient(
        outputs=[
            {
                "risk_level": None,
                "conclusion": "收窄后的结论。",
                "remove_claim_indexes": [0],
                "replace_claims": [
                    {
                        "claim_index": 0,
                        "claim": {"text": "收窄后的主张。", "supporting_chunk_ids": ["c1"]},
                    }
                ],
                "add_claims": [],
                "append_missing_information": [],
                "append_recommended_actions": [],
                "append_risk_boundaries": [],
            }
        ]
    )

    result = revise_review_result_with_deepseek(
        result=original,
        actions=[RevisionAction(operation="narrow_claim", claim_index=0, reason="收窄")],
        evidence_hits=[_hit()],
        client=client,  # type: ignore[arg-type]
        max_retries=0,
    )

    assert [claim.text for claim in result.claims] == ["收窄后的主张。"]


def test_revision_application_degrades_when_claims_lose_support() -> None:
    original = ReviewResult(
        review_result_id="result_1",
        review_case_id="review_1",
        trace_id="trace_1",
        risk_level="medium",
        conclusion="原结论。",
        review_facts=ReviewFacts(),
        claims=[{"text": "原主张。", "supporting_chunk_ids": ["old_chunk"]}],
    )

    result = revise_review_result_with_deepseek(
        result=original,
        actions=[RevisionAction(operation="mark_evidence_gap", reason="缺少当前依据")],
        evidence_hits=[_hit()],
        client=FakeClient(outputs=[]),  # type: ignore[arg-type]
        max_retries=0,
    )

    # Claim referenced a stale chunk_id not in current evidence → silently
    # dropped to [] instead of crashing the revision workflow.
    assert result.claims == []
    assert "缺少当前依据" in result.missing_information
