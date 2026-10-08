"""Tests for the Production Agent eval harness (Slice 2A).

The runtime is faked: these tests exercise the pause/resume harness,
deterministic metrics, judge contract and report rendering — never LLM,
Elasticsearch or pgvector.
"""

from __future__ import annotations

import pytest

from law_agent.review.agent import AgentState, AgentStep
from law_agent.review.evalset.agent_runner import (
    format_summary_markdown,
    format_summary_text,
    run_agent_case,
)
from law_agent.review.evalset.agent_schemas import (
    AgentCase,
    AgentRubric,
    JudgeVerdict,
    ScriptedAnswer,
)
from law_agent.review.llm import ReviewWorkflowFailed


def _case(**rubric_overrides) -> AgentCase:
    rubric = AgentRubric(**rubric_overrides)
    return AgentCase(
        case_id="agent_eval_t001",
        question="这种规模的出境要不要先申报？",
        material_text="公司将手机号发送给新加坡服务商，日均 50 万用户。",
        rubric=rubric,
    )


def _citation(
    source_id: str,
    *,
    can_cite: bool = True,
    article_no: str | None = "第四条",
    role: str = "primary_legal_basis",
    doc_type: str = "law",
) -> dict:
    return {
        "source_id": source_id,
        "chunk_id": f"chunk_{source_id}",
        "title": source_id,
        "can_cite_clause": can_cite,
        "article_no": article_no,
        "citation_role": role,
        "doc_type": doc_type,
    }


def _payload(
    *,
    risk: str = "medium",
    citations: list[dict] | None = None,
    freshness: bool = False,
) -> dict:
    return {
        "review_result": {
            "risk_level": risk,
            "legal_path": "数据出境安全评估",
            "conclusion": "x" * 40,
            "trigger_reasons": [],
            "missing_information": [],
            "citations": citations or [],
        },
        "freshness_hold": freshness,
        "web_impact": "core" if freshness else "none",
    }


def _completed_state(payload: dict, *, turns: int = 3, steps=None) -> AgentState:
    return AgentState(
        goal="g",
        status="completed",
        turns=turns,
        searches=2,
        reads=1,
        steps=steps
        if steps is not None
        else [AgentStep(number=1, action="search_evidence", summary="s")],
        result=payload,
    )


def _waiting_state(gate_id: str = "input_1", *, reads: int = 1) -> AgentState:
    return AgentState(
        goal="g",
        status="waiting_input",
        turns=2,
        reads=reads,
        gate_id=gate_id,
        pending_question="请确认统计口径",
        steps=[
            AgentStep(number=1, action="read_material", summary="读材料"),
            AgentStep(number=2, action="request_input", summary="请确认统计口径"),
        ],
    )


def test_direct_completion_passes_deterministic_checks() -> None:
    source = "cac_data_export_security_assessment_measures_2022"
    case = _case(must_cover_sources=[source])
    payload = _payload(citations=[_citation(source)])
    executor = lambda _task, _store: _completed_state(payload)

    result = run_agent_case(case, execute=executor, model_id="m1")

    assert result.status == "succeeded"
    assert result.deterministic_pass is True
    assert result.overall_pass is True
    assert result.cited_source_ids == [source]
    assert result.abstain_correct is True


def test_pause_scripted_answer_resume_then_complete() -> None:
    """Resumed final state is cumulative (production checkpoint semantics).

    The second state already contains the first attempt's steps; counters must
    come from that single final state and must not concatenate per-attempt
    histories (which would double-count request_input and denied reads).
    """

    case = AgentCase(
        case_id="agent_eval_t002",
        question="q",
        material_text="m",
        rubric=AgentRubric(),
        scripted_answers=[
            ScriptedAnswer(answer="统计口径为上一年度自然年。", provenance="applicant_statement")
        ],
    )

    def executor(task, store):
        if task.attempt_count == 1:
            return _waiting_state("input_1")
        assert task.attempt_count == 2
        # Cumulative state resumed from the checkpoint: first-attempt history
        # plus the inserted human_input and the post-resume investigation.
        resumed_steps = list(_waiting_state("input_1").steps)
        resumed_steps.append(AgentStep(number=3, action="human_input", summary="统计口径确认"))
        resumed_steps.append(AgentStep(number=4, action="search_evidence", summary="s"))
        payload = _payload(citations=[])
        return AgentState(
            goal="g",
            status="completed",
            turns=4,
            searches=1,
            reads=1,
            steps=resumed_steps,
            result=payload,
        )

    result = run_agent_case(case, execute=executor, model_id="m1")

    assert result.status == "succeeded"
    assert result.scripted_answers_used == 1
    assert result.scripted_answers_unused == 0
    assert result.questions_asked == ["请确认统计口径"]
    # Exactly one request_input — not two, even though the checkpointed
    # request_input step is present in both attempt 1 and the resumed state.
    assert result.request_inputs == 1
    assert result.turns == 4


