"""Claims 定向修复与工具参数宽松解析的单元测试。"""

import json

import pytest

from law_agent.llm.openai_compatible import ChatMessage, OpenAICompatibleClient
from law_agent.config import LLMConfig
from law_agent.review.agent import AgentDecision, AgentModel, AgentState, run_agent
from law_agent.review.claim_repair import (
    RepairedClaims,
    is_claim_citation_error,
    make_claim_repairer,
)
from law_agent.review.llm import ReviewWorkflowFailed
from law_agent.review.schemas import GroundedClaimDraft
from law_agent.review.semantic_grounding import SemanticGroundingRejected, SemanticVerdict
from law_agent.review.tool_calling import InvalidToolCall
from tests.test_review_llm import FakeClient, tool_message
from tests.test_review_result_builder import _hit
from tests.test_semantic_grounding import _draft

CITATION_ERROR = (
    "法律主张引用了不可作为正式法条的材料：['guide_doc']。"
    "对应 claim 文本：不存在免予情形。请删除这条 claim，或移除其中的不可引用 chunk。"
)


def _agent_state(**overrides) -> AgentState:
    return AgentState(goal="审查甲义务", **overrides)


def test_is_claim_citation_error_matches_guardrail_prefixes():
    assert is_claim_citation_error(CITATION_ERROR)
    assert is_claim_citation_error("法律主张引用了未检索到的法条：['x']")
    assert is_claim_citation_error("法律主张必须引用至少一条正式法条")
    assert not is_claim_citation_error("decision_summary 必须为单段纯文本")
    assert not is_claim_citation_error("web_impacts只能包含新官方材料")


def test_run_agent_repairs_claim_citation_rejection_without_full_redraft():
    bad = _draft().model_copy(update={"claims": [
        GroundedClaimDraft(text="不存在免予情形", supporting_chunk_ids=["guide_doc"]),
    ]})
    fixed = _draft()
    finalize_calls, repairs = [], []

    def finalize(draft, current):
        finalize_calls.append(draft)
        if len(finalize_calls) == 1:
            raise ValueError(CITATION_ERROR)
        return {"review_result": draft.model_dump(mode="json")}

    def repair_claims(draft, error_message, evidence_hits):
        repairs.append((draft, error_message, [hit.chunk_id for hit in evidence_hits]))
        return fixed

    client = FakeClient(outputs=[tool_message(
        ("f1", "finish", {"summary": "提交", "draft": bad.model_dump(mode="json")}),
    )])
    state = _agent_state(evidence=[_hit()])
    run_agent(state, material="冻结材料", intake={"id": "frozen", "facts": {"condition_b": True}},
              decide=AgentModel(model_id="test", client=client), search=lambda *_: [],
              web_search=lambda *_: [], finalize=finalize, checkpoint=lambda _: None,
              repair_claims=repair_claims)

    assert state.status == "completed"
    assert len(finalize_calls) == 2 and finalize_calls[1] is fixed
    assert [step.observation.get("claims_repaired") for step in state.steps] == [True]
    assert len(repairs) == 1
    assert repairs[0][1] == CITATION_ERROR
    assert repairs[0][2] == ["c1"]


def test_run_agent_falls_back_to_full_revision_when_repair_fails():
    bad = _draft().model_copy(update={"claims": [
        GroundedClaimDraft(text="不存在免予情形", supporting_chunk_ids=["guide_doc"]),
    ]})
    finalize_calls = []

    def finalize(draft, current):
        finalize_calls.append(draft)
        if len(finalize_calls) == 1:
            raise ValueError(CITATION_ERROR)
        return {"review_result": draft.model_dump(mode="json")}

    def failing_repair(draft, error_message, evidence_hits):
        raise ReviewWorkflowFailed(
            failed_node="claim_repair", reason="llm_api_error",
            message="claim_repair failed", attempts=2,
        )

    client = FakeClient(outputs=[
        tool_message(("f1", "finish", {"summary": "提交", "draft": bad.model_dump(mode="json")})),
        tool_message(("f2", "finish", {"summary": "修订", "draft": _draft().model_dump(mode="json")})),
    ])
    state = _agent_state(evidence=[_hit()])
    run_agent(state, material="冻结材料", intake={"id": "frozen", "facts": {"condition_b": True}},
              decide=AgentModel(model_id="test", client=client), search=lambda *_: [],
              web_search=lambda *_: [], finalize=finalize, checkpoint=lambda _: None,
              repair_claims=failing_repair)

    assert state.status == "completed"
    # 修复失败后走完整循环：第二条 finish 的 draft 由 decide 重新产出
    assert len(finalize_calls) == 2 and finalize_calls[1] is not None
    assert "claims_repaired" not in state.steps[0].observation
    assert state.steps[0].observation["error"].startswith("法律主张引用了不可作为正式法条的材料")


