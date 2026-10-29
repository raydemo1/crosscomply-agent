"""Scope feedback removes an unnecessary source hold without dropping real blockers."""

from law_agent.review.agent import AgentDecision, AgentState, run_agent
from law_agent.review.report_grounding import ReportCheck
from law_agent.review.result_builder import LLMReviewResultDraft
from law_agent.review.schemas import ReviewFacts
from law_agent.review.semantic_grounding import SemanticVerdict
from law_agent.review.workflow import review_needs_source_verification
from tests.test_review_completion_contracts import tools


def test_scope_gap_feedback_is_revised_in_the_existing_finish_loop():
    gap = "非自贸区主体适用清单的上位依据待核"
    draft = LLMReviewResultDraft(risk_level="insufficient_evidence",
        decision_summary="本案已明确在自贸区，仍需确认实际出境数据种类后判断路径；范围外企业的适用问题不影响本案当前审查。",
        conclusion="本案在自贸区。", questions=[{"text": "实际出境数据种类"}], legal_basis_gaps=[gap])
    revised = draft.model_copy(update={"legal_basis_gaps": []})
    verifier_calls = []
    finalizer = tools()

    def verify(**kwargs):
        verifier_calls.append(kwargs)
        if kwargs["draft"].legal_basis_gaps:
            return SemanticVerdict(status="unsupported", claim_checks=[], conclusion_reason="应缩小范围",
                core_checks=[ReportCheck(target="legal_basis_gaps:0", status="unsupported",
                    reason="本案明确在自贸区，非自贸区适用关系不阻塞当前目标")])
        return SemanticVerdict(status="supported", claim_checks=[], conclusion_reason="缺口范围已修正")

    finalizer._semantic_verifier = verify
    decisions = iter([draft, revised])
    state = run_agent(AgentState(goal="自贸区业务", facts=ReviewFacts(regions=["自贸区"])), material="", intake={},
        decide=lambda *_: AgentDecision(action="finish", summary="提交", draft=next(decisions)),
        search=lambda *_, **_kwargs: [], web_search=lambda *_: [],
        finalize=lambda report, current: finalizer.finalize(report, current, case_id="case", intake_snapshot={"facts": {}}),
        checkpoint=lambda _: None)
    assert len(verifier_calls) == 2
    assert state.steps[0].observation["revision_targets"][0]["target"] == "legal_basis_gaps:0"
    assert state.result["review_result"]["legal_basis_gaps"] == []
    assert state.result["review_result"]["missing_information"] == ["实际出境数据种类"]
    assert not review_needs_source_verification(state.result)
