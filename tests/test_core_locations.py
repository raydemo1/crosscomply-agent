"""Core-report rejection locations and replay of frozen Shanghai outputs."""


import pytest

from law_agent.review.agent import AgentDecision, AgentState, revision_targets, run_agent
from law_agent.review.llm import ReviewWorkflowFailed
from law_agent.review.schemas import ReviewFacts
from law_agent.review.semantic_grounding import (
    SemanticGroundingRejected,
    SemanticGroundingVerifier,
    SemanticVerdict,
)
from tests.test_review_llm import FakeClient
from tests.test_review_result_builder import _hit
from tests.test_semantic_grounding import _core_supported, _draft


def body_failure(**changes):
    return {"target": "conclusion", "status": "unsupported", "quote": "应采用标准合同。",
            "reason": "未保留影响结论的条件", "evidence_ids": ["c1"], **changes}


def verify(client, draft=None):
    return SemanticGroundingVerifier(model_id="offline", client=client)(
        review_goal="限定问题", draft=draft or _draft(), confirmed_intake={}, extracted_facts=ReviewFacts(),
        material="材料", evidence=[_hit()],
    )


def test_body_error_with_supported_claims_is_located_in_one_existing_call():
    client = FakeClient(outputs=[{**_core_supported(), "core_checks": [body_failure()]}])
    verdict = verify(client)
    assert verdict.status == "unsupported" and len(client.calls) == 1
    assert all(c.status == "supported" for c in verdict.claim_checks)
    target = revision_targets(_draft(), verdict, evidence_ids={"c1"})[0]
    assert target["target"] == "conclusion" and target["evidence_ids"] == ["c1"]
    assert target["text"][target["start_offset"]:target["end_offset"]] == target["quote"]


def test_supported_checks_are_not_core_failure_records():
    payload = {**_core_supported(), "core_checks": [body_failure(status="supported")]}
    with pytest.raises(ReviewWorkflowFailed):
        verify(FakeClient(outputs=[payload, payload]))


@pytest.mark.parametrize("quote", [None, "", "概括出来的错误句", "应采用标准合同。"])
def test_unreliable_quote_returns_whole_field_without_retry_or_highlight(quote):
    draft = _draft().model_copy(update={"conclusion": "应采用标准合同。应采用标准合同。"})
    payload = {**_core_supported(), "core_checks": [body_failure(quote=quote)]}
    client = FakeClient(outputs=[payload])
    verdict = verify(client, draft)
    target = revision_targets(draft, verdict, evidence_ids={"c1"})[0]
    assert verdict.status == "unsupported" and len(client.calls) == 1
    assert verdict.core_checks[0].quote == quote
    assert target["text"] == draft.conclusion and target["reason"] == body_failure()["reason"]
    assert not {"quote", "start_offset", "end_offset"} & target.keys()


@pytest.mark.parametrize("target", ["conclusion:invented", "recommended_actions:0"])
def test_unknown_core_target_is_rejected(target):
    invalid = {**_core_supported(), "core_checks": [body_failure(target=target)]}
    with pytest.raises(ReviewWorkflowFailed, match="字段定位"):
        verify(FakeClient(outputs=[invalid, invalid]))


def test_unknown_auxiliary_evidence_is_rejected():
    invalid = {**_core_supported(), "core_checks": [body_failure(evidence_ids=["c1", "invented"])]}
    with pytest.raises(ReviewWorkflowFailed, match="证据编号"):
        verify(FakeClient(outputs=[invalid, invalid]))


def test_overlapping_quote_occurrences_do_not_produce_a_false_unique_location():
    draft = _draft().model_copy(update={"conclusion": "数据数据数据"})
    verdict = SemanticVerdict.model_validate({**_core_supported(), "core_checks": [body_failure(quote="数据数据")]})
    target = revision_targets(draft, verdict, evidence_ids={"c1"})[0]
    assert target["text"] == draft.conclusion and "start_offset" not in target


def test_rejection_without_a_specific_problem_requires_structural_repair():
    payload = {**_core_supported(), "status": "unsupported", "conclusion_reason": "正文有错"}
    client = FakeClient(outputs=[payload, {**payload, "core_checks": [body_failure(quote=None)]}])
    verdict = verify(client)
    assert verdict.status == "unsupported" and len(client.calls) == 2
    assert revision_targets(_draft(), verdict, evidence_ids={"c1"})[0]["target"] == "conclusion"




def test_located_feedback_preserves_draft_and_unaffected_passage_for_next_decision():
    original = _draft().model_copy(update={"conclusion": "已明确的背景保持不变。应采用标准合同。"})
    corrected = original.model_copy(update={"conclusion": "已明确的背景保持不变。需要结合适用条件判断。"})
    verdict = SemanticVerdict.model_validate({**_core_supported(), "core_checks": [body_failure()]})
    seen = []
    def decide(state, _intake):
        if state.steps:
            seen.append(state.model_copy(deep=True))
            return AgentDecision(action="finish", summary="修正定位句", draft=corrected)
        return AgentDecision(action="finish", summary="提交", draft=original)
    def finalize(draft, state):
        if not state.steps:
            raise SemanticGroundingRejected(verdict)
        return {"review_result": draft.model_dump(mode="json")}
    result = run_agent(AgentState(goal="限定问题"), material="", intake={}, decide=decide, search=lambda *_: [],
                       web_search=lambda *_: [], finalize=finalize, checkpoint=lambda _: None)
    assert seen[0].report_draft == original
    target = seen[0].steps[-1].observation["revision_targets"][0]
    assert original.conclusion[target["start_offset"]:target["end_offset"]] == "应采用标准合同。"
    assert result.result["review_result"]["conclusion"].startswith("已明确的背景保持不变。")


@pytest.mark.parametrize("feedback", [
    {"status": "unsupported", "conclusion_reason": "正文缺少法律推导依据"},
    {"core_checks": [body_failure(quote=None)]},
    {"core_checks": [body_failure(target="unknown_field")]},
])
def test_coarse_rejection_keeps_original_draft_and_full_feedback_for_agent(feedback):
    original = _draft()
    corrected = original.model_copy(update={"conclusion": "需要结合适用条件判断。"})
    verdict = SemanticVerdict.model_validate({**_core_supported(), **feedback})
    observations = []

    def decide(state, _intake):
        if state.steps:
            assert state.report_draft == original
            observations.append(state.steps[-1].observation)
            return AgentDecision(action="finish", summary="修订", draft=corrected)
        return AgentDecision(action="finish", summary="提交", draft=original)

    def finalize(draft, state):
        if not state.steps:
            raise SemanticGroundingRejected(verdict)
        return {"review_result": draft.model_dump(mode="json")}

    result = run_agent(
        AgentState(goal="限定问题"), material="", intake={}, decide=decide,
        search=lambda *_: pytest.fail("定位不完整不应自动补证"),
        web_search=lambda *_: pytest.fail("定位不完整不应自动补证"), finalize=finalize, checkpoint=lambda _: None,
    )
    assert result.status == "completed"
    assert observations[0]["semantic_grounding"] == verdict.model_dump(mode="json")
    assert all("start_offset" not in target for target in observations[0]["revision_targets"])