def test_gate_specific_answer_is_bound_to_gate_id() -> None:
    case = AgentCase(
        case_id="agent_eval_t003",
        question="q",
        material_text="m",
        rubric=AgentRubric(),
        scripted_answers=[
            ScriptedAnswer(answer="不相关的答案", gate_id="input_9"),
            ScriptedAnswer(answer="正确答案", gate_id="input_1"),
        ],
    )
    captured: list[str] = []
    states = iter([_waiting_state("input_1"), _completed_state(_payload())])

    def executor(_task, _store):
        return next(states)

    def judge(_case, _payload, _questions, answers):
        captured.extend(answer.answer for answer in answers)
        return JudgeVerdict(overall_pass=True, judge_model="j1")

    result = run_agent_case(case, execute=executor, model_id="m1", judge=judge)

    assert result.status == "succeeded"
    assert captured == ["正确答案"]
    assert result.scripted_answers_used == 1
    assert result.scripted_answers_unused == 1


def test_waiting_without_scripted_answer_is_unanswered_gate() -> None:
    case = AgentCase(
        case_id="agent_eval_t004",
        question="q",
        material_text="m",
        rubric=AgentRubric(should_abstain=False),
    )
    executor = lambda _task, _store: _waiting_state("input_1")

    result = run_agent_case(case, execute=executor, model_id="m1")

    assert result.status == "unanswered_gate"
    assert result.overall_pass is None
    assert result.deterministic_pass is False
    assert result.deterministic_fail_reasons == ["unanswered_gate"]


def test_unexpected_abstention_fails() -> None:
    case = _case(should_abstain=False)
    executor = lambda _task, _store: _completed_state(
        _payload(risk="insufficient_evidence")
    )

    result = run_agent_case(case, execute=executor, model_id="m1")

    assert result.abstain_correct is False
    assert "unexpected_abstention" in result.deterministic_fail_reasons
    assert result.overall_pass is False


def test_required_abstention_missing_fails() -> None:
    case = _case(should_abstain=True, acceptable_outcomes=["insufficient_evidence"])
    executor = lambda _task, _store: _completed_state(_payload(risk="high"))

    result = run_agent_case(case, execute=executor, model_id="m1")

    assert result.abstain_correct is False
    assert "expected_abstention" in result.deterministic_fail_reasons
    assert result.outcome_acceptable is False


@pytest.mark.parametrize("risk", ["high", "insufficient_evidence"])
@pytest.mark.parametrize("semantic_pass", [True, False])
def test_conditional_report_uses_semantic_boundaries_instead_of_risk_label(risk, semantic_pass) -> None:
    case = _case(should_abstain=None, forbidden_judgments=["关键要件未知时确定例外不成立"])
    result = run_agent_case(
        case, execute=lambda _task, _store: _completed_state(_payload(risk=risk)),
        judge=lambda *_args: JudgeVerdict(overall_pass=semantic_pass),
    )

    assert result.abstain_correct is None
    assert result.deterministic_pass is True
    assert result.overall_pass is semantic_pass


def test_conditional_report_without_judge_has_no_overall_verdict() -> None:
    case = _case(should_abstain=None)
    result = run_agent_case(case, execute=lambda _task, _store: _completed_state(_payload()))

    assert result.deterministic_pass is True
    assert result.overall_pass is None


