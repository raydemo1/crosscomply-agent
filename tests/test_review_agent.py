"""Behavioral tests for the dynamic single-Agent loop."""

from law_agent.review.agent import (
    AgentDecision,
    AgentModel,
    AgentState,
    answer_agent,
    run_agent,
)
from law_agent.review.result_builder import LLMReviewResultDraft
from law_agent.review.schemas import RetrievalQuery
from tests.test_review_llm import FakeClient


def _draft() -> LLMReviewResultDraft:
    return LLMReviewResultDraft(
        risk_level="insufficient_evidence",
        decision_summary="当前材料和法源证据仍不足以形成正式风险结论，需要明确标示调查边界、尚未核实事项以及后续补充要求。",
        conclusion="现有证据不足，暂不形成正式合规路径结论。",
        trigger_reasons=["证据不足"],
        missing_information=["境外接收方所在地区"],
        recommended_actions=["补充接收方信息"],
        risk_boundaries=["未覆盖地方和行业特别规则"],
    )


def test_agent_can_finish_without_fixed_intermediate_steps() -> None:
    decisions = iter([
        AgentDecision(action="finish", summary="交付当前调查结果", draft=_draft())
    ])
    checkpoints: list[AgentState] = []

    state = run_agent(
        AgentState(goal="审查境外 SaaS 接入"),
        material="已冻结材料",
        intake={},
        decide=lambda _state, _rule: next(decisions),
        search=lambda _queries, _facts: [],
        web_search=lambda _queries, _facts: [],
        finalize=lambda _draft_value, _state: {"review_result": {"risk_level": "insufficient_evidence"}},
        checkpoint=lambda current: checkpoints.append(current.model_copy(deep=True)),
    )

    assert state.status == "completed"
    assert [step.action for step in state.steps] == ["finish"]
    assert checkpoints[-1].status == "completed"


def test_agent_can_choose_multiple_retrieval_batches() -> None:
    decisions = iter([
        AgentDecision(
            action="search_evidence",
            summary="先查数据出境一般规则",
            queries=[RetrievalQuery(query_id="q1", query_type="legal_issue", text="数据出境规则")],
        ),
        AgentDecision(
            action="search_evidence",
            summary="再查境外 SaaS 场景",
            queries=[RetrievalQuery(query_id="q2", query_type="material_fact", text="境外 SaaS 接收方")],
        ),
        AgentDecision(action="finish", summary="交付调查结果", draft=_draft()),
    ])
    searched: list[str] = []
    state = run_agent(
        AgentState(goal="审查境外 SaaS 接入"),
        material="已冻结材料",
        intake={},
        decide=lambda _state, _rule: next(decisions),
        search=lambda queries, _facts: searched.extend(item.text for item in queries) or [],
        web_search=lambda _queries, _facts: [],
        finalize=lambda _draft_value, _state: {"ok": True},
        checkpoint=lambda _state: None,
    )

    assert searched == ["数据出境规则", "境外 SaaS 接收方"]
    assert [step.action for step in state.steps] == [
        "search_evidence", "search_evidence", "finish"
    ]


def test_agent_pauses_and_resumes_from_human_input() -> None:
    waiting = run_agent(
        AgentState(goal="确认接收方地区"),
        material="已冻结材料",
        intake={},
        decide=lambda _state, _rule: AgentDecision(
            action="request_input",
            summary="需要确认接收方地区",
            question="境外接收方位于哪个国家或地区？",
        ),
        search=lambda _queries, _facts: [],
        web_search=lambda _queries, _facts: [],
        finalize=lambda _draft_value, _state: {},
        checkpoint=lambda _state: None,
    )
    assert waiting.status == "waiting_input"
    resumed = answer_agent(
        waiting,
        gate_id=waiting.gate_id or "",
        answer="接收方位于新加坡。",
    )

    assert resumed.status == "running"
    assert resumed.steps[-1].action == "human_input"
    assert resumed.steps[-1].observation["answer"] == "接收方位于新加坡。"


def test_agent_plan_is_visible_without_blocking_the_run() -> None:
    decisions = iter([
        AgentDecision(
            action="propose_plan",
            summary="提交调查计划",
            plan=["核对材料", "检索法源", "形成有引用的结论"],
        ),
        AgentDecision(action="finish", summary="交付当前调查结果", draft=_draft()),
    ])
    state = run_agent(
        AgentState(goal="审查境外 SaaS 接入"),
        material="已冻结材料",
        intake={},
        decide=lambda _state, _rule: next(decisions),
        search=lambda _queries, _facts: [],
        web_search=lambda _queries, _facts: [],
        finalize=lambda _draft_value, _state: {"ok": True},
        checkpoint=lambda _state: None,
    )

    assert state.status == "completed"
    assert state.plan == ["核对材料", "检索法源", "形成有引用的结论"]
    assert [step.action for step in state.steps] == ["propose_plan", "finish"]


def test_agent_model_retries_invalid_decision_json_instead_of_failing_task() -> None:
    client = FakeClient(
        outputs=[
            {"type": "json_object", "plan": ["读取冻结材料"]},
            {
                "action": "propose_plan",
                "summary": "给出执行计划",
                "plan": ["读取冻结材料", "检索数据出境界定规则"],
            },
        ]
    )
    model = AgentModel(model_id="test-model", client=client)  # type: ignore[arg-type]

    assert model.node.max_retries >= 1

    decision = model(AgentState(goal="审查境外 SaaS 接入"), {})

    assert decision.action == "propose_plan"
    assert len(client.calls) == 2


def test_agent_model_sends_decision_schema_in_system_prompt() -> None:
    client = FakeClient(
        outputs=[
            {
                "action": "propose_plan",
                "summary": "给出执行计划",
                "plan": ["读取冻结材料", "检索数据出境界定规则"],
            }
        ]
    )
    model = AgentModel(model_id="test-model", client=client)  # type: ignore[arg-type]

    model(AgentState(goal="审查境外 SaaS 接入"), {})

    system = client.calls[0][0]
    assert system.role == "system"
    assert '"action"' in system.content
    assert '"summary"' in system.content
