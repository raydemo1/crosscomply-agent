"""The Agent receives legal entailment feedback and abstains when its budget ends."""

import json

import pytest

from law_agent.data.schemas import Chunk
from law_agent.review.agent import AgentDecision, AgentState, run_agent
from law_agent.review.result_builder import LLMReviewResultDraft
from law_agent.review.schemas import GroundedClaim, ReviewFacts
from law_agent.review.semantic_grounding import (
    SemanticGroundingRejected,
    SemanticGroundingVerifier,
    SemanticVerdict,
)
from tests.test_review_llm import FakeClient
from tests.test_review_result_builder import _hit


def _draft() -> LLMReviewResultDraft:
    return LLMReviewResultDraft(
        risk_level="medium",
        decision_summary="本案现有资料提示需要进一步核对数据出境路径，但已引用法条和确认事实暂时不足以排除全部法定免予情形。",
        legal_path="标准合同",
        conclusion="应采用标准合同。",
        claims=[GroundedClaim(text="不存在免予情形", supporting_chunk_ids=["c1"])],
        trigger_reasons=["人数超过某一数量门槛"],
        missing_information=[],
        recommended_actions=["核查例外"],
        risk_boundaries=[],
    )


@pytest.mark.parametrize("status", ["uncertain", "unsupported"])
def test_unresolved_claim_cannot_be_overridden_by_supported_summary(status):
    verdict = SemanticVerdict(
        status="supported",
        claim_checks=[{"claim_index": 0, "status": status, "reason": "义务适用关系尚未证明"}],
        conclusion_reason="主路径正确",
    )
    assert verdict.status == status
    assert "claim:0" in verdict.conclusion_reason
    assert "义务适用关系" in verdict.conclusion_reason
    assert verdict.missing_facts == []


def test_verifier_receives_full_article_and_rejects_unresolved_exemptions() -> None:
    client = FakeClient(outputs=[{
        "status": "uncertain",
        "claim_checks": [{"claim_index": 0, "status": "uncertain", "reason": "并列免予情形未核查", "missing_facts": ["个人合同必要性"]}],
        "conclusion_reason": "第五条其他免予分支尚未核查",
        "missing_facts": ["个人合同必要性"],
    }])
    verifier = SemanticGroundingVerifier(model_id="test-model", client=client)
    hit = _hit().model_copy(update={"full_article_text": "第五条（一）个人合同；（四）数量条件。", "effective_date": "2024-03-22"})

    verdict = verifier(
        review_goal="只判断出境机制，不认定手续已经完成",
        draft=_draft(), confirmed_intake={"annual_non_sensitive_count": "300000", "count_period": "unknown"},
        extracted_facts=ReviewFacts(), material="申请材料", evidence=[hit],
    )

    payload = json.loads(client.calls[0][1].content)
    assert payload["review_goal"] == "只判断出境机制，不认定手续已经完成"
    assert payload["cited_authorities"][0]["effective_date"] == "2024-03-22"
    assert payload["cited_authorities"][0]["article_text"] == hit.full_article_text
    assert payload["confirmed_intake"]["count_period"] == "unknown"
    assert verdict.status == "uncertain"
    assert client.kwargs[0]["model"] == "test-model"


def test_verifier_sees_the_applicability_boundary_of_each_cited_authority() -> None:
    """检索到不等于适用于本案：适用地区与对象必须随被引法源一并核验。"""

    client = FakeClient(outputs=[{
        "status": "supported",
        "claim_checks": [{"claim_index": 0, "status": "supported", "reason": "支持"}],
        "conclusion_reason": "已核查",
    }])
    verifier = SemanticGroundingVerifier(model_id="test-model", client=client)
    chunk = Chunk(
        chunk_id="c1",
        doc_id="d1",
        source_id="s1",
        title="上海自贸区数据出境负面清单",
        text="再保险领域：需要通过数据出境安全评估的数据清单。",
        chunk_index=0,
        source_url="u",
        char_count=26,
        doc_type="guideline",
        citation_role="conditional_local_basis",
        applicable_region="CN-SH",
        applicable_subjects=["再保险"],
    )

    verifier(
        review_goal="判断上海自贸区规则是否适用",
        draft=_draft(), confirmed_intake={}, extracted_facts=ReviewFacts(),
        material="申请材料", evidence=[_hit()], chunks_by_id={"c1": chunk},
    )

    payload = json.loads(client.calls[0][1].content)
    assert payload["cited_authorities"][0]["applicable_region"] == "CN-SH"
    assert payload["cited_authorities"][0]["applicable_subjects"] == ["再保险"]