def test_missing_required_source_fails() -> None:
    case = _case(must_cover_sources=["required_law_2024"])
    executor = lambda _task, _store: _completed_state(
        _payload(citations=[_citation("other_law")])
    )

    result = run_agent_case(case, execute=executor, model_id="m1")

    assert result.deterministic_pass is False
    assert any(
        reason.startswith("missing_required_sources:required_law_2024")
        for reason in result.deterministic_fail_reasons
    )


def test_standard_cited_as_clause_is_illegal() -> None:
    case = _case(forbidden_clause_sources=["tc260_guide_2024"])
    executor = lambda _task, _store: _completed_state(
        _payload(
            citations=[
                _citation(
                    "tc260_guide_2024",
                    role="implementation_reference",
                    doc_type="national_standard",
                )
            ]
        )
    )

    result = run_agent_case(case, execute=executor, model_id="m1")

    assert len(result.illegal_clause_citations) == 1
    assert result.illegal_clause_citations[0].source_id == "tc260_guide_2024"


def test_regulation_without_article_locator_is_illegal_clause_citation() -> None:
    case = _case()
    executor = lambda _task, _store: _completed_state(
        _payload(
            citations=[
                _citation(
                    "shanghai_rules_2024",
                    role="conditional_local_basis",
                    doc_type="regulation",
                    article_no=None,
                )
            ]
        )
    )

    result = run_agent_case(case, execute=executor, model_id="m1")

    assert len(result.illegal_clause_citations) == 1
    assert "条款定位" in result.illegal_clause_citations[0].reason


def test_local_regulation_with_article_is_a_valid_clause_citation() -> None:
    case = _case(must_cover_sources=["shanghai_rules_2024"])
    executor = lambda _task, _store: _completed_state(
        _payload(
            citations=[
                _citation(
                    "shanghai_rules_2024",
                    role="conditional_local_basis",
                    doc_type="regulation",
                    article_no="第十条",
                )
            ]
        )
    )

    result = run_agent_case(case, execute=executor, model_id="m1")

    assert result.illegal_clause_citations == []
    assert result.deterministic_pass is True


def test_freshness_hold_expected_but_not_triggered_fails() -> None:
    case = _case(expect_freshness_hold=True)
    executor = lambda _task, _store: _completed_state(
        _payload(risk="insufficient_evidence", freshness=False)
    )

    result = run_agent_case(case, execute=executor, model_id="m1")

    assert result.freshness_hold_correct is False
    assert "freshness_hold_expected" in result.deterministic_fail_reasons


def test_workflow_failure_is_recorded_not_raised() -> None:
    case = _case()

    def executor(_task, _store):
        raise ReviewWorkflowFailed(
            failed_node="semantic_grounding",
            reason="service_unavailable",
            message="judge model down",
            attempts=3,
        )

    result = run_agent_case(case, execute=executor, model_id="m1")

    assert result.status == "failed"
    assert result.failure_node == "semantic_grounding"
    assert result.failure_category == "service_unavailable"
    assert result.deterministic_pass is False
    assert result.overall_pass is False


def test_judge_infra_error_makes_case_unevaluated_not_pass() -> None:
    case = _case()
    executor = lambda _task, _store: _completed_state(_payload())
    judge = lambda *_args: JudgeVerdict(
        judge_model="j1", error="RateLimitError: upstream busy"
    )

    result = run_agent_case(case, execute=executor, model_id="m1", judge=judge)

    assert result.deterministic_pass is True
    assert result.overall_pass is None
    assert result.judge.error is not None


def test_judge_pass_with_deterministic_failure_still_fails() -> None:
    case = _case(must_cover_sources=["required_law_2024"])
    executor = lambda _task, _store: _completed_state(
        _payload(citations=[_citation("other_law")])
    )
    judge = lambda *_args: JudgeVerdict(overall_pass=True, judge_model="j1")

    result = run_agent_case(case, execute=executor, model_id="m1", judge=judge)

    assert result.overall_pass is False


