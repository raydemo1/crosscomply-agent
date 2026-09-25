"""The Agent receives legal entailment feedback and abstains when its budget ends."""

import json

from law_agent.review.agent import AgentDecision, AgentState, run_agent
from law_agent.review.result_builder import LLMReviewResultDraft
from law_agent.review.schemas import GroundedClaim, ReviewFacts
from law_agent.review.semantic_grounding import SemanticGroundingRejected, SemanticGroundingVerifier, SemanticVerdict
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


def test_verifier_receives_full_article_and_rejects_unresolved_exemptions() -> None:
    client = FakeClient(outputs=[{
        "status": "uncertain",
        "claim_checks": [{"claim_index": 0, "status": "uncertain", "reason": "并列免予情形未核查", "missing_facts": ["个人合同必要性"]}],
        "conclusion_reason": "第五条其他免予分支尚未核查",
        "missing_facts": ["个人合同必要性"],
    }])
    verifier = SemanticGroundingVerifier(model_id="test-model", client=client)
    hit = _hit().model_copy(update={"full_article_text": "第五条（一）个人合同；（四）数量条件。"})

    verdict = verifier(
        draft=_draft(), confirmed_intake={"annual_non_sensitive_count": "300000", "count_period": "unknown"},
        extracted_facts=ReviewFacts(), material="申请材料", evidence=[hit],
    )

    payload = json.loads(client.calls[0][1].content)
    assert payload["cited_authorities"][0]["article_text"] == hit.full_article_text
    assert payload["confirmed_intake"]["count_period"] == "unknown"
    assert verdict.status == "uncertain"
    assert client.kwargs[0]["model"] == "test-model"


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