def test_semantic_grounding_rejection_never_reaches_claim_repair():
    verdict = SemanticVerdict(status="unsupported", claim_checks=[], core_checks=[], conclusion_reason="正文与例外不一致")
    finalize_calls, repair_calls = [], []

    def finalize(draft, current):
        finalize_calls.append(draft)
        if len(finalize_calls) == 1:
            raise SemanticGroundingRejected(verdict)
        return {"review_result": draft.model_dump(mode="json")}

    def repair_claims(draft, error_message, evidence_hits):
        repair_calls.append(error_message)
        return draft

    client = FakeClient(outputs=[
        tool_message(("f1", "finish", {"summary": "提交", "draft": _draft().model_dump(mode="json")})),
        tool_message(("f2", "finish", {"summary": "修订", "draft": _draft().model_dump(mode="json")})),
    ])
    state = _agent_state(evidence=[_hit()])
    run_agent(state, material="冻结材料", intake={"id": "frozen", "facts": {"condition_b": True}},
              decide=AgentModel(model_id="test", client=client), search=lambda *_: [],
              web_search=lambda *_: [], finalize=finalize, checkpoint=lambda _: None,
              repair_claims=repair_claims)

    assert state.status == "completed"
    assert repair_calls == []
    assert "semantic_grounding" in state.steps[0].observation


def test_make_claim_repairer_rewrites_only_claims():
    bad = _draft().model_copy(update={"claims": [
        GroundedClaimDraft(text="不存在免予情形", supporting_chunk_ids=["guide_doc"]),
    ]})
    client = FakeClient(outputs=[{"claims": [
        {"text": "不存在免予情形", "supporting_chunk_ids": ["c1"]},
    ]}])
    repair = make_claim_repairer(model_id="test", client=client)
    repaired = repair(bad, CITATION_ERROR, [_hit()])
    assert repaired.conclusion == bad.conclusion
    assert [claim.supporting_chunk_ids for claim in repaired.claims] == [["c1"]]
    assert repaired.claims[0].text == "不存在免予情形"
    request = json.loads(client.calls[0][-1].content)
    assert request["rejection"] == CITATION_ERROR
    assert request["citable_chunks"][0]["chunk_id"] == "c1"


def test_make_claim_repairer_fails_closed_when_output_still_uncitable():
    client = FakeClient(outputs=[
        {"claims": [{"text": "不存在免予情形", "supporting_chunk_ids": ["still_guide"]}]},
        {"claims": [{"text": "不存在免予情形", "supporting_chunk_ids": ["still_guide"]}]},
    ])
    repair = make_claim_repairer(model_id="test", client=client)
    with pytest.raises(ReviewWorkflowFailed):
        repair(_draft(), CITATION_ERROR, [_hit()])


def test_tool_arguments_salvaged_for_investigation_tools():
    truncated = '{"summary": "需要补充", "question": "本年度累计出境学生人数是多少'
    client = FakeClient(outputs=[tool_message(("q1", "request_input", truncated))])
    decision = AgentModel(model_id="test", client=client)(
        _agent_state(), {"id": "frozen", "facts": {}},
    )
    assert isinstance(decision, AgentDecision)
    assert decision.action == "request_input"
    assert decision.question == "本年度累计出境学生人数是多少"


def test_finish_arguments_not_salvaged():
    truncated = '{"summary": "提交", "draft": {"risk_level": "medium", "conclusion'
    client = FakeClient(outputs=[tool_message(("f1", "finish", truncated))])
    with pytest.raises(InvalidToolCall):
        AgentModel(model_id="test", client=client)(
            _agent_state(), {"id": "frozen", "facts": {}},
        )


def test_chat_tools_rejects_truncated_tool_response(monkeypatch):
    client = OpenAICompatibleClient(LLMConfig(
        api_key="test", base_url="https://example.test/v1", beta_base_url="https://example.test/beta",
        model="test", timeout_seconds=10, structured_output_mode="json_object", reasoning_effort="none",
    ))
    monkeypatch.setattr(client, "_post_chat", lambda payload, url: {
        "choices": [{"finish_reason": "length", "message": {"role": "assistant", "content": None}}],
    })
    with pytest.raises(RuntimeError, match="truncated"):
        client.chat_tools([ChatMessage(role="user", content="hi")], tools=[{"type": "function"}])


def test_repaired_claims_model_rejects_non_list_payload():
    with pytest.raises(Exception):
        RepairedClaims.model_validate({"claims": "not-a-list"})