def test_deterministic_failure_is_not_masked_by_judge_error() -> None:
    # Deterministic FAIL + judge infra failure must stay FAIL, not UNEVALUATED:
    # a proven Agent error cannot be hidden by an evaluator outage.
    case = _case(must_cover_sources=["required_law_2024"])
    executor = lambda _task, _store: _completed_state(
        _payload(citations=[_citation("other_law")])
    )
    judge = lambda *_args: JudgeVerdict(
        judge_model="j1", error="RateLimitError: upstream busy"
    )

    result = run_agent_case(case, execute=executor, model_id="m1", judge=judge)

    assert result.deterministic_pass is False
    assert result.judge.error is not None
    assert result.overall_pass is False


def test_judge_failure_overrides_deterministic_pass() -> None:
    case = _case()
    executor = lambda _task, _store: _completed_state(_payload())
    judge = lambda *_args: JudgeVerdict(
        legal_correctness="fail", overall_pass=False, reasons=["路径错误"], judge_model="j1"
    )

    result = run_agent_case(case, execute=executor, model_id="m1", judge=judge)

    assert result.deterministic_pass is True
    assert result.overall_pass is False
    assert result.judge.overall_pass is False


def test_budget_abstention_step_is_counted() -> None:
    state = _completed_state(
        _payload(risk="insufficient_evidence"),
        steps=[
            AgentStep(number=1, action="search_evidence", summary="s"),
            AgentStep(number=2, action="budget_abstention", summary="证据不足"),
        ],
    )
    case = _case(should_abstain=True, acceptable_outcomes=["insufficient_evidence"])
    result = run_agent_case(case, execute=lambda _t, _s: state, model_id="m1")

    assert result.budget_exhausted is True
    assert result.deterministic_pass is True


def test_duplicate_read_denial_is_counted() -> None:
    state = AgentState(
        goal="g",
        status="completed",
        turns=4,
        reads=2,
        steps=[
            AgentStep(number=1, action="read_evidence", summary="r"),
            AgentStep(
                number=2,
                action="read_evidence",
                summary="r",
                observation={"error": "相同位置的证据已经读过", "already_returned_chunk_id": "c1"},
            ),
        ],
        result=_payload(),
    )
    result = run_agent_case(_case(), execute=lambda _t, _s: state, model_id="m1")

    assert result.duplicate_read_denials == 1


def test_reports_render_without_error() -> None:
    from law_agent.review.evalset.agent_schemas import AgentEvalSummary

    case = _case(must_cover_sources=["missing_law"])
    failure = run_agent_case(
        case,
        execute=lambda _t, _s: _completed_state(_payload()),
        model_id="m1",
        judge=lambda *_a: JudgeVerdict(
            legal_correctness="minor_issue", overall_pass=False, reasons=["r"], judge_model="j1"
        ),
    )
    unevaluated = run_agent_case(
        _case(),
        execute=lambda _t, _s: _completed_state(_payload()),
        model_id="m1",
        judge=lambda *_a: JudgeVerdict(judge_model="j1", error="upstream 500"),
    )
    unevaluated.case_id = "agent_eval_t007"
    summary = AgentEvalSummary(
        generated_at="2026-10-08T00:00:00Z",
        suite="smoke",
        agent_model="m1",
        judge_model="j1",
        rerank_mode="off",
        chunks_path="chunks.jsonl",
        results=[failure, unevaluated],
        total_cases=2,
        completed_cases=2,
        overall_fail_count=1,
        overall_unevaluated_count=1,
    )

    text = format_summary_text(summary)
    markdown = format_summary_markdown(summary)
    assert "agent_eval_t001" in text
    assert "Production Agent Evaluation" in markdown
    assert "missing_law" in markdown
    assert "UNEV" in text
    assert "UNEVALUATED" in markdown
    assert "FAIL 1" in text
    assert "judge=unev" in text