def test_verifier_receives_returned_legal_interpretations_without_granting_clause_authority():
    client = FakeClient(outputs=[{
        "status": "supported", "conclusion_reason": "依据法条及解释资料核对",
        "claim_checks": [{"claim_index": 0, "status": "supported", "reason": "支持"}],
    }])
    explanation = _hit().model_copy(update={
        "chunk_id": "faq", "citation_role": "interpretation_auxiliary",
        "can_cite_clause": False, "doc_type": "faq", "authority": "public_interpretation",
        "text": "官方解释说明一般规定与例外的关系。", "full_article_text": None,
        "publish_date": "2026-07-24",
    })
    internal = explanation.model_copy(update={"chunk_id": "internal"})
    unregistered = explanation.model_copy(update={"chunk_id": "unknown"})
    uncited_law = _hit().model_copy(update={"chunk_id": "uncited"})
    chunks = {
        hit.chunk_id: Chunk(
            chunk_id=hit.chunk_id, doc_id=hit.doc_id, source_id=hit.source_id,
            title=hit.title, text=hit.text, chunk_index=0, source_url=hit.source_url,
            char_count=len(hit.text), doc_type="faq", citation_role="interpretation_auxiliary",
            library_kind=library, applicable_region="CN", applicable_subjects=["数据处理者"],
        )
        for hit, library in [(explanation, "legal"), (internal, "internal_policy")]
    }
    SemanticGroundingVerifier(model_id="test-model", client=client)(
        review_goal="审查", draft=_draft(), confirmed_intake={}, extracted_facts=ReviewFacts(),
        material="材料", evidence=[_hit(), explanation, internal, unregistered, uncited_law],
        chunks_by_id=chunks,
    )
    payload = json.loads(client.calls[0][1].content)
    assert [a["chunk_id"] for a in payload["cited_authorities"]] == ["c1"]
    assert [a["chunk_id"] for a in payload["interpretation_authorities"]] == ["faq"]
    context = payload["interpretation_authorities"][0]
    assert context["can_cite_clause"] is False
    assert context["doc_type"] == "faq"
    assert context["article_text"] == explanation.text
    assert context["publish_date"] == "2026-07-24"
    assert context["applicable_region"] == "CN"
    assert context["applicable_subjects"] == ["数据处理者"]
    assert payload["draft"]["claims"][0]["supporting_chunk_ids"] == ["c1"]


def test_semantic_rejection_returns_to_agent_then_budget_abstains() -> None:
    verdict = SemanticVerdict(
        status="unsupported",
        claim_checks=[{"claim_index": 0, "status": "unsupported", "reason": "数量条件不能排除全部例外"}],
        conclusion_reason="第五条并列免予分支未核查",
    )
    seen = []

    def decide(state, _intake):
        seen.append(state.model_copy(deep=True))
        return AgentDecision(action="finish", summary="提交法律判断", draft=_draft())

    def finalize(_draft, _state):
        raise SemanticGroundingRejected(verdict)

    state = run_agent(
        AgentState(goal="审查数据出境", max_turns=2), material="材料", intake={},
        decide=decide, search=lambda *_: [], web_search=lambda *_: [], finalize=finalize,
        abstain=lambda draft, _state: {"review_result": {"risk_level": draft.risk_level, "missing_information": draft.missing_information}},
        checkpoint=lambda _state: None,
    )

    assert seen[1].steps[0].observation["semantic_grounding"]["status"] == "unsupported"
    assert state.status == "completed"
    assert state.result["review_result"]["risk_level"] == "insufficient_evidence"
    assert "并列免予分支" in state.result["review_result"]["missing_information"][0]
