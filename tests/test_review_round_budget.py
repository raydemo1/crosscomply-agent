"""Human continuation renews a round, never the cumulative audit counters."""

import pytest

from law_agent.review.agent import (
    AgentDecision,
    AgentState,
    BudgetUsage,
    EvidenceRead,
    EvidenceReadRequest,
    answer_agent,
    investigate,
    remaining_budget,
    run_agent,
)
from law_agent.review.qa_agent import QAState
from law_agent.review.schemas import RetrievalQuery
from law_agent.review.web_research import WebSearchBatch
from tests.test_review_agent import _draft, _hit


def query():
    return RetrievalQuery(query_id="q", query_type="legal_issue", text="当前争点")


@pytest.mark.parametrize("provenance", ["applicant_statement", "reviewer_instruction"])
def test_human_continuation_renews_all_limits_and_keeps_history(provenance):
    hit = _hit("old", "s1")
    state = AgentState(goal="审查", status="waiting_input", gate_id="input_16", turns=16,
                       searches=5, reads=10, web_searches=5, evidence=[hit],
                       read_history=[{"source_id": "s1", "offset": i} for i in range(35)])
    assert remaining_budget(state) == {"turns": 0, "searches": 0, "reads": 0, "web_searches": 0}
    answer_agent(state, gate_id="input_16", answer="审核确认继续调查", provenance=provenance)
    assert remaining_budget(state) == {"turns": 16, "searches": 5, "reads": 10, "web_searches": 5}
    assert state.turns == 17 and state.searches == 5 and state.reads == 10 and state.web_searches == 5
    assert state.evidence == [hit] and len(state.read_history) == 35
    state = AgentState.model_validate_json(state.model_dump_json())
    decisions = iter([
        AgentDecision(action="search_evidence", summary="定向查", queries=[query()]),
        AgentDecision(action="search_web", summary="定向联网", queries=[query()]),
        AgentDecision(action="read_evidence", summary="读取新原文", read_requests=[EvidenceReadRequest(source_id="s1", chunk_id="new")]),
        AgentDecision(action="finish", summary="提交", draft=_draft()),
    ])
    run_agent(state, material="", intake={}, decide=lambda *_: next(decisions),
              search=lambda *_: [], web_search=lambda *_: WebSearchBatch(),
              read_evidence=lambda *_a, **_k: EvidenceRead(hits=[_hit("new", "s1")]),
              finalize=lambda *_: {}, checkpoint=lambda _: None)
    assert state.status == "completed"
    assert (state.turns, state.searches, state.reads, state.web_searches) == (21, 6, 11, 6)
    assert remaining_budget(state) == {"turns": 12, "searches": 4, "reads": 9, "web_searches": 4}
    assert [h.chunk_id for h in state.evidence] == ["old", "new"]


def test_stale_or_invalid_human_input_cannot_renew_a_round():
    state = AgentState(goal="审查", status="waiting_input", gate_id="valid", turns=16)
    before = state.model_dump()
    for gate, answer in [("old", "继续"), ("valid", " ")]:
        with pytest.raises(ValueError):
            answer_agent(state, gate_id=gate, answer=answer)
        assert state.model_dump() == before


def test_restart_without_human_gate_keeps_consumed_budget():
    state = AgentState(goal="审查", turns=20, budget_start=BudgetUsage(turns=19), max_turns=1)
    before = state.budget_start.model_dump()
    run_agent(state, material="", intake={}, decide=lambda *_: pytest.fail("must not renew on restart"),
              search=lambda *_: [], web_search=lambda *_: WebSearchBatch(), finalize=lambda *_: {}, checkpoint=lambda _: None)
    assert state.budget_start.model_dump() == before and state.turns == 20
    assert remaining_budget(state)["turns"] == 0


def test_last_turn_can_pause_and_receive_a_new_round_without_duplicate_gate_ids():
    state = AgentState(goal="审查", max_turns=1)
    ids = []
    for _ in range(3):
        run_agent(state, material="", intake={},
                  decide=lambda *_: AgentDecision(action="request_input", summary="需要人工确认", question="请确认继续调查"),
                  search=lambda *_: [], web_search=lambda *_: WebSearchBatch(), finalize=lambda *_: {}, checkpoint=lambda _: None)
        assert state.status == "waiting_input"
        ids.append(state.gate_id)
        answer_agent(state, gate_id=state.gate_id, answer="继续", provenance="reviewer_instruction")
        assert remaining_budget(state)["turns"] == 1
    assert ids == ["input_1", "input_3", "input_5"]


@pytest.mark.parametrize("state_type", [AgentState, QAState])
def test_review_and_qa_allow_five_web_calls_per_round(state_type):
    state = state_type(goal="调查")
    decision = AgentDecision(action="search_web", summary="查当前争点", queries=[query()])
    calls = []
    for _ in range(5):
        investigate(state, decision, search=None, search_navigation=None, read_evidence=None,
                    web_search=lambda *_: calls.append(1) or WebSearchBatch(), checkpoint=lambda _: None)
    with pytest.raises(ValueError, match="Web 搜索预算已用尽"):
        investigate(state, decision, search=None, search_navigation=None, read_evidence=None,
                    web_search=lambda *_: pytest.fail("sixth web call"), checkpoint=lambda _: None)
    assert len(calls) == state.web_searches == state.max_web_searches == 5