def test_smoke_suite_resolves_scenarios_without_copying_material() -> None:
    from law_agent.review.evalset.agent_cases import SMOKE_CASE_IDS, get_agent_cases

    cases = get_agent_cases("smoke")
    assert {case.case_id for case in cases} == set(SMOKE_CASE_IDS)
    assert all(case.material_text and case.question for case in cases)
    assert all(case.rubric.should_abstain for case in cases)
    # Case A drives the pause/resume mechanism; its unverified applicant
    # answer must not turn into a definitive conclusion (it stays abstain).
    cross_border = next(c for c in cases if c.case_id == "eval_cross_border_001")
    assert len(cross_border.scripted_answers) == 1
    assert cross_border.scripted_answers[0].provenance == "applicant_statement"
    assert cross_border.rubric.should_abstain is True
    assert cross_border.rubric.acceptable_outcomes == ["insufficient_evidence"]


def _cli_args(**overrides):
    import argparse

    defaults = {
        "suite": "smoke",
        "case_ids": None,
        "chunks": "chunks.jsonl",
        "rerank_mode": "off",
        "agent_model": None,
        "judge_model": None,
        "no_judge": True,
        "output": None,
        "report": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_cli_named_suite_does_not_become_custom(monkeypatch, capsys) -> None:
    from law_agent.review import cli as review_cli
    from law_agent.review.evalset import agent_runner
    from law_agent.review.evalset.agent_schemas import AgentEvalSummary

    captured: dict = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return AgentEvalSummary(
            generated_at="2026-10-08T00:00:00Z",
            suite=kwargs["suite"],
            agent_model="m1",
            rerank_mode="off",
            chunks_path="chunks.jsonl",
        )

    monkeypatch.setattr(
        "law_agent.config.require_llm_config", lambda: type("C", (), {"model": "m1"})()
    )
    monkeypatch.setattr(agent_runner, "run_agent_evaluation", fake_run)

    code = review_cli._cmd_agent_eval(_cli_args())

    assert code == 0
    assert captured["cases"] is None
    assert captured["suite"] == "smoke"
    assert "suite=smoke" in capsys.readouterr().out


def test_cli_case_filter_passes_explicit_cases(monkeypatch) -> None:
    from law_agent.review import cli as review_cli
    from law_agent.review.evalset import agent_runner
    from law_agent.review.evalset.agent_schemas import AgentEvalSummary

    captured: dict = {}

    monkeypatch.setattr(
        "law_agent.config.require_llm_config", lambda: type("C", (), {"model": "m1"})()
    )
    monkeypatch.setattr(
        agent_runner,
        "run_agent_evaluation",
        lambda **kwargs: (captured.update(kwargs) or AgentEvalSummary(
            generated_at="2026-10-08T00:00:00Z",
            suite="custom",
            agent_model="m1",
            rerank_mode="off",
            chunks_path="chunks.jsonl",
        )),
    )

    code = review_cli._cmd_agent_eval(
        _cli_args(case_ids=["eval_cross_border_001"])
    )

    assert code == 0
    assert captured["cases"] is not None
    assert [c.case_id for c in captured["cases"]] == ["eval_cross_border_001"]


def test_cli_unknown_case_id_returns_error(monkeypatch) -> None:
    from law_agent.review import cli as review_cli
    from law_agent.review.evalset import agent_runner

    monkeypatch.setattr(
        "law_agent.config.require_llm_config", lambda: type("C", (), {"model": "m1"})()
    )
    monkeypatch.setattr(
        agent_runner, "run_agent_evaluation", lambda **kwargs: None
    )

    code = review_cli._cmd_agent_eval(_cli_args(case_ids=["does_not_exist"]))

    assert code == 2


def test_cli_empty_core_suite_returns_error(monkeypatch) -> None:
    from law_agent.review import cli as review_cli
    from law_agent.review.evalset import agent_cases, agent_runner

    monkeypatch.setattr(
        "law_agent.config.require_llm_config", lambda: type("C", (), {"model": "m1"})()
    )
    monkeypatch.setattr(agent_cases, "get_agent_cases", lambda suite: [])
    monkeypatch.setattr(
        agent_runner, "run_agent_evaluation", lambda **kwargs: None
    )

    code = review_cli._cmd_agent_eval(_cli_args(suite="core"))

    assert code == 2


def test_suite_label_uses_named_suite_not_always_custom(monkeypatch) -> None:
    from law_agent.review.evalset import agent_runner

    monkeypatch.setattr(
        agent_runner,
        "make_production_executor",
        lambda case, **kwargs: (lambda task, store: _completed_state(_payload())),
    )
    summary = agent_runner.run_agent_evaluation(
        suite="smoke", agent_model="m1", use_judge=False
    )
    assert summary.suite == "smoke"
    assert summary.total_cases == 2

    custom = AgentCase(
        case_id="custom_1", question="q", material_text="m", rubric=AgentRubric()
    )
    summary_custom = agent_runner.run_agent_evaluation(
        cases=[custom], suite="smoke", agent_model="m1", use_judge=False
    )
    assert summary_custom.suite == "custom"


def test_judge_defaults_to_agent_model(monkeypatch) -> None:
    from law_agent.review.evalset import agent_runner

    constructed: list[str] = []

    class _RecordingJudge:
        def __init__(self, *, model_id=None):
            constructed.append(model_id or "")
            self.model_id = model_id

        def __call__(self, case, payload, questions_asked, answers_used):
            return JudgeVerdict(overall_pass=True, judge_model=self.model_id)

    monkeypatch.setattr(agent_runner, "StructuredAgentJudge", _RecordingJudge)
    monkeypatch.setattr(
        agent_runner,
        "make_production_executor",
        lambda case, **kwargs: (lambda task, store: _completed_state(_payload())),
    )
    case = AgentCase(
        case_id="custom_2", question="q", material_text="m", rubric=AgentRubric()
    )

    default_summary = agent_runner.run_agent_evaluation(
        cases=[case], agent_model="model-A"
    )
    explicit_summary = agent_runner.run_agent_evaluation(
        cases=[case], agent_model="model-A", judge_model="model-J"
    )

    assert constructed == ["model-A", "model-J"]
    assert default_summary.judge_model == "model-A"
    assert explicit_summary.judge_model == "model-J"


def test_core_candidates_have_review_boundary_and_valid_intake() -> None:
    from law_agent.review.evalset.agent_cases import get_agent_cases
    from law_agent.review.http.schemas import IntakePayload

    cases = get_agent_cases("core")
    assert 8 <= len(cases) <= 20
    assert len({c.case_id for c in cases}) == len(cases)
    assert all(c.review_status == "candidate" for c in cases)
    for case in cases:
        IntakePayload.model_validate(case.intake)
        assert case.selection_reason
        assert case.rubric.allowed_judgments
        assert case.rubric.forbidden_judgments
        assert case.reference_basis


def test_provider_balance_failure_is_unevaluated_with_checkpoint_counters() -> None:
    def execute(task, store):
        state = AgentState(goal="g", turns=4, searches=1)
        store.checkpoint_agent(task.id, state=state.model_dump(mode="json"), expected_attempt=task.attempt_count)
        raise ReviewWorkflowFailed(failed_node="compliance_agent", reason="llm_api_error", message="LLM request failed with HTTP 402: Insufficient Balance", attempts=1)

    result = run_agent_case(_case(), execute=execute)
    assert result.status == "blocked"
    assert result.overall_pass is None
    assert result.turns == 4
    assert result.searches == 1


def test_smoke_results_remain_framework_checks() -> None:
    from law_agent.review.evalset.agent_cases import get_agent_cases

    case = get_agent_cases("smoke")[1]
    result = run_agent_case(case, execute=lambda task, store: _completed_state(_payload(risk="insufficient_evidence")))
    assert result.review_status == "framework_check"


def test_judge_sees_legal_constraints_and_cited_evidence_without_production_verdict() -> None:
    from types import SimpleNamespace

    from law_agent.review.evalset.agent_runner import StructuredAgentJudge

    captured = []
    judge = object.__new__(StructuredAgentJudge)
    judge.model_id = "j1"
    judge.on_input = captured.extend
    judge.node = SimpleNamespace(run=lambda messages: JudgeVerdict(overall_pass=True))
    case = _case(allowed_judgments=["允许合同或认证"], forbidden_judgments=["禁止按旧门槛强制评估"])
    payload = _payload(citations=[_citation("law")])
    payload["review_result"]["claims"] = [{"text": "正式法律断言", "supporting_chunk_ids": ["chunk_law"]}]
    payload["evidence_chunks"] = [{"chunk_id": "chunk_law", "text": "对应原文"}, {"chunk_id": "unused", "text": "不相关的未引用证据"}]
    payload["semantic_grounding"] = {"conclusion_reason": "生产评审秘密判词"}
    payload["fact_ledger"] = [{"field": "supplemental_statement", "value": "人数只是申请人估计", "source_type": "applicant_statement", "source_ref": "gate-1", "status": "unverified"}]
    verdict = judge(case, payload, [], [])
    prompt = captured[1].content
    assert verdict.overall_pass is True
    assert "允许合同或认证" in prompt
    assert "禁止按旧门槛强制评估" in prompt
    assert "正式法律断言" in prompt
    assert "人数只是申请人估计" in prompt
    assert '"status": "unverified"' in prompt
    assert "对应原文" in prompt
    assert "不相关的未引用证据" not in prompt
    assert "生产评审秘密判词" not in prompt


def test_required_law_mentioned_only_as_context_does_not_satisfy_clause_coverage() -> None:
    case = _case(must_cover_sources=["law"])
    result = run_agent_case(case, execute=lambda task, store: _completed_state(_payload(citations=[_citation("law", can_cite=False)])))
    assert result.cited_source_ids == ["law"]
    assert result.missing_required_sources == ["law"]
    assert result.deterministic_pass is False


def test_unknown_suite_rejected() -> None:
    from law_agent.review.evalset.agent_cases import get_agent_cases

    with pytest.raises(ValueError):
        get_agent_cases("full")


def test_controlled_source_is_unavailable_to_reads_and_service_hits(tmp_path) -> None:
    from law_agent.review.agent_tools import ComplianceAgentTools
    from law_agent.review.evalset.agent_cases import get_agent_cases
    from law_agent.review.evalset.agent_runner import FixedWebClient, prepare_controlled_corpus
    from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH, load_corpus
    from law_agent.review.retrieval.neighbors import hit_from_chunk
    from law_agent.review.retrieval.temporal import filter_hits_as_of
    from law_agent.review.schemas import ReviewFacts
    from law_agent.review.web_research import WebResearch

    case = next(c for c in get_agent_cases("core") if c.controlled_web)
    path = tmp_path / "chunks.jsonl"
    prepare_controlled_corpus(case, DEFAULT_CHUNKS_PATH, path)
    chunks = load_corpus(path)
    held = next(c for c in load_corpus() if c.source_id in case.controlled_web.held_out_source_ids)
    assert held.source_id not in {c.source_id for c in chunks}
    by_id = {c.chunk_id: c for c in chunks}
    from datetime import date

    assert not filter_hits_as_of([hit_from_chunk(held, 1)], by_id, as_of=date(2026, 10, 8))
    tools = object.__new__(ComplianceAgentTools)
    tools._chunks = chunks
    with pytest.raises(ValueError, match="受控法律库中没有来源"):
        tools.read_evidence(held.source_id, "第一条", None, ReviewFacts())
    from law_agent.review.schemas import RetrievalQuery

    findings = WebResearch(client=FixedWebClient(case), corpus_chunks=chunks).search([RetrievalQuery(query_id="q1", text="汽车数据", query_type="industry_condition")])
    assert len(findings) == 1
    assert findings[0].known_source_id is None


def test_controlled_web_rejects_nonofficial_results_before_model_call(tmp_path) -> None:
    from law_agent.review.evalset.agent_cases import get_agent_cases
    from law_agent.review.evalset.agent_runner import prepare_controlled_corpus
    from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH

    case = next(c for c in get_agent_cases("core") if c.controlled_web).model_copy(deep=True)
    case.controlled_web.results[0].url = "https://untrusted.example/new-law"
    with pytest.raises(ValueError, match="trusted official"):
        prepare_controlled_corpus(case, DEFAULT_CHUNKS_PATH, tmp_path / "chunks.jsonl")
